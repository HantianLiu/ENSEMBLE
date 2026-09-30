from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.errors import (
    ProviderError,
    ResearchQualityControlError,
    RepresentativeUnavailableError,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.research.desk import ResearchDesk
from project_ensemble.research.documents import DocumentFetcher, LiteratureBundleManager
from project_ensemble.research.models import (
    FreshnessClass,
    NormalizedClaim,
    ResearchClaimResolution,
    ResearchClaimResolutionStatus,
    ResearchRequest,
    ResearchRequestOrigin,
    ResearchRoundDedupGroup,
    ResearchRoundReleaseGate,
    ResearchRoundSubmission,
    ResearchStage,
)
from project_ensemble.research.retrievers import ResearchRetriever
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.model_lanes import run_bounded_model_lanes
from project_ensemble.runtime.progress import TaskProgressItem
from project_ensemble.storage.meeting import MeetingRepository


_ROUND_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,95}$")
_GENERATED_LITERATURE_BUNDLE_VIEWS = frozenset({"manifest.json", "README.md"})


@lru_cache(maxsize=4)
def _submission_schema(max_claims: int) -> type[ResearchRoundSubmission]:
    if max_claims == 4:
        return ResearchRoundSubmission
    return create_model(
        f"ResearchRoundSubmissionMax{max_claims}",
        __base__=ResearchRoundSubmission,
        claims=(list[str], Field(default_factory=list, max_length=max_claims)),
    )


class ResearchRoundResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_id: str
    stage: ResearchStage
    representative_count: int = Field(ge=0)
    packet_count: int = Field(ge=0)
    qc_failed_claim_count: int = Field(default=0, ge=0)
    release_disposition: str = "COMPLETE"
    packet_ids: list[str] = Field(default_factory=list)
    evidence_snapshot_path: str
    released: bool = True


