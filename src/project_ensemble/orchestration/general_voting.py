from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from project_ensemble.domain import DecisionRigor, MeetingPhase, Persona
from project_ensemble.governance_private.thresholds import (
    high_threshold,
    high_threshold_formula,
    meeting_decision_rigor,
    strict_majority_threshold,
)
from project_ensemble.orchestration.amendments import AmendmentSubmission, AmendmentType
from project_ensemble.orchestration.ballot import SealedBallot
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.storage.meeting import MeetingRepository


@dataclass(frozen=True)
class BallotRecordInspection:
    records: dict[str, dict]
    record_paths: dict[str, Path]
    invalid_records: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class BallotAttempt:
    number: int
    votes_relative: Path
    records: dict[str, dict]


def _select_ballot_attempt(
    *,
    repo: MeetingRepository,
    engine: MeetingEngine,
    ballot_id: str,
    private_root: Path,
    eligible: set[str],
    load_records: Callable[[Path, Path], BallotRecordInspection],
) -> BallotAttempt:
    """Have Chair retain complete sealed votes and re-collect invalid/missing votes."""
    votes_relative = private_root / "votes"
    recovered_relative = private_root / "recovered_votes"
    inspection = load_records(repo.root / votes_relative, repo.root / recovered_relative)
    if inspection.records or inspection.invalid_records:
        review_root = private_root / "recovery_reviews"
        existing_reviews = sorted((repo.root / review_root).glob("review_*.json"))
        review_number = len(existing_reviews) + 1
        review_relative = review_root / f"review_{review_number:03d}.json"
        missing = sorted(eligible - set(inspection.records))
        repo.docs.write_once(
            review_relative,
            json.dumps(
                {
                    "ballot_id": ballot_id,
                    "review_number": review_number,
                    "review_actor": "CHAIR",
                    "integrity_criteria": [
                        "valid JSON object with the exact ballot schema",
                        "ballot_id matches the open ballot",
                        "representative_id is eligible and matches its record path",
                        "choice is a legal ballot option",
                        "any option-specific required reason is present",
                    ],
                    "retained_record_paths": [
                        str(inspection.record_paths[representative_id])
                        for representative_id in sorted(inspection.records)
                    ],
                    "invalid_records": list(inspection.invalid_records),
                    "representatives_to_recollect": missing,
                    "partial_tally_disclosed": False,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        repo.events.append(
            "CHAIR_BALLOT_RECOVERY_REVIEWED",
            {
                "meeting_id": repo.meeting_id,
                "ballot_id": ballot_id,
                "review_number": review_number,
                "record_path": str(review_relative),
            },
            actor="CHAIR",
        )
        engine.progress.info(
            f"{ballot_id} · Chair 已完成密封票完整性检查；完整票保留，其余代表重新投票"
        )
    return BallotAttempt(
        number=1,
        votes_relative=votes_relative,
        records=inspection.records,
    )


def _next_vote_record_path(
    *,
    repo: MeetingRepository,
    private_root: Path,
    votes_relative: Path,
    representative_id: str,
) -> Path:
    primary = votes_relative / f"{representative_id}.json"
    if not (repo.root / primary).exists():
        return primary
    recovery_root = private_root / "recovered_votes" / representative_id
    sequence = 1
    while True:
        candidate = recovery_root / f"vote_{sequence:03d}.json"
        if not (repo.root / candidate).exists():
            return candidate
        sequence += 1


class ChairAmendmentDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    amendment_id: str
    final_type: AmendmentType
    final_impact_scope: list[str] = Field(min_length=1)
    status: Literal["READY", "REBASE_REQUIRED", "SUPERSEDED"]
    conflicts_with: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    procedural_effect: str = Field(min_length=1)

    @field_validator("conflicts_with", "dependencies")
    @classmethod
    def references_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Chair references must be unique")
        return value


class ChairWindowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dispositions: list[ChairAmendmentDisposition]


class BallotAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["AMENDMENT", "STATUS_QUO"]


class ReasonStatementAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["PASS", "STATE"]
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def reason_cannot_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("reason cannot be blank")
        return value

    def model_post_init(self, __context) -> None:
        if self.action == "PASS" and self.reason is not None:
            raise ValueError("PASS must not include a reason")
        if self.action == "STATE" and self.reason is None:
            raise ValueError("STATE requires a reason")


class ChairDraftChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["ADD", "REPLACE", "DELETE", "MOVE", "RENUMBER"]
    target: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ChairDraftApplication(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["APPLY_ADOPTED_AMENDMENT"]
    amendment_id: str
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_text: str = Field(min_length=1)
    changes: list[ChairDraftChange] = Field(min_length=1)
    reason: str = Field(min_length=1)


class ConflictFragment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    fragment_id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,63}$")
    source_amendment_ids: list[str] = Field(min_length=1)
    text: str = Field(min_length=1)
    impact_scope: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("source_amendment_ids", "impact_scope")
    @classmethod
    def fragment_lists_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("conflict-fragment lists must not contain duplicates")
        return value


class ConflictChoiceSet(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    choice_set_id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,63}$")
    question: str = Field(min_length=1)
    options: list[ConflictFragment] = Field(min_length=2)

    @field_validator("options")
    @classmethod
    def option_ids_are_unique(cls, value: list[ConflictFragment]) -> list[ConflictFragment]:
        ids = [item.fragment_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("choice-set option fragment IDs must be unique")
        return value


class ChairConflictDecomposition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    affected_amendment_ids: list[str] = Field(min_length=2)
    relationship: Literal["NO_CONFLICT", "PARTIAL_CONFLICT", "FULL_CONFLICT"]
    compatible_fragments: list[ConflictFragment] = Field(default_factory=list)
    choice_sets: list[ConflictChoiceSet] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class OptionBallotAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    choice: str = Field(min_length=1)


@dataclass(frozen=True)
class ConflictGroupResult:
    draft_path: Path
    ballot_count: int
    adopted_count: int
    rejected_count: int


class GeneralVotingResult(BaseModel):
    ballot_count: int
    adopted_amendment_count: int
    rejected_amendment_count: int
    deferred_amendment_count: int
    draft_path: str
    next_phase: MeetingPhase
    paused_reason: str | None = None


class RatificationAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    choice: Literal["YES", "NO"]
    opposition_reason: str | None = None

    @field_validator("opposition_reason")
    @classmethod
    def opposition_reason_cannot_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("opposition reason cannot be blank")
        return value

    def model_post_init(self, __context) -> None:
        if self.choice == "YES" and self.opposition_reason is not None:
            raise ValueError("YES must not include an opposition reason")
        if self.choice == "NO" and self.opposition_reason is None:
            raise ValueError("NO requires an opposition reason")


class GeneralRatificationResult(BaseModel):
    passed: bool
    yes_votes: int
    no_votes: int
    required_yes_votes: int
    next_phase: MeetingPhase
    paused_reason: str | None = None


class GeneralRatificationRunner:
    """Final sealed D3 ratification followed by status transition or Human review."""

    BALLOT_ID = "B-GENERAL-D3-RATIFICATION"

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.max_output_tokens = max_output_tokens
        self.resolver = GovernanceDocumentResolver(governance_docs)
        self.assembler = RepresentativeContextAssembler()

    def run(self, *, draft_path: Path) -> GeneralRatificationResult:
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        eligible = {record["representative_id"] for record in registry}
        threshold = high_threshold(self.repo, len(eligible))
        private_root = Path("governance_private/general_principle/ratification")
        frozen_path = self.repo.root / private_root / "frozen_ballot.json"
        public_relative = Path("public/general_principle/ratification.json")
        public_path = self.repo.root / public_relative

        self.engine.status.phase = MeetingPhase.GENERAL_RATIFICATION
        self.engine.progress.status(
            MeetingPhase.GENERAL_RATIFICATION,
            f"D3 最终批准密封 ballot；YES 通过门槛为 {threshold}/{len(eligible)}",
        )
        if frozen_path.exists():
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
            if not public_path.exists():
                self._write_public_result(public_relative, frozen)
        else:
            question_relative = Path("public/general_principle/ratification_question.json")
            question_path = self.repo.root / question_relative
            if not question_path.exists():
                self.repo.docs.write_once(
                    question_relative,
                    json.dumps(
                        {
                            "ballot_id": self.BALLOT_ID,
                            "draft": "D3",
                            "draft_path": str(draft_path.relative_to(self.repo.root)),
                            "options": ["YES", "NO"],
                            "yes_threshold": {
                                "formula": high_threshold_formula(self.repo, active_symbol="N"),
                                "eligible_count": len(eligible),
                                "required_votes": threshold,
                            },
                            "no_requires_opposition_reason": True,
                        },
                        indent=2,
                    ),
                )
            attempt = _select_ballot_attempt(
                repo=self.repo,
                engine=self.engine,
                ballot_id=self.BALLOT_ID,
                private_root=private_root,
                eligible=eligible,
                load_records=lambda votes_path, recovered_path: self._load_vote_records(
                    votes_path,
                    recovered_path,
                    eligible=eligible,
                ),
            )
            ballot = SealedBallot(
                ballot_id=self.BALLOT_ID,
                eligible_representatives=eligible,
                options=("YES", "NO"),
            )
            submissions_by_id = dict(attempt.records)
            missing_records = [
                record
                for record in registry
                if record["representative_id"] not in attempt.records
            ]

            def collect_vote(record: dict) -> tuple[str, RatificationAction, dict, Path]:
                representative_id = record["representative_id"]
                persona = Persona(record["runtime"]["persona"])
                spec = self.resolver.representative_context_spec(
                    persona=persona,
                    stage="ballot",
                    representative_id=representative_id,
                    public_state_files=(
                        self.repo.root / "public/task.json",
                        draft_path,
                        question_path,
                    ),
                )
                response = self.engine.invoke_participant(
                    representative_id,
                    system_text=self.assembler.assemble(spec),
                    user_text=(
                        'Return exactly {"choice":"YES"} to ratify, or '
                        '{"choice":"NO","opposition_reason":"..."}. '
                        "A NO vote must give a substantive reason. Do not use Markdown or add keys."
                    ),
                    stage="general_ratification",
                    max_output_tokens=self.max_output_tokens,
                )
                try:
                    action = RatificationAction.model_validate(json.loads(response.text))
                except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                    self.engine.pause_for_unconfigured_policy(
                        participant_id=representative_id,
                        reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                    )
                submission = {
                    "ballot_id": self.BALLOT_ID,
                    "representative_id": representative_id,
                    **action.model_dump(mode="json"),
                }
                vote_relative = _next_vote_record_path(
                    repo=self.repo,
                    private_root=private_root,
                    votes_relative=attempt.votes_relative,
                    representative_id=representative_id,
                )
                return representative_id, action, submission, vote_relative

            def persist_vote(result: tuple[str, RatificationAction, dict, Path]) -> None:
                representative_id, _action, submission, vote_relative = result
                self.repo.docs.write_once(
                    vote_relative,
                    json.dumps(submission, indent=2, ensure_ascii=False),
                )
                self.repo.events.append(
                    "SEALED_BALLOT_SUBMITTED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "ballot_id": self.BALLOT_ID,
                        "representative_id": representative_id,
                        "attempt_number": attempt.number,
                        "record_path": str(vote_relative),
                    },
                    actor=representative_id,
                )

            completed = run_bounded_representative_lanes(
                missing_records,
                collect_vote,
                getattr(
                    self.engine,
                    "model_concurrency_limit",
                    lambda _provider_id, _model_id: 1,
                ),
                on_result=persist_vote,
                progress=self.engine.progress,
                progress_records=registry,
                completed_participant_ids=submissions_by_id,
            )
            for representative_id, _action, submission, _vote_relative in completed:
                submissions_by_id[representative_id] = submission
            submissions = []
            for record in registry:
                submission = submissions_by_id[record["representative_id"]]
                ballot.submit(record["representative_id"], submission["choice"])
                submissions.append(submission)
            ballot.close()
            tally = ballot.tally()
            frozen = {
                **ballot.sealed_export(),
                "kind": "general_ratification",
                "attempt_number": attempt.number,
                "eligible_count": len(eligible),
                "required_yes_votes": threshold,
                "tally": tally,
                "opposition_reasons": [
                    {
                        "representative_id": item["representative_id"],
                        "reason": item["opposition_reason"],
                    }
                    for item in submissions
                    if item["choice"] == "NO"
                ],
            }
            self.repo.docs.write_once(private_root / "frozen_ballot.json", json.dumps(frozen, indent=2, ensure_ascii=False))
            self.repo.events.append(
                "SEALED_BALLOT_CLOSED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "ballot_id": self.BALLOT_ID,
                    "attempt_number": attempt.number,
                    "submitted_count": len(eligible),
                },
                actor="orchestrator",
            )
            self._write_public_result(public_relative, frozen)

        passed = frozen["tally"]["YES"] >= frozen["required_yes_votes"]
        if passed:
            reason_code = None
            next_phase = MeetingPhase.STATUS_TRANSITION
            event_type = "GENERAL_PRINCIPLE_RATIFIED"
        else:
            reason_code = "GENERAL_RATIFICATION_FAILED"
            next_phase = MeetingPhase.PAUSED
            event_type = "GENERAL_PRINCIPLE_RATIFICATION_FAILED"
        marker_relative = Path("governance_private/general_principle") / f"{event_type.lower()}.json"
        marker_path = self.repo.root / marker_relative
        if not marker_path.exists():
            self.repo.docs.write_once(
                marker_relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "ballot_id": self.BALLOT_ID,
                        "passed": passed,
                        "tally": frozen["tally"],
                        "required_yes_votes": frozen["required_yes_votes"],
                        "next_phase": next_phase.value,
                        "next_reason_code": reason_code,
                    },
                    indent=2,
                ),
            )
            event = self.repo.events.append(
                event_type,
                {
                    "meeting_id": self.repo.meeting_id,
                    "ballot_id": self.BALLOT_ID,
                    "tally": frozen["tally"],
                    "required_yes_votes": frozen["required_yes_votes"],
                    "record_path": str(marker_relative),
                },
                actor="orchestrator",
            )
            if not passed:
                self.engine.escalation.request(
                    reason_code=reason_code,
                    summary="D3 did not reach the final ratification threshold; Human direction is required.",
                    related_event_hash=event["event_hash"],
                )
        self.engine.status.phase = next_phase
        self.engine.status.paused_reason = reason_code
        self.engine.progress.status(
            next_phase,
            (
                f"总则批准完成 · final YES={frozen['tally']['YES']}/{len(eligible)}；"
                f"批准门槛={threshold}/{len(eligible)}；进入试行 atomic-item 状态转换"
                if passed
                else f"{reason_code} · final YES={frozen['tally']['YES']}/{len(eligible)}；"
                f"批准门槛={threshold}/{len(eligible)}"
            ),
        )
        return GeneralRatificationResult(
            passed=passed,
            yes_votes=frozen["tally"]["YES"],
            no_votes=frozen["tally"]["NO"],
            required_yes_votes=frozen["required_yes_votes"],
            next_phase=next_phase,
            paused_reason=reason_code,
        )

    def _load_vote_records(
        self,
        votes_path: Path,
        recovered_path: Path,
        *,
        eligible: set[str],
    ) -> BallotRecordInspection:
        records: dict[str, dict] = {}
        record_paths: dict[str, Path] = {}
        invalid_records: list[dict[str, str]] = []
        paths = list(votes_path.glob("*.json")) if votes_path.exists() else []
        if recovered_path.exists():
            paths.extend(recovered_path.glob("*/*.json"))
        for path in sorted(paths, key=lambda item: (item.parent != votes_path, str(item))):
            path_relative = path.relative_to(self.repo.root)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if set(raw) != {"ballot_id", "representative_id", "choice", "opposition_reason"}:
                    raise ValueError("record does not have the exact ratification schema")
                representative_id = raw["representative_id"]
                if raw["ballot_id"] != self.BALLOT_ID:
                    raise ValueError("ballot ID mismatch")
                if representative_id not in eligible:
                    raise ValueError("ineligible representative")
                if path.parent == votes_path and path.stem != representative_id:
                    raise ValueError("representative ID does not match record path")
                if path.parent != votes_path and path.parent.name != representative_id:
                    raise ValueError("representative ID does not match recovery record path")
                action = RatificationAction.model_validate(
                    {
                        "choice": raw["choice"],
                        "opposition_reason": raw.get("opposition_reason"),
                    }
                )
                if representative_id in records:
                    raise ValueError("multiple complete votes exist for one representative")
            except (KeyError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                invalid_records.append({"record_path": str(path_relative), "reason": str(exc)})
                continue
            records[representative_id] = {
                "ballot_id": self.BALLOT_ID,
                "representative_id": representative_id,
                **action.model_dump(mode="json"),
            }
            record_paths[representative_id] = path_relative
        return BallotRecordInspection(
            records=records,
            record_paths=record_paths,
            invalid_records=tuple(invalid_records),
        )

    def _write_public_result(self, relative: Path, frozen: dict) -> None:
        self.repo.docs.write_once(
            relative,
            json.dumps(
                {
                    "ballot_id": self.BALLOT_ID,
                    "status": "CLOSED",
                    "eligible_count": frozen["eligible_count"],
                    "required_yes_votes": frozen["required_yes_votes"],
                    "tally": frozen["tally"],
                    "passed": frozen["tally"]["YES"] >= frozen["required_yes_votes"],
                    "opposition_reasons": frozen["opposition_reasons"],
                },
                indent=2,
                ensure_ascii=False,
            ),
        )


class GeneralVotingRunner:
    """Chair processing and the confirmed binary sequential-amendment path.

    Each ballot is a sealed window. After an interruption, Chair mechanically
    validates stored vote records, retains complete votes, and re-collects only
    invalid or missing votes without disclosing a partial tally.
    """

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.governance_docs = Path(governance_docs)
        self.max_output_tokens = max_output_tokens
        self.resolver = GovernanceDocumentResolver(self.governance_docs)
        self.assembler = RepresentativeContextAssembler()

    def run(
        self,
        *,
        draft_path: Path,
        docket_path: Path,
        window_number: int = 1,
        target_draft_name: str = "D1",
    ) -> GeneralVotingResult:
        if window_number <= 0:
            raise ValueError("window_number must be positive")
        if target_draft_name not in {"D1", "D2", "D3"}:
            raise ValueError("target_draft_name must be D1, D2, or D3")
        self.window_number = window_number
        self.window_code = f"W{window_number:03d}"
        self.target_draft_name = target_draft_name
        if window_number == 1:
            legacy_boundary = self.repo.root / "governance_private/general_principle/chair_processing_boundary.json"
            resume_relative = Path("governance_private/general_principle/chair_processing_implemented_resume.json")
            if legacy_boundary.exists() and not (self.repo.root / resume_relative).exists():
                self.repo.docs.write_once(
                    resume_relative,
                    json.dumps(
                        {
                            "meeting_id": self.repo.meeting_id,
                            "superseded_boundary": str(legacy_boundary.relative_to(self.repo.root)),
                            "resume_stage": "CHAIR_AMENDMENT_PROCESSING",
                        },
                        indent=2,
                    ),
                )
                self.repo.events.append(
                    "MEETING_RESUMED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "prior_reason_code": "CHAIR_DOCUMENT_PROCESSING_NOT_IMPLEMENTED",
                        "record_path": str(resume_relative),
                    },
                    actor="orchestrator",
                )
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        docket = json.loads(docket_path.read_text(encoding="utf-8"))
        amendments = {
            item["amendment_id"]: AmendmentSubmission.model_validate(item)
            for item in docket["amendments"]
        }
        order = list(docket["random_order"]["order"])
        if set(order) != set(amendments) or len(order) != len(amendments):
            raise ValueError("frozen amendment order does not exactly match the docket")

        self.engine.status.phase = MeetingPhase.BALLOT
        self.engine.progress.status(
            MeetingPhase.BALLOT,
            "Chair 将按冻结顺序更新冲突/依赖状态，并逐项开启密封表决",
        )
        current_draft = draft_path
        ballot_count = adopted = rejected = deferred = 0

        for index, amendment_id in enumerate(order, start=1):
            decision_path = self._decision_path(index, amendment_id)
            if decision_path.exists():
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
                current_draft = self.repo.root / decision["resulting_draft_path"]
                ballot_count += int(decision["ballot_count"])
                adopted += int(decision["outcome"] in {"ADOPTED", "PARTIALLY_ADOPTED"})
                rejected += int(decision["outcome"] == "REJECTED")
                deferred += int(decision["outcome"] in {"REBASE_REQUIRED", "SUPERSEDED"})
                self.engine.progress.info(f"已恢复修正案 {index}/{len(order)} 的冻结决定")
                continue

            remaining = order[index - 1 :]
            plan = self._ensure_chair_plan(
                step=index - 1,
                current_draft=current_draft,
                remaining_ids=remaining,
                amendments=amendments,
            )
            disposition = next(x for x in plan.dispositions if x.amendment_id == amendment_id)
            unresolved_conflicts = sorted(set(disposition.conflicts_with) & set(remaining[1:]))
            if unresolved_conflicts:
                group_ids = [amendment_id, *unresolved_conflicts]
                decomposition = self._ensure_conflict_decomposition(
                    index=index,
                    current_draft=current_draft,
                    amendment_ids=group_ids,
                    amendments=amendments,
                )
                if decomposition.relationship != "NO_CONFLICT":
                    group_result = self._process_conflict_group(
                        index=index,
                        group_ids=group_ids,
                        decomposition=decomposition,
                        current_draft=current_draft,
                        registry=registry,
                        amendments=amendments,
                        order=order,
                        docket_seed=str(docket["random_order"]["seed"]),
                        plan=plan,
                    )
                    current_draft = group_result.draft_path
                    ballot_count += group_result.ballot_count
                    adopted += group_result.adopted_count
                    rejected += group_result.rejected_count
                    self.engine.progress.info(
                        f"冲突集合 {', '.join(group_ids)} 已按 multi-option 优先规则处理"
                    )
                    continue
                self.engine.progress.info(
                    f"Chair 自动复核认定 {amendment_id} 与 {unresolved_conflicts} 可兼容；继续随机顺序二元表决"
                )

            if disposition.status != "READY":
                outcome = disposition.status
                deferred += 1
                self._freeze_decision(
                    index=index,
                    amendment_id=amendment_id,
                    outcome=outcome,
                    source="CHAIR_PROCEDURAL_DISPOSITION",
                    ballot_count=0,
                    resulting_draft=current_draft,
                    chair_disposition=disposition,
                )
                self.engine.progress.info(f"{amendment_id} · {outcome} · 未开启 ballot")
                continue

            first_ballot = self._collect_ballot(
                index=index,
                amendment=amendments[amendment_id],
                current_draft=current_draft,
                registry=registry,
                kind="supermajority",
                additional_public_files=(),
            )
            ballot_count += 1
            tally = first_ballot["tally"]
            high_required = high_threshold(self.repo, len(registry))
            source_prefix = (
                "MAJORITY" if meeting_decision_rigor(self.repo) == DecisionRigor.RELAXED
                else "SUPERMAJORITY"
            )
            if tally["AMENDMENT"] >= high_required:
                outcome, source = "ADOPTED", f"{source_prefix}_PASS"
            elif tally["STATUS_QUO"] >= high_required:
                outcome, source = "REJECTED", f"{source_prefix}_REJECT"
            else:
                reasons_path = self._collect_reasons(
                    index=index,
                    amendment=amendments[amendment_id],
                    current_draft=current_draft,
                    registry=registry,
                    first_ballot=first_ballot,
                )
                protective = self._collect_ballot(
                    index=index,
                    amendment=amendments[amendment_id],
                    current_draft=current_draft,
                    registry=registry,
                    kind="protective",
                    additional_public_files=(reasons_path,),
                )
                ballot_count += 1
                protective_tally = protective["tally"]
                majority = strict_majority_threshold(len(registry))
                if protective_tally["AMENDMENT"] >= majority:
                    outcome, source = "ADOPTED", "PROTECTIVE_PASS"
                else:
                    # A binary tie has a status quo, so the confirmed tie rule
                    # preserves current text.
                    outcome, source = "REJECTED", "PROTECTIVE_REJECT"

            if outcome == "ADOPTED":
                current_draft = self._apply_adopted_amendment(
                    index=index,
                    amendment=amendments[amendment_id],
                    current_draft=current_draft,
                )
                adopted += 1
            else:
                rejected += 1
            self._freeze_decision(
                index=index,
                amendment_id=amendment_id,
                outcome=outcome,
                source=source,
                ballot_count=2 if source.startswith("PROTECTIVE") else 1,
                resulting_draft=current_draft,
                chair_disposition=disposition,
            )
            self.engine.progress.info(f"{amendment_id} · {outcome} · {source}")

        target_path = self._freeze_target_draft(current_draft)
        self.engine.status.phase = MeetingPhase.AMENDMENT_SUBMISSION
        self.engine.progress.status(
            MeetingPhase.AMENDMENT_SUBMISSION,
            f"第 {self.window_number} 轮顺序表决完成并冻结 {self.target_draft_name}",
        )
        return GeneralVotingResult(
            ballot_count=ballot_count,
            adopted_amendment_count=adopted,
            rejected_amendment_count=rejected,
            deferred_amendment_count=deferred,
            draft_path=str(target_path.relative_to(self.repo.root)),
            next_phase=MeetingPhase.AMENDMENT_SUBMISSION,
        )

    def _chair_system_text(self) -> str:
        files = (
            self.governance_docs / "03_roles/chair/chair_role.md",
            self.governance_docs / "07_runtime_memory/other_participants/deliberation_chair.md",
            self.governance_docs / "02_deliberation/deliberation_protocol.md",
        )
        return "\n\n".join(path.read_text(encoding="utf-8") for path in files)

    def _ensure_chair_plan(
        self,
        *,
        step: int,
        current_draft: Path,
        remaining_ids: list[str],
        amendments: dict[str, AmendmentSubmission],
    ) -> ChairWindowPlan:
        relative = (
            Path("public/general_principle/chair_plans")
            / f"window_{self.window_number:03d}"
            / f"step_{step:03d}.json"
        )
        absolute = self.repo.root / relative
        if absolute.exists():
            return ChairWindowPlan.model_validate_json(absolute.read_text(encoding="utf-8"))
        payload = [amendments[item_id].model_dump(mode="json") for item_id in remaining_ids]
        response = self.engine.invoke_participant(
            "CHAIR",
            system_text=self._chair_system_text(),
            user_text=(
                "Perform only procedural document processing for the current sequential-amendment step. "
                "Do not choose an amendment on substantive merit and do not rewrite draft text. "
                "Return exactly one JSON object with key dispositions. Give exactly one disposition for "
                "each remaining amendment ID. Each disposition must contain amendment_id, final_type "
                "(OBJECTION or SUPPLEMENTARY), final_impact_scope (non-empty string list), status "
                "(READY, REBASE_REQUIRED, or SUPERSEDED), conflicts_with (ID list), dependencies "
                "(ID list), reason, and procedural_effect. References must use only the supplied IDs. "
                "Do not use Markdown.\n\nCURRENT DRAFT:\n"
                + current_draft.read_text(encoding="utf-8")
                + "\n\nREMAINING AMENDMENTS:\n"
                + json.dumps(payload, indent=2, ensure_ascii=False)
            ),
            stage="chair_amendment_processing",
            max_output_tokens=self.max_output_tokens,
        )
        try:
            plan = ChairWindowPlan.model_validate(json.loads(response.text))
            self._validate_plan(plan, remaining_ids, set(amendments))
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            self.engine.pause_for_unconfigured_policy(
                participant_id="CHAIR",
                reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
            )
        self.repo.docs.write_once(relative, plan.model_dump_json(indent=2))
        self.repo.events.append(
            "CHAIR_AMENDMENT_PLAN_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "step": step,
                "remaining_amendment_ids": remaining_ids,
                "record_path": str(relative),
            },
            actor="CHAIR",
        )
        self.engine.progress.speech("CHAIR", f"程序性修正案状态 · step {step}", plan.model_dump_json(indent=2))
        return plan

    @staticmethod
    def _validate_plan(plan: ChairWindowPlan, remaining_ids: list[str], all_ids: set[str]) -> None:
        actual = [item.amendment_id for item in plan.dispositions]
        if len(actual) != len(set(actual)) or set(actual) != set(remaining_ids):
            raise ValueError("Chair plan must contain each remaining amendment exactly once")
        for item in plan.dispositions:
            references = set(item.conflicts_with) | set(item.dependencies)
            if item.amendment_id in references or not references <= all_ids:
                raise ValueError("Chair plan contains an invalid amendment reference")

    def _ensure_conflict_decomposition(
        self,
        *,
        index: int,
        current_draft: Path,
        amendment_ids: list[str],
        amendments: dict[str, AmendmentSubmission],
    ) -> ChairConflictDecomposition:
        relative = (
            Path("public/general_principle/conflict_decompositions")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}.json"
        )
        absolute = self.repo.root / relative
        if absolute.exists():
            return ChairConflictDecomposition.model_validate_json(absolute.read_text(encoding="utf-8"))
        payload = [amendments[item_id].model_dump(mode="json") for item_id in amendment_ids]
        response = self.engine.invoke_participant(
            "CHAIR",
            system_text=self._chair_system_text(),
            user_text=(
                "Apply the confirmed conflict-set priority rule as a procedural document task. Recheck whether "
                "the supplied amendments are genuinely incompatible. If they are compatible, return relationship "
                "NO_CONFLICT with empty compatible_fragments and choice_sets. Otherwise split their exact normative "
                "content into compatible_fragments that may all coexist and one or more choice_sets whose options "
                "answer the same question but cannot coexist. Use relationship PARTIAL_CONFLICT when compatible "
                "content remains, otherwise FULL_CONFLICT. Do not choose among options or add new substance. "
                "Return exactly one JSON object with affected_amendment_ids, relationship, compatible_fragments, "
                "choice_sets, and reason. Every fragment has fragment_id (uppercase letters/digits/_/-), "
                "source_amendment_ids, directly applicable text, impact_scope, and reason. Every choice set has "
                "choice_set_id, question, and at least two option fragments. Reference every supplied amendment in "
                "at least one fragment and no other amendment IDs. Do not add STATUS_QUO; the orchestrator adds it. "
                "Do not use Markdown.\n\nCURRENT DRAFT:\n"
                + current_draft.read_text(encoding="utf-8")
                + "\n\nAMENDMENTS:\n"
                + json.dumps(payload, indent=2, ensure_ascii=False)
            ),
            stage="chair_conflict_decomposition",
            max_output_tokens=self.max_output_tokens,
        )
        try:
            decomposition = ChairConflictDecomposition.model_validate(json.loads(response.text))
            self._validate_conflict_decomposition(decomposition, set(amendment_ids))
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            self.engine.pause_for_unconfigured_policy(
                participant_id="CHAIR",
                reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
            )
        self.repo.docs.write_once(relative, decomposition.model_dump_json(indent=2))
        self.repo.events.append(
            "CHAIR_CONFLICT_DECOMPOSITION_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "window_id": f"general-principle-{self.window_number:03d}",
                "step": index,
                "relationship": decomposition.relationship,
                "affected_amendment_ids": amendment_ids,
                "record_path": str(relative),
            },
            actor="CHAIR",
        )
        self.engine.progress.speech(
            "CHAIR",
            f"冲突拆分 · step {index}",
            decomposition.model_dump_json(indent=2),
        )
        return decomposition

    @staticmethod
    def _validate_conflict_decomposition(
        decomposition: ChairConflictDecomposition,
        expected_ids: set[str],
    ) -> None:
        if set(decomposition.affected_amendment_ids) != expected_ids or len(
            decomposition.affected_amendment_ids
        ) != len(expected_ids):
            raise ValueError("conflict decomposition must list every affected amendment exactly once")
        fragments = list(decomposition.compatible_fragments)
        fragments.extend(option for choice_set in decomposition.choice_sets for option in choice_set.options)
        if decomposition.relationship == "NO_CONFLICT":
            if fragments or decomposition.choice_sets:
                raise ValueError("NO_CONFLICT must not contain fragments or choice sets")
            return
        if not decomposition.choice_sets:
            raise ValueError("a conflict decomposition requires at least one exclusive choice set")
        if decomposition.relationship == "PARTIAL_CONFLICT" and not decomposition.compatible_fragments:
            raise ValueError("PARTIAL_CONFLICT requires at least one compatible fragment")
        if decomposition.relationship == "FULL_CONFLICT" and decomposition.compatible_fragments:
            raise ValueError("FULL_CONFLICT cannot contain compatible fragments")
        choice_set_ids = [item.choice_set_id for item in decomposition.choice_sets]
        if len(choice_set_ids) != len(set(choice_set_ids)):
            raise ValueError("choice set IDs must be unique across the decomposition")
        fragment_ids = [item.fragment_id for item in fragments]
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ValueError("fragment IDs must be unique across the decomposition")
        referenced: set[str] = set()
        for fragment in fragments:
            sources = set(fragment.source_amendment_ids)
            if not sources <= expected_ids:
                raise ValueError("fragment references an amendment outside the conflict group")
            referenced |= sources
        if referenced != expected_ids:
            raise ValueError("every affected amendment must have provenance in at least one fragment")
        for choice_set in decomposition.choice_sets:
            per_source: set[str] = set()
            for option in choice_set.options:
                overlap = per_source & set(option.source_amendment_ids)
                if overlap:
                    raise ValueError("one amendment cannot supply multiple options in the same choice set")
                per_source |= set(option.source_amendment_ids)

    def _process_conflict_group(
        self,
        *,
        index: int,
        group_ids: list[str],
        decomposition: ChairConflictDecomposition,
        current_draft: Path,
        registry: list[dict],
        amendments: dict[str, AmendmentSubmission],
        order: list[str],
        docket_seed: str,
        plan: ChairWindowPlan,
    ) -> ConflictGroupResult:
        component_state = {
            amendment_id: {"total": 0, "adopted": 0, "components": []}
            for amendment_id in group_ids
        }
        ballot_count = 0
        adopted_compatible: list[ConflictFragment] = []
        ordered_fragments = sorted(
            decomposition.compatible_fragments,
            key=lambda item: min(order.index(source) for source in item.source_amendment_ids),
        )
        for ordinal, fragment in enumerate(ordered_fragments, start=1):
            adopted_fragment, source, used_ballots = self._vote_compatible_fragment(
                index=index * 100 + ordinal,
                fragment=fragment,
                current_draft=current_draft,
                registry=registry,
            )
            ballot_count += used_ballots
            self._record_fragment_outcome(component_state, fragment, adopted_fragment, source)
            if adopted_fragment:
                adopted_compatible.append(fragment)
        if adopted_compatible:
            current_draft = self._apply_fragment_bundle(
                index=index * 1000 + 1,
                bundle_id=f"CG-{self.window_code}-S{index:03d}-COMPAT",
                fragments=adopted_compatible,
                current_draft=current_draft,
            )

        adopted_exclusive: list[ConflictFragment] = []
        for choice_set in decomposition.choice_sets:
            winner, used_ballots = self._run_exclusive_choice_set(
                index=index,
                choice_set=choice_set,
                current_draft=current_draft,
                registry=registry,
                docket_seed=docket_seed,
            )
            ballot_count += used_ballots
            for option in choice_set.options:
                adopted_option = winner == f"OPTION_{option.fragment_id}"
                self._record_fragment_outcome(
                    component_state,
                    option,
                    adopted_option,
                    f"CHOICE_SET:{choice_set.choice_set_id}:{winner}",
                )
                if adopted_option:
                    adopted_exclusive.append(option)
        if adopted_exclusive:
            current_draft = self._apply_fragment_bundle(
                index=index * 1000 + 2,
                bundle_id=f"CG-{self.window_code}-S{index:03d}-EXCLUSIVE",
                fragments=adopted_exclusive,
                current_draft=current_draft,
            )

        current_item_adopted = current_item_rejected = 0
        for position, amendment_id in enumerate(group_ids):
            state = component_state[amendment_id]
            if state["adopted"] == state["total"]:
                outcome = "ADOPTED"
            elif state["adopted"]:
                outcome = "PARTIALLY_ADOPTED"
            else:
                outcome = "REJECTED"
            if position == 0:
                current_item_adopted = int(outcome in {"ADOPTED", "PARTIALLY_ADOPTED"})
                current_item_rejected = int(outcome == "REJECTED")
            original_index = order.index(amendment_id) + 1
            disposition = next(item for item in plan.dispositions if item.amendment_id == amendment_id)
            self._freeze_decision(
                index=original_index,
                amendment_id=amendment_id,
                outcome=outcome,
                source=f"CONFLICT_DECOMPOSITION:{self.window_code}:STEP{index:03d}",
                ballot_count=ballot_count if position == 0 else 0,
                resulting_draft=current_draft,
                chair_disposition=disposition,
                component_outcomes=state["components"],
            )
        return ConflictGroupResult(
            draft_path=current_draft,
            ballot_count=ballot_count,
            adopted_count=current_item_adopted,
            rejected_count=current_item_rejected,
        )

    @staticmethod
    def _record_fragment_outcome(
        component_state: dict[str, dict],
        fragment: ConflictFragment,
        adopted: bool,
        source: str,
    ) -> None:
        for amendment_id in fragment.source_amendment_ids:
            state = component_state[amendment_id]
            state["total"] += 1
            state["adopted"] += int(adopted)
            state["components"].append(
                {"fragment_id": fragment.fragment_id, "adopted": adopted, "source": source}
            )

    def _vote_compatible_fragment(
        self,
        *,
        index: int,
        fragment: ConflictFragment,
        current_draft: Path,
        registry: list[dict],
    ) -> tuple[bool, str, int]:
        amendment = self._fragment_as_amendment(fragment)
        first = self._collect_ballot(
            index=index,
            amendment=amendment,
            current_draft=current_draft,
            registry=registry,
            kind="supermajority",
            additional_public_files=(),
        )
        threshold = high_threshold(self.repo, len(registry))
        source_prefix = (
            "MAJORITY" if meeting_decision_rigor(self.repo) == DecisionRigor.RELAXED
            else "SUPERMAJORITY"
        )
        if first["tally"]["AMENDMENT"] >= threshold:
            return True, f"COMPATIBLE_FRAGMENT_{source_prefix}_PASS", 1
        if first["tally"]["STATUS_QUO"] >= threshold:
            return False, f"COMPATIBLE_FRAGMENT_{source_prefix}_REJECT", 1
        reasons_path = self._collect_reasons(
            index=index,
            amendment=amendment,
            current_draft=current_draft,
            registry=registry,
            first_ballot=first,
        )
        protective = self._collect_ballot(
            index=index,
            amendment=amendment,
            current_draft=current_draft,
            registry=registry,
            kind="protective",
            additional_public_files=(reasons_path,),
        )
        adopted = protective["tally"]["AMENDMENT"] >= strict_majority_threshold(len(registry))
        return adopted, "COMPATIBLE_FRAGMENT_PROTECTIVE_" + ("PASS" if adopted else "REJECT"), 2

    @staticmethod
    def _fragment_as_amendment(fragment: ConflictFragment) -> AmendmentSubmission:
        return AmendmentSubmission(
            amendment_id=f"FRAGMENT-{fragment.fragment_id}",
            proposer_id="CONFLICT_GROUP",
            text=fragment.text,
            declared_type=AmendmentType.SUPPLEMENTARY,
            declared_impact_scope=fragment.impact_scope,
        )

    def _apply_fragment_bundle(
        self,
        *,
        index: int,
        bundle_id: str,
        fragments: list[ConflictFragment],
        current_draft: Path,
    ) -> Path:
        amendment = AmendmentSubmission(
            amendment_id=bundle_id,
            proposer_id="CONFLICT_GROUP",
            text="\n\n".join(
                f"[{item.fragment_id}; sources={','.join(item.source_amendment_ids)}]\n{item.text}"
                for item in fragments
            ),
            declared_type=AmendmentType.SUPPLEMENTARY,
            declared_impact_scope=sorted({scope for item in fragments for scope in item.impact_scope}),
        )
        return self._apply_adopted_amendment(index=index, amendment=amendment, current_draft=current_draft)

    def _run_exclusive_choice_set(
        self,
        *,
        index: int,
        choice_set: ConflictChoiceSet,
        current_draft: Path,
        registry: list[dict],
        docket_seed: str,
    ) -> tuple[str, int]:
        option_details = {
            f"OPTION_{item.fragment_id}": item.model_dump(mode="json")
            for item in choice_set.options
        }
        option_details["STATUS_QUO"] = {
            "text": "维持该冲突点的当前文本；不采纳本 choice set 中的任何新 option。",
            "source_amendment_ids": [],
        }
        prefix = f"B-{self.window_code}-{index:03d}-{choice_set.choice_set_id}"
        multi = self._collect_option_ballot(
            ballot_id=f"{prefix}-MULTI",
            kind="multi_option",
            choice_set=choice_set,
            option_details=option_details,
            current_draft=current_draft,
            registry=registry,
            additional_public_files=(),
        )
        try:
            seed_bytes = bytes.fromhex(docket_seed)
        except ValueError:
            seed_bytes = docket_seed.encode("utf-8")
        ranked = sorted(
            option_details,
            key=lambda option: (
                -int(multi["tally"][option]),
                hashlib.sha256(seed_bytes + b"\0top-two\0" + option.encode("utf-8")).digest(),
                option,
            ),
        )
        top_two = ranked[:2]
        selection_relative = (
            Path("public/general_principle/conflict_choice_sets")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}_{choice_set.choice_set_id}.top_two.json"
        )
        if not (self.repo.root / selection_relative).exists():
            self.repo.docs.write_once(
                selection_relative,
                json.dumps(
                    {
                        "choice_set_id": choice_set.choice_set_id,
                        "source_ballot_id": multi["ballot_id"],
                        "ranking": ranked,
                        "top_two": top_two,
                        "tie_break": "frozen docket seed + SHA-256 option ranking",
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "CONFLICT_CHOICE_SET_TOP_TWO_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "choice_set_id": choice_set.choice_set_id,
                    "top_two": top_two,
                    "record_path": str(selection_relative),
                },
                actor="orchestrator",
            )
        runoff_details = {option: option_details[option] for option in top_two}
        runoff = self._collect_option_ballot(
            ballot_id=f"{prefix}-TOP-TWO-SUPERMAJORITY",
            kind="top_two_supermajority",
            choice_set=choice_set,
            option_details=runoff_details,
            current_draft=current_draft,
            registry=registry,
            additional_public_files=(self.repo.root / selection_relative,),
        )
        threshold = high_threshold(self.repo, len(registry))
        winner = next((option for option in top_two if runoff["tally"][option] >= threshold), None)
        ballot_count = 2
        if winner is None:
            reasons_path = self._collect_option_reasons(
                index=index,
                choice_set=choice_set,
                current_draft=current_draft,
                registry=registry,
                first_ballot=runoff,
            )
            protective = self._collect_option_ballot(
                ballot_id=f"{prefix}-TOP-TWO-PROTECTIVE",
                kind="top_two_protective",
                choice_set=choice_set,
                option_details=runoff_details,
                current_draft=current_draft,
                registry=registry,
                additional_public_files=(self.repo.root / selection_relative, reasons_path),
            )
            majority = strict_majority_threshold(len(registry))
            winner = next(
                (option for option in top_two if protective["tally"][option] >= majority),
                "STATUS_QUO",
            )
            ballot_count += 1
        result_relative = (
            Path("public/general_principle/conflict_choice_sets")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}_{choice_set.choice_set_id}.result.json"
        )
        if not (self.repo.root / result_relative).exists():
            self.repo.docs.write_once(
                result_relative,
                json.dumps(
                    {
                        "choice_set_id": choice_set.choice_set_id,
                        "winner": winner,
                        "status_quo_selected": winner == "STATUS_QUO",
                        "ballot_count": ballot_count,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "CONFLICT_CHOICE_SET_RESOLVED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "choice_set_id": choice_set.choice_set_id,
                    "winner": winner,
                    "status_quo_selected": winner == "STATUS_QUO",
                    "ballot_count": ballot_count,
                    "record_path": str(result_relative),
                },
                actor="orchestrator",
            )
        self.engine.progress.info(
            f"互斥选择集 {choice_set.choice_set_id} 已完成 · winner={winner}"
        )
        return winner, ballot_count

    def _collect_option_ballot(
        self,
        *,
        ballot_id: str,
        kind: str,
        choice_set: ConflictChoiceSet,
        option_details: dict[str, dict],
        current_draft: Path,
        registry: list[dict],
        additional_public_files: tuple[Path, ...],
    ) -> dict:
        private_root = Path("governance_private/general_principle/ballots") / ballot_id
        frozen_path = self.repo.root / private_root / "frozen_ballot.json"
        public_result_relative = Path("public/general_principle/ballots") / f"{ballot_id}.result.json"
        if frozen_path.exists():
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
            if not (self.repo.root / public_result_relative).exists():
                self._write_public_option_ballot_result(public_result_relative, frozen)
            return frozen
        question_relative = Path("public/general_principle/ballots") / f"{ballot_id}.question.json"
        question_path = self.repo.root / question_relative
        if not question_path.exists():
            self.repo.docs.write_once(
                question_relative,
                json.dumps(
                    {
                        "ballot_id": ballot_id,
                        "kind": kind,
                        "choice_set_id": choice_set.choice_set_id,
                        "question": choice_set.question,
                        "options": option_details,
                        "current_draft_path": str(current_draft.relative_to(self.repo.root)),
                        "current_draft_sha256": self._sha256(current_draft.read_text(encoding="utf-8")),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        eligible = {record["representative_id"] for record in registry}
        option_codes = tuple(option_details)
        attempt = _select_ballot_attempt(
            repo=self.repo,
            engine=self.engine,
            ballot_id=ballot_id,
            private_root=private_root,
            eligible=eligible,
            load_records=lambda votes_path, recovered_path: self._load_option_vote_records(
                votes_path,
                recovered_path,
                ballot_id=ballot_id,
                eligible=eligible,
                legal_options=set(option_codes),
                repo_root=self.repo.root,
            ),
        )
        ballot = SealedBallot(
            ballot_id=ballot_id,
            eligible_representatives=eligible,
            options=option_codes,
        )
        choices_by_id = {
            representative_id: submission["choice"]
            for representative_id, submission in attempt.records.items()
        }
        missing_records = [
            record
            for record in registry
            if record["representative_id"] not in attempt.records
        ]

        def collect_vote(record: dict) -> tuple[str, OptionBallotAction, Path]:
            representative_id = record["representative_id"]
            persona = Persona(record["runtime"]["persona"])
            spec = self.resolver.representative_context_spec(
                persona=persona,
                stage="ballot",
                representative_id=representative_id,
                public_state_files=(
                    self.repo.root / "public/task.json",
                    current_draft,
                    question_path,
                    *additional_public_files,
                ),
            )
            response = self.engine.invoke_participant(
                representative_id,
                system_text=self.assembler.assemble(spec),
                user_text=(
                    "Choose exactly one legal option from "
                    + json.dumps(list(option_codes), ensure_ascii=False)
                    + '. Return exactly {"choice":"OPTION_CODE"}. Do not use Markdown or add keys.'
                ),
                stage=kind,
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = OptionBallotAction.model_validate(json.loads(response.text))
                if action.choice not in option_details:
                    raise ValueError("choice is not a legal option")
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            vote_relative = _next_vote_record_path(
                repo=self.repo,
                private_root=private_root,
                votes_relative=attempt.votes_relative,
                representative_id=representative_id,
            )
            return representative_id, action, vote_relative

        def persist_vote(result: tuple[str, OptionBallotAction, Path]) -> None:
            representative_id, action, vote_relative = result
            self.repo.docs.write_once(
                vote_relative,
                json.dumps(
                    {
                        "ballot_id": ballot_id,
                        "representative_id": representative_id,
                        "choice": action.choice,
                    },
                    indent=2,
                ),
            )
            self.repo.events.append(
                "SEALED_BALLOT_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "ballot_id": ballot_id,
                    "representative_id": representative_id,
                    "attempt_number": attempt.number,
                    "record_path": str(vote_relative),
                },
                actor=representative_id,
            )

        completed = run_bounded_representative_lanes(
            missing_records,
            collect_vote,
            getattr(
                self.engine,
                "model_concurrency_limit",
                lambda _provider_id, _model_id: 1,
            ),
            on_result=persist_vote,
            progress=self.engine.progress,
            progress_records=registry,
            completed_participant_ids=choices_by_id,
        )
        for representative_id, action, _vote_relative in completed:
            choices_by_id[representative_id] = action.choice
        for record in registry:
            representative_id = record["representative_id"]
            ballot.submit(representative_id, choices_by_id[representative_id])
        ballot.close()
        frozen = {
            **ballot.sealed_export(),
            "kind": kind,
            "choice_set_id": choice_set.choice_set_id,
            "attempt_number": attempt.number,
            "options": list(option_codes),
            "eligible_count": len(eligible),
            "tally": ballot.tally(),
        }
        self.repo.docs.write_once(private_root / "frozen_ballot.json", json.dumps(frozen, indent=2))
        self.repo.events.append(
            "SEALED_BALLOT_CLOSED",
            {
                "meeting_id": self.repo.meeting_id,
                "ballot_id": ballot_id,
                "attempt_number": attempt.number,
                "submitted_count": len(eligible),
            },
            actor="orchestrator",
        )
        self._write_public_option_ballot_result(public_result_relative, frozen)
        return frozen

    @staticmethod
    def _load_option_vote_records(
        votes_path: Path,
        recovered_path: Path,
        *,
        ballot_id: str,
        eligible: set[str],
        legal_options: set[str],
        repo_root: Path,
    ) -> BallotRecordInspection:
        records: dict[str, dict] = {}
        record_paths: dict[str, Path] = {}
        invalid_records: list[dict[str, str]] = []
        paths = list(votes_path.glob("*.json")) if votes_path.exists() else []
        if recovered_path.exists():
            paths.extend(recovered_path.glob("*/*.json"))
        for path in sorted(paths, key=lambda item: (item.parent != votes_path, str(item))):
            relative = path.relative_to(repo_root)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if set(raw) != {"ballot_id", "representative_id", "choice"}:
                    raise ValueError("record does not have the exact option-ballot schema")
                representative_id = raw["representative_id"]
                if raw["ballot_id"] != ballot_id or representative_id not in eligible:
                    raise ValueError("ballot or representative mismatch")
                if path.parent == votes_path and path.stem != representative_id:
                    raise ValueError("representative ID does not match record path")
                if path.parent != votes_path and path.parent.name != representative_id:
                    raise ValueError("representative ID does not match recovery record path")
                action = OptionBallotAction.model_validate({"choice": raw["choice"]})
                if action.choice not in legal_options:
                    raise ValueError("choice is not a legal option")
                if representative_id in records:
                    raise ValueError("multiple complete votes exist for one representative")
            except (KeyError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                invalid_records.append({"record_path": str(relative), "reason": str(exc)})
                continue
            records[representative_id] = dict(raw)
            record_paths[representative_id] = relative
        return BallotRecordInspection(records, record_paths, tuple(invalid_records))

    def _write_public_option_ballot_result(self, relative: Path, frozen: dict) -> None:
        self.repo.docs.write_once(
            relative,
            json.dumps(
                {
                    "ballot_id": frozen["ballot_id"],
                    "kind": frozen["kind"],
                    "choice_set_id": frozen["choice_set_id"],
                    "eligible_count": frozen["eligible_count"],
                    "tally": frozen["tally"],
                    "status": "CLOSED",
                },
                indent=2,
            ),
        )

    def _collect_option_reasons(
        self,
        *,
        index: int,
        choice_set: ConflictChoiceSet,
        current_draft: Path,
        registry: list[dict],
        first_ballot: dict,
    ) -> Path:
        root = (
            Path("governance_private/general_principle/conflict_reasons")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}_{choice_set.choice_set_id}"
        )
        public_relative = (
            Path("public/general_principle/conflict_reasons")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}_{choice_set.choice_set_id}.json"
        )
        public_path = self.repo.root / public_relative
        if public_path.exists():
            return public_path
        records_by_id: dict[str, dict] = {}
        missing_records = []
        ballot_result_path = (
            self.repo.root / "public/general_principle/ballots" / f"{first_ballot['ballot_id']}.result.json"
        )
        for record in registry:
            representative_id = record["representative_id"]
            relative = root / f"{representative_id}.json"
            absolute = self.repo.root / relative
            if absolute.exists():
                records_by_id[representative_id] = json.loads(
                    absolute.read_text(encoding="utf-8")
                )
            else:
                missing_records.append(record)

        def collect_reason(record: dict) -> tuple[str, dict, Path]:
            representative_id = record["representative_id"]
            relative = root / f"{representative_id}.json"
            persona = Persona(record["runtime"]["persona"])
            spec = self.resolver.representative_context_spec(
                persona=persona,
                stage="reason_statement",
                representative_id=representative_id,
                public_state_files=(
                    self.repo.root / "public/task.json",
                    current_draft,
                    ballot_result_path,
                ),
            )
            response = self.engine.invoke_participant(
                representative_id,
                system_text=self.assembler.assemble(spec),
                user_text=(
                    f"Your sealed choice was {first_ballot['votes'][representative_id]}. "
                    'Return exactly {"action":"PASS"} or '
                    '{"action":"STATE","reason":"..."}. Do not use Markdown or add keys.'
                ),
                stage="multi_option_reason_statement",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = ReasonStatementAction.model_validate(json.loads(response.text))
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            entry = {"representative_id": representative_id, **action.model_dump(mode="json")}
            return representative_id, entry, relative

        def persist_reason(result: tuple[str, dict, Path]) -> None:
            representative_id, entry, relative = result
            self.repo.docs.write_once(relative, json.dumps(entry, indent=2, ensure_ascii=False))
            self.repo.events.append(
                "REASON_STATEMENT_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "choice_set_id": choice_set.choice_set_id,
                    "representative_id": representative_id,
                    "record_path": str(relative),
                },
                actor=representative_id,
            )

        completed = run_bounded_representative_lanes(
            missing_records,
            collect_reason,
            getattr(self.engine, "model_concurrency_limit", lambda *_args: 1),
            on_result=persist_reason,
            progress=self.engine.progress,
            progress_records=registry,
            completed_participant_ids=records_by_id,
        )
        records_by_id.update({representative_id: entry for representative_id, entry, _ in completed})
        records = [records_by_id[record["representative_id"]] for record in registry]
        self.repo.docs.write_once(public_relative, json.dumps(records, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "REASON_STATEMENT_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "choice_set_id": choice_set.choice_set_id,
                "submission_count": len(records),
                "record_path": str(public_relative),
            },
            actor="orchestrator",
        )
        return public_path

    def _collect_ballot(
        self,
        *,
        index: int,
        amendment: AmendmentSubmission,
        current_draft: Path,
        registry: list[dict],
        kind: Literal["supermajority", "protective"],
        additional_public_files: tuple[Path, ...],
    ) -> dict:
        ballot_id = f"B-{self.window_code}-{index:03d}-{amendment.amendment_id}-{kind.upper()}"
        private_root = Path("governance_private/general_principle/ballots") / ballot_id
        frozen_path = self.repo.root / private_root / "frozen_ballot.json"
        public_result_relative = Path("public/general_principle/ballots") / f"{ballot_id}.result.json"
        public_result = self.repo.root / public_result_relative
        if frozen_path.exists():
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
            if not public_result.exists():
                self._write_public_ballot_result(public_result_relative, frozen)
            return frozen

        question_relative = Path("public/general_principle/ballots") / f"{ballot_id}.question.json"
        question_path = self.repo.root / question_relative
        if not question_path.exists():
            question = {
                "ballot_id": ballot_id,
                "kind": kind,
                "required_votes": (
                    high_threshold(self.repo, len(registry))
                    if kind == "supermajority"
                    else strict_majority_threshold(len(registry))
                ),
                "threshold_formula": (
                    high_threshold_formula(self.repo)
                    if kind == "supermajority"
                    else "floor(N_ACTIVE/2)+1"
                ),
                "amendment": amendment.model_dump(mode="json"),
                "options": ["AMENDMENT", "STATUS_QUO"],
                "current_draft_path": str(current_draft.relative_to(self.repo.root)),
                "current_draft_sha256": self._sha256(current_draft.read_text(encoding="utf-8")),
            }
            self.repo.docs.write_once(question_relative, json.dumps(question, indent=2, ensure_ascii=False))

        eligible = {record["representative_id"] for record in registry}
        attempt = _select_ballot_attempt(
            repo=self.repo,
            engine=self.engine,
            ballot_id=ballot_id,
            private_root=private_root,
            eligible=eligible,
            load_records=lambda votes_path, recovered_path: self._load_vote_records(
                votes_path,
                recovered_path,
                ballot_id=ballot_id,
                eligible=eligible,
                repo_root=self.repo.root,
            ),
        )
        ballot = SealedBallot(
            ballot_id=ballot_id,
            eligible_representatives=eligible,
            options=("AMENDMENT", "STATUS_QUO"),
        )
        choices_by_id = {
            representative_id: submission["choice"]
            for representative_id, submission in attempt.records.items()
        }
        missing_records = [
            record
            for record in registry
            if record["representative_id"] not in attempt.records
        ]

        def collect_vote(record: dict) -> tuple[str, BallotAction, Path]:
            representative_id = record["representative_id"]
            persona = Persona(record["runtime"]["persona"])
            spec = self.resolver.representative_context_spec(
                persona=persona,
                stage="ballot",
                representative_id=representative_id,
                public_state_files=(
                    self.repo.root / "public/task.json",
                    current_draft,
                    question_path,
                    *additional_public_files,
                ),
            )
            response = self.engine.invoke_participant(
                representative_id,
                system_text=self.assembler.assemble(spec),
                user_text=(
                    'Return exactly {"choice":"AMENDMENT"} or '
                    '{"choice":"STATUS_QUO"}. Do not use Markdown or add keys.'
                ),
                stage=f"{kind}_ballot",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = BallotAction.model_validate(json.loads(response.text))
            except (json.JSONDecodeError, ValidationError, TypeError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            vote_relative = _next_vote_record_path(
                repo=self.repo,
                private_root=private_root,
                votes_relative=attempt.votes_relative,
                representative_id=representative_id,
            )
            return representative_id, action, vote_relative

        def persist_vote(result: tuple[str, BallotAction, Path]) -> None:
            representative_id, action, vote_relative = result
            self.repo.docs.write_once(
                vote_relative,
                json.dumps(
                    {
                        "ballot_id": ballot_id,
                        "representative_id": representative_id,
                        "choice": action.choice,
                    },
                    indent=2,
                ),
            )
            self.repo.events.append(
                "SEALED_BALLOT_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "ballot_id": ballot_id,
                    "representative_id": representative_id,
                    "attempt_number": attempt.number,
                    "record_path": str(vote_relative),
                },
                actor=representative_id,
            )

        completed = run_bounded_representative_lanes(
            missing_records,
            collect_vote,
            getattr(
                self.engine,
                "model_concurrency_limit",
                lambda _provider_id, _model_id: 1,
            ),
            on_result=persist_vote,
            progress=self.engine.progress,
            progress_records=registry,
            completed_participant_ids=choices_by_id,
        )
        for representative_id, action, _vote_relative in completed:
            choices_by_id[representative_id] = action.choice
        for record in registry:
            representative_id = record["representative_id"]
            ballot.submit(representative_id, choices_by_id[representative_id])

        ballot.close()
        frozen = {
            **ballot.sealed_export(),
            "kind": kind,
            "amendment_id": amendment.amendment_id,
            "attempt_number": attempt.number,
            "options": list(ballot.options),
            "eligible_count": len(eligible),
            "tally": ballot.tally(),
        }
        self.repo.docs.write_once(private_root / "frozen_ballot.json", json.dumps(frozen, indent=2))
        self.repo.events.append(
            "SEALED_BALLOT_CLOSED",
            {
                "meeting_id": self.repo.meeting_id,
                "ballot_id": ballot_id,
                "attempt_number": attempt.number,
                "submitted_count": len(eligible),
            },
            actor="orchestrator",
        )
        self._write_public_ballot_result(public_result_relative, frozen)
        self.engine.progress.info(
            f"{ballot_id} 已关闭 · AMENDMENT={frozen['tally']['AMENDMENT']} · "
            f"STATUS_QUO={frozen['tally']['STATUS_QUO']}"
        )
        return frozen

    @staticmethod
    def _load_vote_records(
        votes_path: Path,
        recovered_path: Path,
        *,
        ballot_id: str,
        eligible: set[str],
        repo_root: Path,
    ) -> BallotRecordInspection:
        records: dict[str, dict] = {}
        record_paths: dict[str, Path] = {}
        invalid_records: list[dict[str, str]] = []
        paths = list(votes_path.glob("*.json")) if votes_path.exists() else []
        if recovered_path.exists():
            paths.extend(recovered_path.glob("*/*.json"))
        for path in sorted(paths, key=lambda item: (item.parent != votes_path, str(item))):
            path_relative = path.relative_to(repo_root)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if set(raw) != {"ballot_id", "representative_id", "choice"}:
                    raise ValueError("record does not have the exact amendment-ballot schema")
                representative_id = raw["representative_id"]
                if raw["ballot_id"] != ballot_id:
                    raise ValueError("ballot ID mismatch")
                if representative_id not in eligible:
                    raise ValueError("ineligible representative")
                if path.parent == votes_path and path.stem != representative_id:
                    raise ValueError("representative ID does not match record path")
                if path.parent != votes_path and path.parent.name != representative_id:
                    raise ValueError("representative ID does not match recovery record path")
                action = BallotAction.model_validate({"choice": raw["choice"]})
                if representative_id in records:
                    raise ValueError("multiple complete votes exist for one representative")
            except (KeyError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                invalid_records.append({"record_path": str(path_relative), "reason": str(exc)})
                continue
            records[representative_id] = {
                "ballot_id": ballot_id,
                "representative_id": representative_id,
                "choice": action.choice,
            }
            record_paths[representative_id] = path_relative
        return BallotRecordInspection(
            records=records,
            record_paths=record_paths,
            invalid_records=tuple(invalid_records),
        )

    def _write_public_ballot_result(self, relative: Path, frozen: dict) -> None:
        result = {
            "ballot_id": frozen["ballot_id"],
            "kind": frozen["kind"],
            "amendment_id": frozen["amendment_id"],
            "eligible_count": frozen["eligible_count"],
            "tally": frozen["tally"],
            "status": "CLOSED",
        }
        self.repo.docs.write_once(relative, json.dumps(result, indent=2))

    def _collect_reasons(
        self,
        *,
        index: int,
        amendment: AmendmentSubmission,
        current_draft: Path,
        registry: list[dict],
        first_ballot: dict,
    ) -> Path:
        root = (
            Path("governance_private/general_principle/reasons")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}"
        )
        public_relative = (
            Path("public/general_principle/reasons")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}.json"
        )
        public_path = self.repo.root / public_relative
        if public_path.exists():
            return public_path
        records_by_id: dict[str, dict] = {}
        missing_records = []
        ballot_result_path = (
            self.repo.root
            / "public/general_principle/ballots"
            / f"{first_ballot['ballot_id']}.result.json"
        )
        for record in registry:
            representative_id = record["representative_id"]
            relative = root / f"{representative_id}.json"
            absolute = self.repo.root / relative
            if absolute.exists():
                records_by_id[representative_id] = json.loads(
                    absolute.read_text(encoding="utf-8")
                )
            else:
                missing_records.append(record)

        def collect_reason(record: dict) -> tuple[str, dict, Path]:
            representative_id = record["representative_id"]
            relative = root / f"{representative_id}.json"
            persona = Persona(record["runtime"]["persona"])
            own_vote = (
                self.repo.root
                / "governance_private/general_principle/ballots"
                / first_ballot["ballot_id"]
                / "votes"
                / f"{representative_id}.json"
            )
            spec = self.resolver.representative_context_spec(
                persona=persona,
                stage="reason_statement",
                representative_id=representative_id,
                public_state_files=(
                    self.repo.root / "public/task.json",
                    current_draft,
                    ballot_result_path,
                ),
                own_state_files=(own_vote,),
            )
            response = self.engine.invoke_participant(
                representative_id,
                system_text=self.assembler.assemble(spec),
                user_text=(
                    'Return exactly {"action":"PASS"} or '
                    '{"action":"STATE","reason":"..."}. Do not use Markdown or add keys.'
                ),
                stage="reason_statement",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = ReasonStatementAction.model_validate(json.loads(response.text))
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            entry = {"representative_id": representative_id, **action.model_dump(mode="json")}
            return representative_id, entry, relative

        def persist_reason(result: tuple[str, dict, Path]) -> None:
            representative_id, entry, relative = result
            self.repo.docs.write_once(relative, json.dumps(entry, indent=2, ensure_ascii=False))
            self.repo.events.append(
                "REASON_STATEMENT_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "amendment_id": amendment.amendment_id,
                    "representative_id": representative_id,
                    "record_path": str(relative),
                },
                actor=representative_id,
            )

        completed = run_bounded_representative_lanes(
            missing_records,
            collect_reason,
            getattr(self.engine, "model_concurrency_limit", lambda *_args: 1),
            on_result=persist_reason,
            progress=self.engine.progress,
            progress_records=registry,
            completed_participant_ids=records_by_id,
        )
        records_by_id.update({representative_id: entry for representative_id, entry, _ in completed})
        records = [records_by_id[record["representative_id"]] for record in registry]
        self.repo.docs.write_once(public_relative, json.dumps(records, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "REASON_STATEMENT_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "amendment_id": amendment.amendment_id,
                "submission_count": len(records),
                "record_path": str(public_relative),
            },
            actor="orchestrator",
        )
        for entry in records:
            if entry["action"] == "STATE":
                self.engine.progress.speech(entry["representative_id"], "表决理由", entry["reason"])
        return public_path

    def _apply_adopted_amendment(
        self,
        *,
        index: int,
        amendment: AmendmentSubmission,
        current_draft: Path,
    ) -> Path:
        root = Path("chair_private/general_principle/applications") / f"window_{self.window_number:03d}"
        application_relative = root / f"step_{index:03d}_{amendment.amendment_id}.json"
        application_path = self.repo.root / application_relative
        draft_relative = (
            Path("public/general_principle/working")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}.md"
        )
        draft_path = self.repo.root / draft_relative
        base_text = current_draft.read_text(encoding="utf-8")
        base_sha = self._sha256(base_text)
        if application_path.exists():
            application = ChairDraftApplication.model_validate_json(application_path.read_text(encoding="utf-8"))
        else:
            response = self.engine.invoke_participant(
                "CHAIR",
                system_text=self._chair_system_text(),
                user_text=(
                    "Apply exactly the adopted amendment to the current draft as a document-control task. "
                    "Do not add substantive content not entailed by the amendment; preserve unaffected text "
                    "and dissent provenance. Return one JSON object with action "
                    "APPLY_ADOPTED_AMENDMENT, amendment_id, base_sha256, updated_text, changes (one or more "
                    "objects with operation ADD/REPLACE/DELETE/MOVE/RENUMBER, target, description), and reason. "
                    "Echo the supplied amendment ID and base SHA-256 exactly. Do not use Markdown fences.\n\n"
                    f"AMENDMENT ID: {amendment.amendment_id}\nBASE SHA-256: {base_sha}\n\n"
                    "CURRENT DRAFT:\n"
                    + base_text
                    + "\n\nADOPTED AMENDMENT:\n"
                    + amendment.model_dump_json(indent=2)
                ),
                stage="chair_apply_amendment",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                application = ChairDraftApplication.model_validate(json.loads(response.text))
                if application.amendment_id != amendment.amendment_id or application.base_sha256 != base_sha:
                    raise ValueError("Chair application does not match the adopted amendment/base draft")
                if application.updated_text.strip() == base_text.strip():
                    raise ValueError("adopted amendment application did not change the draft")
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id="CHAIR",
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            self.repo.docs.write_once(application_relative, application.model_dump_json(indent=2))
            self.repo.events.append(
                "CHAIR_DRAFT_APPLICATION_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "amendment_id": amendment.amendment_id,
                    "base_sha256": base_sha,
                    "record_path": str(application_relative),
                },
                actor="CHAIR",
            )
        if application.amendment_id != amendment.amendment_id or application.base_sha256 != base_sha:
            raise ValueError("persisted Chair application does not match current draft provenance")
        if not draft_path.exists():
            self.repo.docs.write_once(draft_relative, application.updated_text.rstrip() + "\n")
            self.repo.docs.write_once(
                Path("public/general_principle/working")
                / f"window_{self.window_number:03d}"
                / f"step_{index:03d}.provenance.json",
                json.dumps(
                    {
                        "base_draft_path": str(current_draft.relative_to(self.repo.root)),
                        "base_sha256": base_sha,
                        "adopted_amendment_id": amendment.amendment_id,
                        "chair_application_path": str(application_relative),
                        "result_sha256": self._sha256(application.updated_text.rstrip() + "\n"),
                    },
                    indent=2,
                ),
            )
        self.engine.progress.speech("CHAIR", f"已采纳 {amendment.amendment_id} 后的工作稿", draft_path.read_text())
        return draft_path

    def _freeze_decision(
        self,
        *,
        index: int,
        amendment_id: str,
        outcome: str,
        source: str,
        ballot_count: int,
        resulting_draft: Path,
        chair_disposition: ChairAmendmentDisposition,
        component_outcomes: list[dict] | None = None,
    ) -> None:
        relative = self._decision_relative(index, amendment_id)
        record = {
            "step": index,
            "amendment_id": amendment_id,
            "outcome": outcome,
            "source": source,
            "ballot_count": ballot_count,
            "resulting_draft_path": str(resulting_draft.relative_to(self.repo.root)),
            "chair_disposition": chair_disposition.model_dump(mode="json"),
        }
        if component_outcomes is not None:
            record["component_outcomes"] = component_outcomes
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "AMENDMENT_DECISION_FROZEN",
            {"meeting_id": self.repo.meeting_id, **{k: record[k] for k in ("step", "amendment_id", "outcome", "source")}},
            actor="orchestrator",
        )

    def _freeze_target_draft(self, current_draft: Path) -> Path:
        relative = Path("public/general_principle") / f"{self.target_draft_name}.md"
        absolute = self.repo.root / relative
        if not absolute.exists():
            text = current_draft.read_text(encoding="utf-8")
            self.repo.docs.write_once(relative, text)
            self.repo.docs.write_once(
                Path("public/general_principle") / f"{self.target_draft_name}.provenance.json",
                json.dumps(
                    {
                        "source_draft_path": str(current_draft.relative_to(self.repo.root)),
                        "source_sha256": self._sha256(text),
                        "window_id": f"general-principle-{self.window_number:03d}",
                    },
                    indent=2,
                ),
            )
            self.repo.events.append(
                "GENERAL_PRINCIPLE_DRAFT_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "draft": self.target_draft_name,
                    "record_path": str(relative),
                },
                actor="orchestrator",
            )
        return absolute

    def _decision_relative(self, index: int, amendment_id: str) -> Path:
        return (
            Path("public/general_principle/decisions")
            / f"window_{self.window_number:03d}"
            / f"step_{index:03d}_{amendment_id}.json"
        )

    def _decision_path(self, index: int, amendment_id: str) -> Path:
        return self.repo.root / self._decision_relative(index, amendment_id)

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