class ResearchRoundRunner:
    """Sealed, resumable Research Round with a public release manifest as its visibility gate."""

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        retriever: ResearchRetriever,
        document_fetcher: DocumentFetcher,
        freshness_windows: dict[FreshnessClass, int] | None = None,
        max_output_tokens: int | None = None,
        max_concurrent_claim_groups: int = 4,
        retrieval_max_retries: int = 0,
        retrieval_retry_base_delay_seconds: float = 0.5,
    ):
        if max_concurrent_claim_groups < 1:
            raise ValueError("max_concurrent_claim_groups must be positive")
        self.repo = repo
        self.engine = engine
        self.retriever = retriever
        self.document_fetcher = document_fetcher
        self.freshness_windows = freshness_windows
        self.max_output_tokens = max_output_tokens
        self.max_concurrent_claim_groups = max_concurrent_claim_groups
        self.retrieval_max_retries = retrieval_max_retries
        self.retrieval_retry_base_delay_seconds = retrieval_retry_base_delay_seconds
        self.resolver = GovernanceDocumentResolver(governance_docs)
        self.assembler = RepresentativeContextAssembler()
        manifest = json.loads(
            self.repo.docs.read_text("identity_private/meeting_manifest.json")
        )
        self.prompt_family = manifest.get("representative_prompt_family", "legacy_shared")
        if not manifest.get("research_enabled", False):
            raise ValueError("ResearchRoundRunner requires an enabled Research Desk")

    def run(
        self,
        *,
        round_id: str,
        stage: ResearchStage,
        subject_files: tuple[Path, ...],
        representative_records: list[dict[str, Any]] | None = None,
        max_claims_per_representative: int = 4,
    ) -> ResearchRoundResult:
        if not _ROUND_ID.fullmatch(round_id):
            raise ValueError("research round ID contains unsupported characters")
        snapshot_relative = (
            Path("public/research/rounds") / round_id / "evidence_snapshot.json"
        )
        snapshot_path = self.repo.root / snapshot_relative
        if snapshot_path.exists():
            self._ensure_release_event(
                json.loads(snapshot_path.read_text(encoding="utf-8")),
                snapshot_relative,
            )
            return self._result_from_snapshot(snapshot_path)

        claim_limit = self._load_or_freeze_claim_limit(
            round_id=round_id,
            requested_limit=max_claims_per_representative,
        )

        records = representative_records or json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        records = sorted(records, key=lambda item: item["representative_id"])
        self._validate_subject_files(subject_files)
        self.engine.status.phase = MeetingPhase.RESEARCH_ROUND
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.RESEARCH_ROUND,
            f"{round_id} · 收集 {len(records)} 名 Representative 的密封检索请求；"
            f"每人 0–{claim_limit} 条",
        )

        submissions = self._collect_submissions(
            round_id=round_id,
            records=records,
            subject_files=subject_files,
            max_claims_per_representative=claim_limit,
        )
        claims = self._freeze_submissions(
            round_id=round_id,
            stage=stage,
            records=records,
            submissions=submissions,
            subject_files=subject_files,
            max_claims_per_representative=claim_limit,
        )
        stage_repo = self._staging_repo(round_id)
        desk = ResearchDesk(
            repo=stage_repo,
            engine=self.engine,
            retriever=self.retriever,
            freshness_windows=self.freshness_windows,
            document_fetcher=self.document_fetcher,
            max_output_tokens=self.max_output_tokens,
            retrieval_max_retries=self.retrieval_max_retries,
            retrieval_retry_base_delay_seconds=self.retrieval_retry_base_delay_seconds,
        )
        groups, rejected_group_ids, normalization_qc_failures = self._normalize_and_group(
            round_id=round_id,
            stage=stage,
            claims=claims,
            desk=desk,
        )
        resolutions, packets = self._resolve_groups(
            round_id=round_id,
            stage=stage,
            groups=groups,
            rejected_group_ids=rejected_group_ids,
            normalization_qc_failures=normalization_qc_failures,
            desk=desk,
            stage_repo=stage_repo,
        )
        result = self._release(
            round_id=round_id,
            stage=stage,
            records=records,
            groups=groups,
            subject_files=subject_files,
            resolutions=resolutions,
            packets=packets,
            stage_repo=stage_repo,
        )
        for packet in packets:
            self.engine.progress.speech(
                "RESEARCH_DESK",
                f"证据包 {packet['packet_id']} · {packet['knowledge_status']}",
                json.dumps(
                    {
                        "claim": packet["normalized_claim"],
                        "consensus": packet["consensus"],
                        "source_count": len(packet["sources"]),
                        "retrieval_backends": packet["retrieval_backend_ids"],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        return result

    def _load_or_freeze_claim_limit(
        self,
        *,
        round_id: str,
        requested_limit: int,
    ) -> int:
        """Freeze a per-round cap while preserving already-started legacy rounds."""

        if not 1 <= requested_limit <= 4:
            raise ValueError("max_claims_per_representative must be between 1 and 4")
        base = Path("governance_private/research/rounds") / round_id
        relative = base / "round_policy.json"
        path = self.repo.root / relative
        if path.exists():
            policy = json.loads(path.read_text(encoding="utf-8"))
            frozen_limit = int(policy["max_claims_per_representative"])
            if not 1 <= frozen_limit <= 4:
                raise ValueError("frozen Research Round claim limit is invalid")
            return frozen_limit

        legacy_activity = False
        round_root = self.repo.root / base
        if round_root.exists() and any(item.is_file() for item in round_root.rglob("*")):
            legacy_activity = True
        exchange_root = self.repo.root / "governance_private/provider_exchanges"
        if not legacy_activity and exchange_root.exists():
            expected_stage = f"research_round_submission:{round_id}"
            for exchange_path in exchange_root.glob("X-*.json"):
                try:
                    exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                if exchange.get("stage") == expected_stage:
                    legacy_activity = True
                    break

        frozen_limit = 4 if legacy_activity else requested_limit
        policy = {
            "meeting_id": self.repo.meeting_id,
            "round_id": round_id,
            "policy_status": "TRIAL",
            "policy_version": "STAGE_SCALED_CLAIM_LIMIT_V1",
            "max_claims_per_representative": frozen_limit,
            "requested_limit_at_freeze": requested_limit,
            "compatibility_disposition": (
                "LEGACY_STARTED_ROUND_RETAINS_FOUR"
                if legacy_activity
                else "NEW_ROUND_STAGE_LIMIT"
            ),
        }
        self.repo.docs.write_once(
            relative, json.dumps(policy, indent=2, ensure_ascii=False)
        )
        self.repo.events.append(
            "RESEARCH_ROUND_CLAIM_LIMIT_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "round_id": round_id,
                "max_claims_per_representative": frozen_limit,
                "requested_limit": requested_limit,
                "compatibility_disposition": policy["compatibility_disposition"],
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        if legacy_activity and requested_limit != frozen_limit:
            self.engine.progress.info(
                f"{round_id} · 已启动的旧轮次保留每名 Representative 最多 4 条 claim"
            )
        return frozen_limit

    @staticmethod
    def incomplete_round_ids(repo: MeetingRepository) -> list[str]:
        """Return formal rounds that have private state but no public release gate."""
        private_root = repo.root / "governance_private/research/rounds"
        if not private_root.exists():
            return []
        return [
            path.name
            for path in sorted(private_root.iterdir())
            if path.is_dir()
            and not (
                repo.root
                / "public/research/rounds"
                / path.name
                / "evidence_snapshot.json"
            ).exists()
        ]

    @staticmethod
    def released_summary(repo: MeetingRepository) -> tuple[int, int]:
        """Count released rounds and distinct packets from durable public snapshots."""
        public_root = repo.root / "public/research/rounds"
        if not public_root.exists():
            return 0, 0
        snapshots = sorted(public_root.glob("*/evidence_snapshot.json"))
        packet_ids: set[str] = set()
        released_count = 0
        for path in snapshots:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if snapshot.get("status") != "RELEASED":
                continue
            released_count += 1
            packet_ids.update(str(value) for value in snapshot.get("packet_ids", []))
        return released_count, len(packet_ids)

    def _collect_submissions(
        self,
        *,
        round_id: str,
        records: list[dict[str, Any]],
        subject_files: tuple[Path, ...],
        max_claims_per_representative: int,
    ) -> dict[str, ResearchRoundSubmission]:
        root = Path("governance_private/research/rounds") / round_id / "submissions"
        submissions: dict[str, ResearchRoundSubmission] = {}
        missing: list[dict[str, Any]] = []
        for record in records:
            representative_id = record["representative_id"]
            path = self.repo.root / root / f"{representative_id}.json"
            if path.exists():
                submissions[representative_id] = ResearchRoundSubmission.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                self.engine.progress.info(
                    f"{round_id} · 已恢复 {representative_id} 的完整密封检索请求"
                )
            else:
                missing.append(record)

        lanes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        persona_order = {persona.value: index for index, persona in enumerate(Persona)}
        for record in missing:
            runtime = record["runtime"]
            lanes.setdefault((runtime["provider_id"], runtime["model_id"]), []).append(record)
        for lane in lanes.values():
            lane.sort(
                key=lambda item: (
                    persona_order.get(item["runtime"]["persona"], len(persona_order)),
                    item["representative_id"],
                )
            )

        if lanes:
            completed = run_bounded_model_lanes(
                lanes,
                lambda record: self._collect_one_submission(
                    round_id=round_id,
                    record=record,
                    subject_files=subject_files,
                    max_claims_per_representative=max_claims_per_representative,
                ),
                getattr(
                    self.engine,
                    "model_concurrency_limit",
                    lambda _provider_id, _model_id: 1,
                ),
                progress=self.engine.progress,
            )
            for representative_id, submission in completed:
                relative = root / f"{representative_id}.json"
                self.repo.docs.write_once(relative, submission.model_dump_json(indent=2))
                self.repo.events.append(
                    "RESEARCH_ROUND_SUBMISSION_RECEIVED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "round_id": round_id,
                        "representative_id": representative_id,
                        "claim_count": len(submission.claims),
                        "record_path": str(relative),
                    },
                    actor=representative_id,
                )
                submissions[representative_id] = submission
                self.engine.progress.info(
                    f"{round_id} · 已收到 {len(submissions)}/{len(records)} 份密封检索请求"
                )
        return submissions

    def _collect_one_submission(
        self,
        *,
        round_id: str,
        record: dict[str, Any],
        subject_files: tuple[Path, ...],
        max_claims_per_representative: int,
    ) -> tuple[str, ResearchRoundSubmission]:
        representative_id = record["representative_id"]
        spec = self.resolver.representative_context_spec(
            persona=Persona(record["runtime"]["persona"]),
            stage="research_request",
            representative_id=representative_id,
            public_state_files=subject_files,
            prompt_family=self.prompt_family,
        )
        system_text = self.assembler.assemble(spec)
        user_text = (
            "提交本证据窗口的密封 Research Desk 请求。只返回一个 JSON 对象，格式为"
            '{"claims":["主张1","主张2"]}。主张列表允许零至 '
            + str(max_claims_per_representative)
            + " 条不重复、具体且可由外部资料核验的事实主张。"
            "不得包含向其他代表提出的问题、政策建议、解释、请求者身份或 Markdown。"
        )
        response = self.engine.find_recorded_response(
            representative_id,
            system_text=system_text,
            user_text=user_text,
            stage=f"research_round_submission:{round_id}",
        )
        if response is None:
            response = self.engine.invoke_participant(
                representative_id,
                system_text=system_text,
                user_text=user_text,
                stage=f"research_round_submission:{round_id}",
                max_output_tokens=self.max_output_tokens,
            )
        submission = self.engine.validate_structured_response(
            representative_id,
            response=response,
            schema_model=_submission_schema(max_claims_per_representative),
            stage=f"research_round_submission:{round_id}",
            max_output_tokens=self.max_output_tokens,
            semantic_requirement=(
                "逐字保留每条已提交主张；列表须包含零至 "
                f"{max_claims_per_representative} 条不重复的事实主张。"
            ),
        )
        return representative_id, ResearchRoundSubmission.model_validate(
            submission.model_dump(mode="python")
        )

    def _freeze_submissions(
        self,
        *,
        round_id: str,
        stage: ResearchStage,
        records: list[dict[str, Any]],
        submissions: dict[str, ResearchRoundSubmission],
        subject_files: tuple[Path, ...],
        max_claims_per_representative: int,
    ) -> list[dict[str, Any]]:
        relative = Path("governance_private/research/rounds") / round_id / "frozen_requests.json"
        path = self.repo.root / relative
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["claims"]
        if set(submissions) != {record["representative_id"] for record in records}:
            raise ValueError("Research Round cannot freeze before every Representative submits")
        claims: list[dict[str, Any]] = []
        for record in records:
            representative_id = record["representative_id"]
            for ordinal, claim in enumerate(submissions[representative_id].claims, start=1):
                digest = hashlib.sha256(
                    f"{round_id}\0{representative_id}\0{ordinal}".encode("utf-8")
                ).hexdigest()[:16].upper()
                claims.append(
                    {
                        "claim_id": f"RC-{digest}",
                        "request_id": f"RQ-{digest}",
                        "requester_id": representative_id,
                        "claim": claim,
                    }
                )
        record = {
            "meeting_id": self.repo.meeting_id,
            "round_id": round_id,
            "stage": stage.value,
            "status": "FROZEN",
            "representative_ids": [item["representative_id"] for item in records],
            "max_claims_per_representative": max_claims_per_representative,
            "subject_files": [self._subject_record(path) for path in subject_files],
            "claims": claims,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "RESEARCH_ROUND_REQUEST_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "round_id": round_id,
                "representative_count": len(records),
                "claim_count": len(claims),
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        return claims

    def _normalize_and_group(
        self,
        *,
        round_id: str,
        stage: ResearchStage,
        claims: list[dict[str, Any]],
        desk: ResearchDesk,
    ) -> tuple[
        list[ResearchRoundDedupGroup],
        set[str],
        dict[str, dict[str, str]],
    ]:
        base = Path("governance_private/research/rounds") / round_id
        normalized_claims: list[
            tuple[dict[str, Any], NormalizedClaim, str, dict[str, str] | None]
        ] = []
        for claim in claims:
            relative = base / "normalized" / f"{claim['claim_id']}.json"
            path = self.repo.root / relative
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                normalized = NormalizedClaim.model_validate(data["normalized_claim"])
                fingerprint = str(data["claim_fingerprint"])
                qc_failure = data.get("qc_failure")
            else:
                request = ResearchRequest(
                    requester_id=claim["requester_id"],
                    stage=stage,
                    claim=claim["claim"],
                )
                qc_failure = None
                try:
                    normalized = desk.normalize_request(request)
                    fingerprint = desk.fingerprint(normalized)
                except ResearchQualityControlError as exc:
                    qc_failure = {"code": exc.code, "summary": exc.summary}
                    normalized = self._qc_failed_normalized_claim(claim["claim"], exc)
                    fingerprint = hashlib.sha256(
                        f"research-qc-failure\0{claim['claim_id']}".encode("utf-8")
                    ).hexdigest()
                self.repo.docs.write_once(
                    relative,
                    json.dumps(
                        {
                            "claim_id": claim["claim_id"],
                            "request_id": claim["request_id"],
                            "normalized_claim": normalized.model_dump(mode="json"),
                            "claim_fingerprint": fingerprint,
                            **({"qc_failure": qc_failure} if qc_failure else {}),
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                )
            normalized_claims.append((claim, normalized, fingerprint, qc_failure))

        grouped: dict[
            str, list[tuple[dict[str, Any], NormalizedClaim, dict[str, str] | None]]
        ] = {}
        for claim, normalized, fingerprint, qc_failure in normalized_claims:
            grouped.setdefault(fingerprint, []).append((claim, normalized, qc_failure))
        groups: list[ResearchRoundDedupGroup] = []
        rejected: set[str] = set()
        qc_failures: dict[str, dict[str, str]] = {}
        for fingerprint in sorted(grouped):
            members = grouped[fingerprint]
            group_id = "RG-" + fingerprint[:16].upper()
            group = ResearchRoundDedupGroup(
                group_id=group_id,
                claim_fingerprint=fingerprint,
                normalized_claim=members[0][1].normalized_claim,
                origins=[
                    ResearchRequestOrigin(
                        request_id=item[0]["request_id"],
                        requester_id=item[0]["requester_id"],
                    )
                    for item in members
                ],
            )
            groups.append(group)
            if members[0][2] is not None:
                qc_failures[group_id] = members[0][2]
            elif not members[0][1].is_researchable:
                rejected.add(group_id)
        relative = base / "dedup_groups.json"
        path = self.repo.root / relative
        serialized = {
            "round_id": round_id,
            "groups": [group.model_dump(mode="json") for group in groups],
            "rejected_group_ids": sorted(rejected),
        }
        if qc_failures:
            serialized["qc_failures"] = qc_failures
        if not path.exists():
            self.repo.docs.write_once(relative, json.dumps(serialized, indent=2, ensure_ascii=False))
        elif json.loads(path.read_text(encoding="utf-8")) != serialized:
            raise ValueError("frozen Research Round dedup groups changed during recovery")
        return groups, rejected, qc_failures

    @staticmethod
    def _qc_failed_normalized_claim(
        original_claim: str,
        exc: ResearchQualityControlError,
    ) -> NormalizedClaim:
        """Create a non-evidentiary placeholder so one QC failure can be released atomically."""

        return NormalizedClaim(
            is_researchable=False,
            normalized_claim=original_claim,
            verification_question=original_claim,
            supporting_query="Not executed because Research Desk normalization failed QC.",
            contradictory_query="Not executed because Research Desk normalization failed QC.",
            limitations_query="Not executed because Research Desk normalization failed QC.",
            alternatives_query="Not executed because Research Desk normalization failed QC.",
            scope_terms=[],
            source_domain="GENERAL",
            source_domain_rationale="Source routing was not established because normalization failed QC.",
            freshness_class=FreshnessClass.STABLE,
            freshness_rationale="No cacheable evidence conclusion was produced.",
            rejection_reason=f"{exc.code}: {exc.summary}",
        )

    def _record_qc_failure(
        self,
        round_id: str,
        group: ResearchRoundDedupGroup,
        resolution: ResearchClaimResolution,
    ) -> None:
        relative = (
            Path("audit_private/research/qc_failures")
            / round_id
            / f"{group.group_id}.json"
        )
        path = self.repo.root / relative
        if not path.exists():
            self.repo.docs.write_once(
                relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "round_id": round_id,
                        "group_id": group.group_id,
                        "normalized_claim": group.normalized_claim,
                        "failure_code": resolution.failure_code,
                        "failure_summary": resolution.failure_summary,
                        "disposition": "NO_EVIDENCE_UPGRADE_NONBLOCKING",
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "RESEARCH_CLAIM_QC_FAILED_NONBLOCKING",
                {
                    "meeting_id": self.repo.meeting_id,
                    "round_id": round_id,
                    "group_id": group.group_id,
                    "failure_code": resolution.failure_code,
                    "audit_record_path": str(relative),
                    "meeting_continues": True,
                },
                actor="RESEARCH_DESK",
            )
        self.engine.progress.info(
            f"{round_id} · {group.group_id} 文献 QC 未通过，记为 QC_FAILED（无证据升级）；会议继续"
        )

    def _resolve_groups(
        self,
        *,
        round_id: str,
        stage: ResearchStage,
        groups: list[ResearchRoundDedupGroup],
        rejected_group_ids: set[str],
        normalization_qc_failures: dict[str, dict[str, str]],
        desk: ResearchDesk,
        stage_repo: MeetingRepository,
    ) -> tuple[list[ResearchClaimResolution], list[dict[str, Any]]]:
        base = Path("governance_private/research/rounds") / round_id / "resolutions"
        frozen = json.loads(
            (
                self.repo.root
                / "governance_private/research/rounds"
                / round_id
                / "frozen_requests.json"
            ).read_text(encoding="utf-8")
        )
        claims_by_request = {item["request_id"]: item for item in frozen["claims"]}
        normalized_root = self.repo.root / "governance_private/research/rounds" / round_id / "normalized"
        resolutions_by_id: dict[str, ResearchClaimResolution] = {}
        packets_by_id: dict[str, dict[str, Any]] = {}
        pending_groups: list[ResearchRoundDedupGroup] = []
        completed_count = 0
        for group in groups:
            relative = base / f"{group.group_id}.json"
            path = self.repo.root / relative
            if path.exists():
                resolution = ResearchClaimResolution.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                resolutions_by_id[group.group_id] = resolution
                if resolution.packet_id:
                    packet_path = (
                        stage_repo.root
                        / "public/research/evidence_packets"
                        / f"{resolution.packet_id}.json"
                    )
                    packets_by_id[resolution.packet_id] = json.loads(
                        packet_path.read_text(encoding="utf-8")
                    )
                completed_count += 1
                self.engine.progress.info(
                    f"{round_id} · 已恢复 {completed_count}/{len(groups)} 个去重 claim group"
                )
            else:
                pending_groups.append(group)

        def resolve_group(
            group: ResearchRoundDedupGroup,
        ) -> tuple[ResearchRoundDedupGroup, ResearchClaimResolution, dict[str, Any] | None]:
            packet_data = None
            if group.group_id in normalization_qc_failures:
                failure = normalization_qc_failures[group.group_id]
                resolution = ResearchClaimResolution(
                    claim_id=group.group_id,
                    status=ResearchClaimResolutionStatus.QC_FAILED,
                    failure_code=failure["code"],
                    failure_summary=failure["summary"],
                )
                self._record_qc_failure(round_id, group, resolution)
            elif group.group_id in rejected_group_ids:
                resolution = ResearchClaimResolution(
                    claim_id=group.group_id,
                    status=ResearchClaimResolutionStatus.REJECTED_NON_RESEARCHABLE,
                )
            else:
                origin = group.origins[0]
                source = claims_by_request[origin.request_id]
                normalized_data = json.loads(
                    (normalized_root / f"{source['claim_id']}.json").read_text(encoding="utf-8")
                )
                request = ResearchRequest(
                    requester_id=origin.requester_id,
                    stage=stage,
                    claim=source["claim"],
                )
                try:
                    packet = desk.research(
                        request,
                        normalized_claim=NormalizedClaim.model_validate(
                            normalized_data["normalized_claim"]
                        ),
                        request_id=origin.request_id,
                        defer_bundle_rebuild=True,
                    )
                except ResearchQualityControlError as exc:
                    resolution = ResearchClaimResolution(
                        claim_id=group.group_id,
                        status=ResearchClaimResolutionStatus.QC_FAILED,
                        failure_code=exc.code,
                        failure_summary=exc.summary,
                    )
                    self._record_qc_failure(round_id, group, resolution)
                else:
                    resolution = ResearchClaimResolution(
                        claim_id=group.group_id,
                        status=ResearchClaimResolutionStatus.STAGED_PACKET,
                        packet_id=packet.packet_id,
                    )
                    packet_data = packet.model_dump(mode="json")
            return group, resolution, packet_data

        first_error: Exception | None = None
        if pending_groups:
            register_tasks = getattr(self.engine.progress, "task_batch_started", None)
            if callable(register_tasks):
                register_tasks(
                    [
                        TaskProgressItem(
                            task_id=group.group_id,
                            participant_id="RESEARCH_DESK",
                            label=f"{group.group_id} · 文献调研",
                            initial_state=(
                                "failed"
                                if group.group_id in resolutions_by_id
                                and resolutions_by_id[group.group_id].status
                                == ResearchClaimResolutionStatus.QC_FAILED
                                else "completed"
                                if group.group_id in resolutions_by_id
                                else "pending"
                            ),
                            initial_detail=(
                                resolutions_by_id[group.group_id].status.value
                                if group.group_id in resolutions_by_id
                                else None
                            ),
                        )
                        for group in groups
                    ]
                )

            def resolve_tracked_group(
                group: ResearchRoundDedupGroup,
            ) -> tuple[
                ResearchRoundDedupGroup,
                ResearchClaimResolution,
                dict[str, Any] | None,
            ]:
                task_started = getattr(self.engine.progress, "task_started", None)
                task_finished = getattr(self.engine.progress, "task_finished", None)
                if callable(task_started):
                    task_started(group.group_id, "检索、筛选与证据综合")
                try:
                    result = resolve_group(group)
                except Exception as exc:
                    if callable(task_finished):
                        task_finished(
                            group.group_id,
                            failed=True,
                            detail=f"{type(exc).__name__}",
                        )
                    raise
                if callable(task_finished):
                    resolution = result[1]
                    task_finished(
                        group.group_id,
                        failed=resolution.status
                        == ResearchClaimResolutionStatus.QC_FAILED,
                        detail=resolution.status.value,
                    )
                return result

            worker_count = min(
                len(pending_groups), self.max_concurrent_claim_groups
            )
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(resolve_tracked_group, group): group
                    for group in pending_groups
                }
                for future in as_completed(futures):
                    try:
                        group, resolution, packet_data = future.result()
                    except Exception as exc:
                        if first_error is None:
                            first_error = exc
                        continue
                    relative = base / f"{group.group_id}.json"
                    self.repo.docs.write_once(
                        relative, resolution.model_dump_json(indent=2)
                    )
                    resolutions_by_id[group.group_id] = resolution
                    if resolution.packet_id and packet_data is not None:
                        packets_by_id[resolution.packet_id] = packet_data
                    completed_count += 1
                    self.engine.progress.info(
                        f"{round_id} · 已完成 {completed_count}/{len(groups)} 个去重 claim group"
                    )

        if first_error is not None:
            if isinstance(first_error, ProviderError):
                self.engine._fail(
                    "RESEARCH_ROUND_PROVIDER_FAILED", "RESEARCH_DESK", first_error
                )
            elif (
                isinstance(first_error, RepresentativeUnavailableError)
                and self.engine.status.phase != MeetingPhase.PAUSED
            ):
                self.engine._fail(
                    "RESEARCH_ROUND_PROVIDER_FAILED", "RESEARCH_DESK", first_error
                )
            raise first_error

        resolutions = [resolutions_by_id[group.group_id] for group in groups]
        packets = [
            packets_by_id[resolution.packet_id]
            for resolution in resolutions
            if resolution.packet_id is not None
        ]
        return resolutions, packets

    def _release(
        self,
        *,
        round_id: str,
        stage: ResearchStage,
        records: list[dict[str, Any]],
        groups: list[ResearchRoundDedupGroup],
        subject_files: tuple[Path, ...],
        resolutions: list[ResearchClaimResolution],
        packets: list[dict[str, Any]],
        stage_repo: MeetingRepository,
    ) -> ResearchRoundResult:
        private_root = Path("governance_private/research/rounds") / round_id
        document_ids = sorted(
            {
                str(source["archived_sha256"])
                for packet in packets
                for source in packet.get("sources", [])
                if source.get("archived_sha256")
            }
        )
        packet_ids = [
            resolution.packet_id
            for resolution in resolutions
            if resolution.status == ResearchClaimResolutionStatus.STAGED_PACKET
        ]
        qc_failed_claim_count = sum(
            resolution.status == ResearchClaimResolutionStatus.QC_FAILED
            for resolution in resolutions
        )
        release_disposition = (
            "PARTIAL_WITH_QC_EXCLUSIONS" if qc_failed_claim_count else "COMPLETE"
        )
        prepared = ResearchRoundReleaseGate(
            expected_claim_ids=[resolution.claim_id for resolution in resolutions],
            resolutions=resolutions,
            staged_document_ids=document_ids,
            released=False,
        )
        prepared_relative = private_root / "release_gate_prepared.json"
        if not (self.repo.root / prepared_relative).exists():
            self.repo.docs.write_once(prepared_relative, prepared.model_dump_json(indent=2))

        self._copy_tree_once(
            stage_repo.root / "public/research/evidence_packets",
            self.repo.root / "public/research/evidence_packets",
        )
        self._copy_tree_once(
            stage_repo.root / "public/research/literature_bundle",
            self.repo.root / "public/research/literature_bundle",
            # ``manifest.json`` and ``README.md`` are generated views of the
            # records.  They necessarily change as a resumed round adds
            # packets (and the manifest contains a generation timestamp), so
            # treating them as immutable staged artifacts makes a retry fail
            # even when all substantive records are already consistent.  The
            # public bundle is rebuilt immediately below after the immutable
            # records/documents have been reconciled.
            skip_relative_paths=_GENERATED_LITERATURE_BUNDLE_VIEWS,
        )
        self._copy_tree_once(
            stage_repo.root / "audit_private/research",
            self.repo.root / "audit_private/research",
        )
        literature_bundle_path = LiteratureBundleManager(
            repo=self.repo,
            fetcher=self.document_fetcher,
        ).rebuild_download_bundle()
        released = ResearchRoundReleaseGate(
            expected_claim_ids=prepared.expected_claim_ids,
            resolutions=resolutions,
            staged_document_ids=document_ids,
            released=True,
            published_packet_ids=packet_ids,
            published_document_ids=document_ids,
            literature_bundle_published=True,
        )
        released_relative = private_root / "release_gate_released.json"
        if not (self.repo.root / released_relative).exists():
            self.repo.docs.write_once(released_relative, released.model_dump_json(indent=2))

        snapshot_relative = (
            Path("public/research/rounds") / round_id / "evidence_snapshot.json"
        )
        snapshot = {
            "meeting_id": self.repo.meeting_id,
            "round_id": round_id,
            "stage": stage.value,
            "status": "RELEASED",
            "release_disposition": release_disposition,
            "released_at": datetime.now(timezone.utc).isoformat(),
            "representative_count": len(records),
            "max_claims_per_representative": int(
                json.loads(
                    (self.repo.root / private_root / "round_policy.json").read_text(
                        encoding="utf-8"
                    )
                )["max_claims_per_representative"]
            ),
            "subject_files": [self._subject_record(path) for path in subject_files],
            "claim_resolutions": [
                {
                    **resolution.model_dump(mode="json"),
                    "normalized_claim": next(
                        group.normalized_claim
                        for group in groups
                        if group.group_id == resolution.claim_id
                    ),
                }
                for resolution in resolutions
            ],
            "packet_ids": packet_ids,
            "qc_failed_claim_count": qc_failed_claim_count,
            "packets": packets,
            "literature_bundle_path": str(literature_bundle_path),
        }
        self.repo.docs.write_once(
            snapshot_relative,
            json.dumps(snapshot, indent=2, ensure_ascii=False),
        )
        self._ensure_release_event(snapshot, snapshot_relative)
        return ResearchRoundResult(
            round_id=round_id,
            stage=stage,
            representative_count=len(records),
            packet_count=len(packet_ids),
            qc_failed_claim_count=qc_failed_claim_count,
            release_disposition=release_disposition,
            packet_ids=packet_ids,
            evidence_snapshot_path=str(snapshot_relative),
        )

    def _staging_repo(self, round_id: str) -> MeetingRepository:
        root = (
            self.repo.root
            / "governance_private/research/rounds"
            / round_id
            / "staging_meeting"
        )
        required = (
            "public/meeting_manifest.json",
            "identity_private/meeting_manifest.json",
            "identity_private/representative_registry.json",
        )
        for relative in required:
            source = self.repo.root / relative
            destination = root / relative
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        for relative in (
            "public/research/evidence_packets",
            "public/research/cache_invalidations",
        ):
            self._copy_tree_once(self.repo.root / relative, root / relative)
        self._copy_tree_once(
            self.repo.root / "public/research/literature_bundle",
            root / "public/research/literature_bundle",
            # The manifest is a generated view of all records known to that
            # repository.  An interrupted round's staging repository can
            # legitimately contain more records than the public repository,
            # so copying the older public view back into staging on resume
            # must not be treated as an immutable-artifact conflict.  The
            # README is generated alongside the manifest; both are rebuilt at
            # the release barrier from the reconciled immutable records.
            skip_relative_paths=_GENERATED_LITERATURE_BUNDLE_VIEWS,
        )
        return MeetingRepository(root)

    def _ensure_release_event(self, snapshot: dict[str, Any], snapshot_relative: Path) -> None:
        if self.repo.events.path.exists():
            for line in self.repo.events.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if (
                    event.get("event_type") == "RESEARCH_ROUND_RELEASED"
                    and event.get("payload", {}).get("round_id") == snapshot["round_id"]
                ):
                    return
        self.repo.events.append(
            "RESEARCH_ROUND_RELEASED",
            {
                "meeting_id": self.repo.meeting_id,
                "round_id": snapshot["round_id"],
                "stage": snapshot["stage"],
                "representative_count": snapshot["representative_count"],
                "packet_ids": snapshot["packet_ids"],
                "qc_failed_claim_count": snapshot.get("qc_failed_claim_count", 0),
                "release_disposition": snapshot.get("release_disposition", "COMPLETE"),
                "evidence_snapshot_path": str(snapshot_relative),
                "literature_bundle_path": snapshot["literature_bundle_path"],
            },
            actor="RESEARCH_DESK",
        )

    @staticmethod
    def _copy_tree_once(
        source: Path,
        destination: Path,
        *,
        skip_relative_paths: frozenset[str] = frozenset(),
    ) -> None:
        if not source.exists():
            return
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if relative.as_posix() in skip_relative_paths:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            content = path.read_bytes()
            if target.exists():
                if target.read_bytes() != content:
                    raise ValueError(f"staged artifact conflicts with existing file: {target}")
                continue
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)

    @staticmethod
    def _validate_subject_files(subject_files: tuple[Path, ...]) -> None:
        if not subject_files:
            raise ValueError("Research Round requires at least one public subject file")
        for path in subject_files:
            if not path.exists() or not path.is_file():
                raise ValueError(f"Research Round subject file is unavailable: {path}")

    def _subject_record(self, path: Path) -> dict[str, str]:
        content = path.read_bytes()
        return {
            "path": str(path.relative_to(self.repo.root)),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    @staticmethod
    def _result_from_snapshot(path: Path) -> ResearchRoundResult:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        return ResearchRoundResult(
            round_id=snapshot["round_id"],
            stage=ResearchStage(snapshot["stage"]),
            representative_count=snapshot["representative_count"],
            packet_count=len(snapshot["packet_ids"]),
            qc_failed_claim_count=int(snapshot.get("qc_failed_claim_count", 0)),
            release_disposition=str(snapshot.get("release_disposition", "COMPLETE")),
            packet_ids=snapshot["packet_ids"],
            evidence_snapshot_path=str(path.relative_to(path.parents[4])),
        )
