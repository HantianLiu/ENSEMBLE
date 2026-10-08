from __future__ import annotations

import hashlib
import json
import secrets
import threading
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from project_ensemble.errors import ResearchQualityControlError, ResearchRequestRejectedError
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.readability_policy import (
    structured_prose_context_from_repo, structured_result_prose_contract,
)
from project_ensemble.runtime.structured_output import parse_json_object
from project_ensemble.research.cache import ResearchPacketCache
from project_ensemble.research.documents import (
    DocumentFetcher,
    HttpDocumentFetcher,
    LiteratureBundleManager,
)
from project_ensemble.research.models import (
    ConfidenceLevel,
    DocumentArchiveStatus,
    EvidencePacket,
    EvidenceUseClass,
    FreshnessClass,
    KnowledgeStatus,
    LiteratureConsensus,
    NormalizedClaim,
    ResearchRequest,
    ResearchSynthesis,
    ScreeningDecision,
    ScreeningDecisionSupplement,
)
from project_ensemble.research.retrievers import (
    ResearchRetrievalResult,
    ResearchRetriever,
    coerce_retrieval_result,
    openalex_unavailable_reason,
)
from project_ensemble.research.source_reading import SourceReader
from project_ensemble.research.institutional_access import institutional_access_allowed
from project_ensemble.providers.retry import call_with_retries
from project_ensemble.storage.meeting import MeetingRepository


class AdjustableCallGate:
    """Resize future model admissions without interrupting active calls."""

    def __init__(self, limit: int):
        self._condition = threading.Condition()
        self._limit = max(1, int(limit))
        self._active = 0

    def set_limit(self, limit: int) -> None:
        with self._condition:
            self._limit = max(1, int(limit))
            self._condition.notify_all()

    def __enter__(self):
        with self._condition:
            while self._active >= self._limit:
                self._condition.wait()
            self._active += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        with self._condition:
            self._active -= 1
            self._condition.notify_all()
        return False


class ResearchDesk:
    """Claim-scoped research service with public packets and private retrieval traces."""

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        retriever: ResearchRetriever,
        freshness_windows: dict[FreshnessClass, int] | None = None,
        document_fetcher: DocumentFetcher | None = None,
        max_output_tokens: int | None = None,
        retrieval_max_retries: int = 0,
        retrieval_retry_base_delay_seconds: float = 0.5,
    ):
        self.repo = repo
        self.engine = engine
        self.retriever = retriever
        self.max_output_tokens = max_output_tokens
        self.retrieval_max_retries = retrieval_max_retries
        self.retrieval_retry_base_delay_seconds = retrieval_retry_base_delay_seconds
        self.freshness_windows = freshness_windows or {
            FreshnessClass.VOLATILE: 7,
            FreshnessClass.VERSIONED: 30,
            FreshnessClass.STABLE: 180,
        }
        if set(self.freshness_windows) != set(FreshnessClass):
            raise ValueError("freshness windows must configure VOLATILE, VERSIONED, and STABLE")
        if any(days < 0 for days in self.freshness_windows.values()):
            raise ValueError("freshness windows cannot be negative")
        self.literature_bundle = LiteratureBundleManager(
            repo=repo,
            fetcher=document_fetcher or HttpDocumentFetcher(),
        )
        self.source_reader = SourceReader(
            repo=repo,
            retriever=retriever,
            document_fetcher=document_fetcher or HttpDocumentFetcher(),
        )
        self.packet_cache = ResearchPacketCache(repo)
        self._assert_enabled()
        model_limit = getattr(
            self.engine, "participant_concurrency_limit", lambda _participant_id: 1
        )("RESEARCH_DESK")
        self._model_gate = AdjustableCallGate(model_limit)

    def update_model_concurrency_limit(self, limit: int) -> None:
        self._model_gate.set_limit(limit)

    def _structured_prose_contract(self, *, planning_scope: bool = False) -> str:
        preferences, disciplines = structured_prose_context_from_repo(self.repo)
        return structured_result_prose_contract(
            preferences, disciplines, planning_scope=planning_scope,
        )

    def research(
        self,
        request: ResearchRequest,
        *,
        normalized_claim: NormalizedClaim | None = None,
        retrieval_result: ResearchRetrievalResult | None = None,
        prepared_sources: tuple[list[dict], list[dict]] | None = None,
        request_id: str | None = None,
        defer_bundle_rebuild: bool = False,
    ) -> EvidencePacket:
        self._assert_requester(request.requester_id)
        request_id = request_id or ("RQ-" + secrets.token_hex(6).upper())
        normalized = normalized_claim or self.normalize_request(request)
        self._progress_step("规范化完成；正在检索外部来源")
        fingerprint = self._fingerprint(normalized)
        if not normalized.is_researchable:
            self._record_request(
                request_id=request_id,
                request=request,
                normalized=normalized,
                fingerprint=fingerprint,
                cache_hit=False,
                packet_path=None,
                status="REJECTED_NOT_CLAIM_SCOPED",
            )
            self.repo.events.append(
                "RESEARCH_REQUEST_REJECTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "request_id": request_id,
                    "requester_id": request.requester_id,
                    "stage": request.stage.value,
                    "claim_fingerprint": fingerprint,
                    "reason": normalized.rejection_reason,
                },
                actor="RESEARCH_DESK",
            )
            raise ResearchRequestRejectedError(str(normalized.rejection_reason))
        matching_packets = self._matching_cached_packets(fingerprint)
        cached = None if request.force_refresh else self._fresh_cached_packet(matching_packets)
        if cached is not None:
            if not defer_bundle_rebuild:
                self.literature_bundle.rebuild_download_bundle()
            self._record_request(
                request_id=request_id,
                request=request,
                normalized=normalized,
                fingerprint=fingerprint,
                cache_hit=True,
                packet_path=cached[0],
                status="CACHE_REUSED",
            )
            self.repo.events.append(
                "RESEARCH_EVIDENCE_PACKET_REUSED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "request_id": request_id,
                    "requester_id": request.requester_id,
                    "stage": request.stage.value,
                    "claim_fingerprint": fingerprint,
                    "packet_path": str(cached[0]),
                },
                actor="RESEARCH_DESK",
            )
            return cached[1]

        retrieval = retrieval_result or coerce_retrieval_result(
            call_with_retries(
                lambda: self.retriever.retrieve(normalized),
                max_retries=self.retrieval_max_retries,
                base_delay_seconds=self.retrieval_retry_base_delay_seconds,
                on_retry=self._report_retrieval_retry,
            ),
            default_backend_ids=getattr(
                self.retriever, "backend_ids", (type(self.retriever).__name__,)
            ),
        )
        candidates = retrieval.candidates
        query_trace = retrieval.query_trace
        failed_backend_ids = list(retrieval.failed_backend_ids)
        if failed_backend_ids:
            openalex_reason = openalex_unavailable_reason(query_trace)
            if openalex_reason is not None:
                callback = getattr(getattr(self.engine, "progress", None), "research_backend_unavailable", None)
                if callable(callback):
                    callback("RESEARCH_DESK", "openalex", openalex_reason)
            effective_backend_ids = list(retrieval.effective_backend_ids)
            self.repo.events.append(
                "RESEARCH_RETRIEVAL_BACKEND_DEGRADED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "failed_backend_ids": failed_backend_ids,
                    "openalex_error_summary": openalex_reason,
                    "continuing_backend_ids": effective_backend_ids,
                    "meeting_continues": True,
                },
                actor="RESEARCH_DESK",
            )
            progress = getattr(self.engine, "progress", None)
            info = getattr(progress, "info", None)
            if callable(info):
                info(
                    "Research Desk · 检索后端 "
                    + ", ".join(failed_backend_ids)
                    + " 暂时不可用；改用 "
                    + ", ".join(effective_backend_ids)
                    + " 继续，覆盖率将降级"
                )
        self._validate_adversarial_query_trace(query_trace)
        candidates = self._deduplicate_candidate_sources(candidates)
        self._progress_step("检索完成；正在读取候选来源")
        if prepared_sources is None:
            candidates, source_read_ledger = self.source_reader.prepare(
                candidates,
                question=normalized.verification_question,
                request_key=request_id,
            )
        else:
            candidates, source_read_ledger = prepared_sources
        self._progress_step("等待模型名额 · 检索与原文读取已完成，待证据综合", waiting=True)
        with self._model_gate:
            synthesis = self._synthesize(request, normalized, candidates)
        self._progress_step("综合返回；正在核对来源与引文")
        self._validate_source_bounded(synthesis, candidates)
        cited_ids = {
            finding.source_id
            for collection in (
                synthesis.packet.supporting_evidence,
                synthesis.packet.contradictory_evidence,
                synthesis.packet.scope_limitations,
                synthesis.packet.canonical_alternatives,
            )
            for finding in collection
        }
        unread_cited = [
            item for item in candidates
            if item["source_id"] in cited_ids
            and item.get("source_read", {}).get("status") != "READABLE_EXCERPT"
            and self.source_reader._readable_location(item)
        ]
        if unread_cited:
            self._progress_step("正在补读被引用来源")
            reread, extra_ledger = self.source_reader.prepare(
                unread_cited[:3],
                question=normalized.verification_question,
                request_key=request_id,
            )
            updated = {item["source_id"]: item for item in reread}
            candidates = [updated.get(item["source_id"], item) for item in candidates]
            existing_ids = {item["source_id"] for item in candidates}
            candidates.extend(item for item in reread if item["source_id"] not in existing_ids)
            source_read_ledger.extend(extra_ledger)
            if any(item.get("source_read", {}).get("status") == "READABLE_EXCERPT"
                   for item in reread):
                try:
                    self._progress_step("等待模型名额 · 补读后复核", waiting=True)
                    with self._model_gate:
                        with self.engine.recoverable_call():
                            reviewed = self._synthesize(
                                request, normalized, candidates,
                                stage="research_evidence_synthesis_source_review",
                            )
                    self._validate_source_bounded(reviewed, candidates)
                    synthesis = reviewed
                    self._progress_step("复核返回；正在核对来源与引文")
                except Exception as exc:
                    self.repo.events.append(
                        "RESEARCH_SOURCE_REVIEW_DEGRADED",
                        {
                            "meeting_id": self.repo.meeting_id,
                            "request_id": request_id,
                            "error_type": type(exc).__name__,
                            "kept_initial_synthesis": True,
                        },
                        actor="RESEARCH_DESK",
                    )
        self._progress_step("校验完成；正在归档证据包")
        packet_id = "RP-" + secrets.token_hex(6).upper()
        now = datetime.now(timezone.utc)
        retrieval_backend_ids = list(retrieval.effective_backend_ids)
        published_confidence = synthesis.packet.confidence
        if failed_backend_ids or set(retrieval_backend_ids) == {"openalex"}:
            coverage_note = (
                " Retrieval continued without unavailable backend(s) "
                + ", ".join(failed_backend_ids)
                + ", so overall retrieval coverage is LOW."
                if failed_backend_ids
                else " OpenAlex-only retrieval is limited to scholarly metadata, available abstracts, "
                "and discovered open links, so overall retrieval coverage is LOW."
            )
            published_confidence = synthesis.packet.confidence.model_copy(
                update={
                    "coverage": ConfidenceLevel.LOW,
                    "rationale": (
                        synthesis.packet.confidence.rationale.rstrip()
                        + coverage_note
                    ),
                }
            )
        maximum_cache_reuse_age_days = self.freshness_windows[normalized.freshness_class]
        cache_expires_at = now + timedelta(days=maximum_cache_reuse_age_days)
        archived_sources = self.literature_bundle.archive_sources(
            packet_id=packet_id,
            sources=synthesis.packet.sources,
            candidates=candidates,
        )
        supporting_source_ids = {
            finding.source_id for finding in synthesis.packet.supporting_evidence
        }
        read_source_ids = {
            item["source_id"] for item in candidates
            if item.get("source_read", {}).get("status") == "READABLE_EXCERPT"
        }
        archived_sources = [
            source if source.source_id in read_source_ids
            else source.model_copy(update={"evidence_use_class": EvidenceUseClass.PROVISIONAL})
            for source in archived_sources
        ]
        has_archived_eligible_support = any(
            source.source_id in supporting_source_ids
            and source.source_id in read_source_ids
            and source.archive_status == DocumentArchiveStatus.ARCHIVED
            and source.evidence_use_class
            in {
                EvidenceUseClass.AUTHORITATIVE,
                EvidenceUseClass.PRIMARY,
                EvidenceUseClass.REVIEW,
            }
            for source in archived_sources
        )
        requested_status = synthesis.packet.knowledge_status
        published_status = requested_status
        if requested_status == KnowledgeStatus.SOURCE_BACKED and not has_archived_eligible_support:
            published_status = KnowledgeStatus.QUALIFIED
        archived_count = sum(
            source.archive_status == DocumentArchiveStatus.ARCHIVED
            for source in archived_sources
        )
        full_text_access_assessment = (
            f"已归档所引来源原件 {archived_count}/{len(archived_sources)}；"
            f"综合前取得相关原文摘录 {len(read_source_ids & {source.source_id for source in archived_sources})} 项。"
            "原文摘录不等于阅读全文；未取得可核对摘录的来源属于 PROVISIONAL，"
            "不能单独支撑 SOURCE_BACKED。"
        )
        packet = synthesis.packet.model_copy(
            update={
                "packet_id": packet_id,
                "claim_fingerprint": fingerprint,
                "original_claim": request.claim,
                "normalized_claim": normalized.normalized_claim,
                "verification_question": normalized.verification_question,
                "scope_terms": normalized.scope_terms,
                "source_domain": normalized.source_domain,
                "retrieval_backend_ids": retrieval_backend_ids,
                "confidence": published_confidence,
                "retrieved_at": now,
                "freshness_class": normalized.freshness_class,
                "maximum_cache_reuse_age_days": maximum_cache_reuse_age_days,
                "cache_expires_at": cache_expires_at,
                "sources": archived_sources,
                "full_text_access_assessment": full_text_access_assessment,
                "source_reading_performed": True,
                "source_recovery_performed": True,
                "institutional_access_allowed": institutional_access_allowed(self.repo),
                "knowledge_status": published_status,
                "supersedes_packet_ids": (
                    [matching_packets[0][2].packet_id] if matching_packets else []
                ),
            }
        )
        requested_consensus = packet.consensus
        requested_knowledge_status = packet.knowledge_status
        packet = self._downgrade_unsupported_clear_consensus(packet)
        if packet.consensus != requested_consensus:
            self.repo.events.append(
                "RESEARCH_PACKET_CONSENSUS_DOWNGRADED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "packet_id": packet.packet_id,
                    "requested_consensus": requested_consensus.value,
                    "published_consensus": packet.consensus.value,
                    "requested_knowledge_status": requested_knowledge_status.value,
                    "published_knowledge_status": packet.knowledge_status.value,
                    "reason": "CLEAR consensus threshold was not met; evidence packet retained at lower certainty",
                },
                actor="RESEARCH_DESK",
            )
        packet = self._validate_final_packet(packet)
        packet_path = Path("public/research/evidence_packets") / f"{packet_id}.json"
        trace_path = Path("audit_private/research/traces") / f"{request_id}.json"
        self.repo.docs.write_once(
            packet_path,
            json.dumps(
                packet.model_dump(mode="json"),
                indent=2,
                ensure_ascii=False,
            ),
        )
        self.repo.docs.write_once(
            trace_path,
            json.dumps(
                {
                    "request_id": request_id,
                    "request": request.model_dump(mode="json"),
                    "normalized_claim": normalized.model_dump(mode="json"),
                    "claim_fingerprint": fingerprint,
                    "search_queries": query_trace,
                    "candidate_sources": candidates,
                    "screening_decisions": [
                        x.model_dump(mode="json") for x in synthesis.screening_decisions
                    ],
                    "model_requested_knowledge_status": requested_status.value,
                    "published_knowledge_status": published_status.value,
                    "model_requested_consensus": requested_consensus.value,
                    "published_consensus": packet.consensus.value,
                    "retrieval_backend_ids": retrieval_backend_ids,
                    "failed_retrieval_backend_ids": failed_backend_ids,
                    "model_requested_coverage": synthesis.packet.confidence.coverage.value,
                    "published_coverage": published_confidence.coverage.value,
                    "full_text_access_assessment": full_text_access_assessment,
                    "source_reading": source_read_ledger,
                    "packet_path": str(packet_path),
                    "created_at": now.isoformat(),
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        literature_bundle_path = (
            None
            if defer_bundle_rebuild
            else self.literature_bundle.rebuild_download_bundle()
        )
        self.repo.events.append(
            "RESEARCH_EVIDENCE_PACKET_CREATED",
            {
                "meeting_id": self.repo.meeting_id,
                "request_id": request_id,
                "requester_id": request.requester_id,
                "stage": request.stage.value,
                "claim_fingerprint": fingerprint,
                "packet_id": packet_id,
                "packet_path": str(packet_path),
                "audit_trace_path": str(trace_path),
                "literature_bundle_path": (
                    str(literature_bundle_path)
                    if literature_bundle_path is not None
                    else None
                ),
                "literature_bundle_rebuild_deferred": defer_bundle_rebuild,
                "forced_refresh": request.force_refresh,
                "refresh_reason": request.refresh_reason,
            },
            actor="RESEARCH_DESK",
        )
        return packet

    @staticmethod
    def _downgrade_unsupported_clear_consensus(packet: EvidencePacket) -> EvidencePacket:
        """Keep usable evidence when only the model's consensus label is too strong.

        ``CLEAR`` is a literature-consensus claim, not the threshold for keeping
        a source-backed packet.  A single eligible primary source can remain
        ``SOURCE_BACKED`` while its consensus is conservatively published as
        ``QUALIFIED``.
        """

        if packet.consensus != LiteratureConsensus.CLEAR:
            return packet
        supporting_ids = {finding.source_id for finding in packet.supporting_evidence}
        supporting_sources = [
            source for source in packet.sources if source.source_id in supporting_ids
        ]
        eligible = [
            source
            for source in supporting_sources
            if source.evidence_use_class
            in {
                EvidenceUseClass.AUTHORITATIVE,
                EvidenceUseClass.PRIMARY,
                EvidenceUseClass.REVIEW,
            }
        ]
        if not supporting_sources:
            return packet
        status = packet.knowledge_status
        if not eligible and status == KnowledgeStatus.SOURCE_BACKED:
            status = KnowledgeStatus.QUALIFIED
        note = (
            "Model requested CLEAR consensus, but the supporting evidence did not meet the "
            "independent-source/authoritative-review threshold; consensus was downgraded to "
            "QUALIFIED while preserving the claim-scoped evidence packet."
        )
        return packet.model_copy(
            update={
                "consensus": LiteratureConsensus.QUALIFIED,
                "knowledge_status": status,
                "evidence_conflict_assessment": (
                    packet.evidence_conflict_assessment.rstrip() + " " + note
                ),
            }
        )

    @staticmethod
    def _validate_final_packet(packet: EvidencePacket) -> EvidencePacket:
        """Convert post-archive packet inconsistencies into claim-scoped QC.

        The strict EvidencePacket validator remains authoritative.  This boundary
        prevents a single malformed claim from escaping as a meeting-wide
        Pydantic exception after the research round has already isolated claims.
        """

        try:
            return EvidencePacket.model_validate(packet.model_dump(mode="python"))
        except ValidationError as exc:
            raise ResearchQualityControlError(
                "RESEARCH_EVIDENCE_PACKET_FINAL_VALIDATION_FAILED",
                "Evidence packet failed final post-archive QC: " + str(exc),
            ) from exc

    def _deduplicate_candidate_sources(self, candidates: list[dict]) -> list[dict]:
        """Quarantine duplicate candidate rows while preserving first-seen metadata."""

        seen: set[str] = set()
        retained: list[dict] = []
        duplicate_ids: list[str] = []
        for candidate in candidates:
            source_id = str(candidate.get("source_id") or "").strip()
            if not source_id:
                raise ResearchQualityControlError(
                    "RESEARCH_CANDIDATE_SOURCE_ID_MISSING",
                    "A retrieved candidate source has no source_id and cannot be isolated safely",
                )
            if source_id in seen:
                duplicate_ids.append(source_id)
                continue
            seen.add(source_id)
            retained.append(candidate)
        if duplicate_ids:
            self.repo.events.append(
                "RESEARCH_CANDIDATE_ITEMS_QUARANTINED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "reason_code": "DUPLICATE_SOURCE_ID",
                    "quarantined_source_ids": sorted(set(duplicate_ids)),
                    "original_candidate_count": len(candidates),
                    "retained_candidate_count": len(retained),
                },
                actor="RESEARCH_DESK",
            )
        return retained

    def normalize_request(self, request: ResearchRequest) -> NormalizedClaim:
        """Normalize one claim without retrieving or publishing evidence."""

        self._assert_requester(request.requester_id)
        self._progress_step("等待模型名额 · 主张规范化", waiting=True)
        with self._model_gate:
            return self._normalize(request)

    def _progress_step(self, detail: str, *, waiting: bool = False) -> None:
        callback = getattr(getattr(self.engine, "progress", None), "research_step", None)
        if callable(callback):
            callback("RESEARCH_DESK", detail, waiting=waiting)

    def _report_retrieval_retry(
        self,
        retry_number: int,
        maximum_retries: int,
        delay_seconds: float,
        exc: Exception,
    ) -> None:
        reporter = getattr(self.engine, "_report_retry", None)
        if callable(reporter):
            reporter(
                "RESEARCH_DESK",
                retry_number,
                maximum_retries,
                delay_seconds,
                exc,
            )

    @staticmethod
    def fingerprint(normalized_claim: NormalizedClaim) -> str:
        return ResearchDesk._fingerprint(normalized_claim)

    def _normalize(self, request: ResearchRequest) -> NormalizedClaim:
        instruction_text = (
            "本任务是将外部事实查询规范化，不判断提案优劣、不为某一立场寻找证明，也不建议投票。"
            "把请求整理为一项具体、可证伪的主张，并生成四类对冲文献检索式：支持证据、反证、"
            "适用范围限制、经典替代方案。检索式将直接发送到 OpenAlex，默认使用普通词、"
            "引号短语和必要的 AND/OR/NOT；不要把自然语言提问或 URL 参数写成检索式。"
            "默认不要使用 * 或 ? 通配符；只有准确召回词形变体确有必要时才使用，"
            "且通配符前的最后一个词段至少三个字符，不能以通配符开头。"
            "含通配符的检索式会整体改用 OpenAlex 的 search.exact（不作词干化），"
            "因此优先用明确的 OR 同义词以维持普通检索的覆盖。"
            "为缓存时效性分类：当前事实、价格、政策状态和在线服务"
            "行为使用 VOLATILE；标准、软件、API 和持续维护的文档使用 VERSIONED；基础理论、"
            "历史实验结果及其他变化缓慢的学术事实使用 STABLE。另将来源领域分类为 ACADEMIC、"
            "STANDARD_METHOD、SOFTWARE_API、CURRENT_FACT 或 GENERAL，并解释分类依据。"
            "原始论文、学术理论、数学定义与研究方法的核查应归为 ACADEMIC；即使原文以网页或 PDF 形式提供，"
            "也不要仅因载体是网页就归入 GENERAL。ACADEMIC 优先使用 OpenAlex，所选通用引擎仅用于明确授权的有界例外，"
            "不得为了获得更多候选而默认并用付费网页检索。"
            "不确定时选择适用类别中有效期较短的一类并说明原因。拒绝开放式的立场委托。只返回 JSON。"
        )
        instruction_text += self._structured_prose_contract()
        schema_text = json.dumps(
            NormalizedClaim.model_json_schema(), indent=2, ensure_ascii=False
        )
        system_text = instruction_text + "\n\n目标 JSON Schema：\n" + schema_text
        user_text = (
            "请求阶段："
            + request.stage.value
            + "\n原始主张：\n"
            + request.claim
        )
        stage = "research_claim_normalization"
        response = self._recorded_response(
            system_text=system_text,
            user_text=user_text,
            stage=stage,
        )
        if response is None:
            # Also check the schema-in-user layout under this governance prompt version.
            response = self._recorded_response(
                system_text=instruction_text,
                user_text=user_text + "\n\n目标 JSON Schema：\n" + schema_text,
                stage=stage,
            )
        if response is None:
            response = self.engine.invoke_participant(
                "RESEARCH_DESK",
                system_text=system_text,
                user_text=user_text,
                stage=stage,
                max_output_tokens=self.max_output_tokens,
            )
        normalized = self.engine.validate_structured_response(
            "RESEARCH_DESK",
            response=response,
            schema_model=NormalizedClaim,
            stage=stage,
            semantic_requirement="请求必须限定为具体主张，并包含四类对冲检索式。",
            nonblocking_quality_failure_code="RESEARCH_NORMALIZATION_SCHEMA_INVALID",
            max_output_tokens=self.max_output_tokens,
        )
        return normalized

    def _synthesize(
        self,
        request: ResearchRequest,
        normalized: NormalizedClaim,
        candidates: list[dict],
        *,
        stage: str = "research_evidence_synthesis",
    ) -> ResearchSynthesis:
        legacy_instruction_text = (
            "本任务只整理证据，不参与表决；只使用下方候选来源实际提供的材料。元数据用于识别来源，"
            "不能替代对具体内容的核查；每项具体发现都必须能在实际取得的相关摘录中定位。"
            "摘要或搜索结果若未作为 source_read.excerpts 的实际摘录提供，只能作为检索线索；"
            "若必须转述其本身的文字，应明确归属为摘要或检索摘要，不得据此推断全文方法、数值或细节。不得替会议决定应相信、"
            "提出、质疑、否决或拒绝什么。主动评估支持证据、反证、适用范围限制和经典替代方案；"
            "每项发现均引用 source_id。按来源对本主张的用途，将其分为 AUTHORITATIVE、PRIMARY、"
            "REVIEW、PROVISIONAL 或 DISCOVERY_ONLY。搜索摘要和普通发现页面属于 DISCOVERY_ONLY，"
            "不能单独支撑 SOURCE_BACKED。仅当合格来源直接支持主张时使用 SOURCE_BACKED；仅支持更窄"
            "或有条件的主张、或材料尚属初步时使用 QUALIFIED；直接证据、实质冲突的调和或检索覆盖"
            "不足时使用 UNRESOLVED。适用条件不同的反证不自动使主张成为 UNRESOLVED：应说明范围"
            "边界，并在适当时使用 QUALIFIED。仅在高质量综述或正式共识来源直接支持主张，或多项"
            "真正独立的合格来源支持且不存在同一实质范围内的重大反证时，才将文献共识标为 CLEAR。"
            "存在适用范围边界或轻微争议的广泛一致标为 QUALIFIED；合格来源在同一实质范围内有重大"
            "冲突标为 MIXED；来源质量、独立性、数量或覆盖范围不足以确立领域共识时标为 INSUFFICIENT。"
            "单项原始研究可以支持 SOURCE_BACKED，但不足以单独确立 CLEAR 共识。分别评价三个独立"
            "的 confidence 维度：coverage 衡量数据库、检索式、来源类型和反向检索的覆盖广度；"
            "source_quality 衡量权威性、原创性、审阅状态和来源可获取性；literature_consistency 衡量"
            "同一实质范围内合格来源的一致程度。不得计算总分，也不得从其他维度、knowledge_status 或"
            "共识状态推导任何一个维度。confidence 绝不是某位代表正确的概率。将筛选决定放在私有"
            "字段中，只返回 JSON。"
        )
        # The legacy prompt layout below is also used for interrupted-call
        # recovery; keep its natural-language constraints current as well.
        legacy_instruction_text += self._structured_prose_contract()
        source_boundary_warning = (
            "\n\n【严格来源边界；违反即退回本条证据综合】候选来源不是写作提示，而是唯一可引用的"
            "来源集合。packet.sources、四类发现中的 source_id 和 screening_decisions 只能使用"
            "下方候选来源 ID；packet.sources 不得重复同一 ID，筛选决定须对每个候选恰好一条。"
            "不得凭模型记忆补写论文、DOI、网址、摘要或引文，也不得把候选来源没有支持的"
            "说法包装成有来源的结论。没有可用证据就如实写证据不足或未解决，不能为凑齐"
            "SOURCE_BACKED/CLEAR 而编造来源。被校验打回时，以具体违规位置和候选清单为准；"
            "删除不合格来源及其依赖的发现、必要时下调知识或共识状态，不得以‘保留实质内容’"
            "为由保留无依据断言。"
        )
        if any(item.get("retrieval_backend_id") == "human_reference" for item in candidates):
            source_boundary_warning += (
                "人类提供的文件只是候选来源；上传行为不证明其真实性、权威性、同行评审状态或"
                "对当前命题的相关性。逐项核对文件摘录实际陈述的事实和适用条件，不能仅凭文件名"
                "或人类提供这一事实将其标为支持证据。"
            )
        writing_preferences = self.repo.root / "public/literature_report/writing_preferences.json"
        if writing_preferences.is_file() and json.loads(
            writing_preferences.read_text(encoding="utf-8")
        ).get("fact_first_writing"):
            source_boundary_warning += (
                "EvidenceFinding.evidence_summary 必须先说来源实际支持的具体观察、定义、"
                "数值结果或论证，而非介绍论文题名、发布机构或检索记录。"
                "applicability 与 limitations 分别说明适用对象、方法、条件和限制。"
                "若候选只有题名或模糊摘要，无法核对具体发现，就不要填写细节性发现；"
                "在 unresolved_questions 和检索覆盖说明中记录缺口。"
                "书目信息只放 sources，不把来源清单写成发现。"
            )
        instruction_text = legacy_instruction_text + source_boundary_warning
        instruction_text += (
            "\n\n【原文核验】候选中的 source_read.excerpts 是从选定网页或原件抽取的"
            "有出处文字；READABLE_EXCERPT 只证明这些摘录可读取，不证明整篇已审查。"
            "任何具体数值、定义、方法条件、条款或例外均须能由相关摘录直接支持。"
            "没有可核对摘录的来源仍可作为检索线索，但不得据其标题或摘要编造细节，"
            "也不要将其列为 AUTHORITATIVE、PRIMARY 或 REVIEW 的直接支持。"
            "如果原网址受阻、原件无可提取文字，或检索结果没有可读链接，系统可能用题名、"
            "DOI 等标识追加检索其他发布页或 PDF。标记为 UNVERIFIED_ALTERNATIVE 的结果"
            "只是新候选：逐一核对题名、作者或发布机构、版本／日期、标识和相关正文；"
            "不得自动把不同网址视为同一文件、同一版本或同等权威。若无法确认对应关系，"
            "保留原件不可访问的限制；使用替代来源时引用它自己的 source_id，并说明"
            "实际核对到的版本和适用范围。"
        )
        schema_text = json.dumps(
            ResearchSynthesis.model_json_schema(), indent=2, ensure_ascii=False
        )
        fixed_output_requirements = (
            "packet_id 使用占位值 'PENDING'，claim_fingerprint 使用 64 个零，retrieved_at 使用"
            "当前 UTC 时间；这三个字段都会由编排器替换。"
        )
        system_text = (
            instruction_text
            + "\n\n目标 JSON Schema：\n"
            + schema_text
            + "\n\n固定输出要求：\n"
            + fixed_output_requirements
        )
        base_user_text = (
            "原始主张：\n"
            + request.claim
            + "\n\n规范化主张：\n"
            + json.dumps(normalized.model_dump(mode="json"), indent=2, ensure_ascii=False)
            + "\n\n候选来源：\n"
            + json.dumps(candidates, indent=2, ensure_ascii=False)
        )
        user_text = (
            base_user_text
            + "\n\n允许引用的候选 source_id（除此之外一律不得引用）：\n"
            + json.dumps(
                [str(candidate["source_id"]) for candidate in candidates],
                ensure_ascii=False,
            )
        )
        response = self._recorded_response(
            system_text=system_text,
            user_text=user_text,
            stage=stage,
        )
        if response is None and stage == "research_evidence_synthesis":
            # A running meeting may have a durable pre-warning answer. Reuse it
            # without reissuing the costly synthesis request on resume.
            response = self._recorded_response(
                system_text=(
                    legacy_instruction_text
                    + "\n\n目标 JSON Schema：\n"
                    + schema_text
                    + "\n\n固定输出要求：\n"
                    + fixed_output_requirements
                ),
                user_text=base_user_text,
                stage=stage,
            )
            if response is None:
                response = self._recorded_response(
                    system_text=legacy_instruction_text,
                    user_text=(
                        base_user_text
                        + "\n\n目标 JSON Schema：\n"
                        + schema_text
                        + "\n"
                        + fixed_output_requirements
                    ),
                    stage=stage,
                )
        if response is None:
            response = self.engine.invoke_participant(
                "RESEARCH_DESK",
                system_text=system_text,
                user_text=user_text,
                stage=stage,
                max_output_tokens=self.max_output_tokens,
            )
        else:
            progress = getattr(self.engine, "progress", None)
            info = getattr(progress, "info", None)
            if callable(info):
                info(f"RESEARCH_DESK · 已恢复落盘的 {stage} 响应")
        synthesis = self.engine.validate_structured_response(
            "RESEARCH_DESK",
            response=response,
            schema_model=ResearchSynthesis,
            stage=stage,
            semantic_requirement=(
                "发现必须有候选来源支撑；筛选须考虑支持、反证、限制和替代方案，不能作出实质决策。"
            ),
            repair_guidance=(
                "以下是本次检索唯一允许的 source_id："
                + json.dumps(
                    [str(candidate["source_id"]) for candidate in candidates],
                    ensure_ascii=False,
                )
                + "。删除所有越界、重复或未列入 packet.sources 的引用及依赖它们的断言；"
                "不要凭记忆改写为另一个 URL/DOI。需要时将 knowledge_status 降为 UNRESOLVED、"
                "consensus 降为 INSUFFICIENT，并写明缺少哪类证据。"
            ),
            repair_diagnostic=lambda raw: self._synthesis_source_diagnostic(
                raw, candidates
            ),
            nonblocking_quality_failure_code="RESEARCH_SYNTHESIS_SCHEMA_INVALID",
            max_output_tokens=self.max_output_tokens,
        )
        synthesis = self._remove_unretrieved_sources(
            synthesis=synthesis,
            candidates=candidates,
        )
        return self._reconcile_screening_decisions(
            synthesis=synthesis,
            request=request,
            normalized=normalized,
            candidates=candidates,
        )

    @staticmethod
    def _synthesis_source_diagnostic(raw: str, candidates: list[dict]) -> str:
        """Name citation boundary defects so a repair is more than an error code."""

        try:
            output = parse_json_object(raw)
        except (ValueError, TypeError):
            return "原输出无法解析为 JSON；先修复语法，再逐一核对来源 ID。"
        if not isinstance(output, dict):
            return "原输出不是 JSON 对象；无法定位来源字段。"
        packet = output.get("packet")
        if not isinstance(packet, dict):
            return "缺少 packet 对象；无法核对引用字段。"

        allowed = {str(candidate["source_id"]) for candidate in candidates}
        issues: list[str] = []
        sources = packet.get("sources", [])
        if not isinstance(sources, list):
            return "packet.sources 不是数组；无法核对来源 ID。"
        source_ids: list[str] = []
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                issues.append(f"packet.sources[{index}] 不是来源对象。")
                continue
            source_id = str(source.get("source_id") or "")
            source_ids.append(source_id)
            if source_id not in allowed:
                issues.append(
                    f"packet.sources[{index}].source_id={source_id!r}：检索器未返回该 ID；"
                    "必须删除该来源及其依赖的发现，不得自行补造出处。"
                )
            if source_id in source_ids[:-1]:
                issues.append(
                    f"packet.sources[{index}].source_id={source_id!r}：同一来源重复；"
                    "只能保留一条。"
                )
        listed = set(source_ids)
        for field in (
            "supporting_evidence",
            "contradictory_evidence",
            "scope_limitations",
            "canonical_alternatives",
        ):
            findings = packet.get(field, [])
            if not isinstance(findings, list):
                issues.append(f"packet.{field} 不是数组。")
                continue
            for index, finding in enumerate(findings):
                if not isinstance(finding, dict):
                    issues.append(f"packet.{field}[{index}] 不是发现对象。")
                    continue
                source_id = str(finding.get("source_id") or "")
                if source_id not in allowed:
                    issues.append(
                        f"packet.{field}[{index}].source_id={source_id!r}：候选集合之外的引用；"
                        "删除该发现，不得沿用其结论。"
                    )
                elif source_id not in listed:
                    issues.append(
                        f"packet.{field}[{index}].source_id={source_id!r}：候选来源虽存在，"
                        "但未列入 packet.sources；只能在核实对应来源后补入，否则删除发现。"
                    )
        decisions = output.get("screening_decisions", [])
        if isinstance(decisions, list):
            decision_ids = [
                str(item.get("source_id") or "")
                for item in decisions
                if isinstance(item, dict)
            ]
            counts = Counter(decision_ids)
            for source_id, count in counts.items():
                if source_id not in allowed:
                    issues.append(
                        f"screening_decisions.source_id={source_id!r}：不是本次候选来源。"
                    )
                elif count > 1:
                    issues.append(
                        f"screening_decisions.source_id={source_id!r}：出现 {count} 次，"
                        "每个候选来源只能有一条筛选决定。"
                    )
            missing = sorted(allowed - set(decision_ids))
            if missing:
                issues.append(
                    "screening_decisions 缺少候选来源的决定："
                    + json.dumps(missing, ensure_ascii=False)
                )
        else:
            issues.append("screening_decisions 不是数组。")
        if not issues:
            return (
                "未发现可机械定位的来源 ID 错误；请依照上面的 Pydantic 校验信息检查"
                "字段类型、证据适用范围和共识/知识状态，不得增造来源。"
            )
        return "\n".join(issues[:40]) + (
            f"\n另有 {len(issues) - 40} 条同类问题；逐项检查全部来源字段。"
            if len(issues) > 40
            else ""
        )

    def _remove_unretrieved_sources(
        self,
        *,
        synthesis: ResearchSynthesis,
        candidates: list[dict],
    ) -> ResearchSynthesis:
        """Remove model-prior citations unless the retriever supplied their source IDs."""

        candidate_ids = {str(item["source_id"]) for item in candidates}
        invalid_ids = {
            source.source_id
            for source in synthesis.packet.sources
            if source.source_id not in candidate_ids
        }
        if not invalid_ids:
            return synthesis

        finding_fields = (
            "supporting_evidence",
            "contradictory_evidence",
            "scope_limitations",
            "canonical_alternatives",
        )
        removed_finding_counts = {
            field: sum(
                finding.source_id in invalid_ids
                for finding in getattr(synthesis.packet, field)
            )
            for field in finding_fields
        }
        packet_update = {
            "sources": [
                source
                for source in synthesis.packet.sources
                if source.source_id not in invalid_ids
            ],
            **{
                field: [
                    finding
                    for finding in getattr(synthesis.packet, field)
                    if finding.source_id not in invalid_ids
                ]
                for field in finding_fields
            },
        }
        try:
            packet = EvidencePacket.model_validate(
                synthesis.packet.model_copy(update=packet_update).model_dump(mode="python")
            )
        except ValueError:
            return self._repair_source_bounded_synthesis(
                synthesis=synthesis,
                candidates=candidates,
                invalid_ids=invalid_ids,
            )

        self.repo.events.append(
            "RESEARCH_UNRETRIEVED_SOURCES_REMOVED",
            {
                "meeting_id": self.repo.meeting_id,
                "removed_source_ids": sorted(invalid_ids),
                "removed_finding_counts": removed_finding_counts,
                "remaining_source_count": len(packet.sources),
                "repair_mode": "DETERMINISTIC_REMOVAL_AND_REVALIDATION",
            },
            actor="RESEARCH_DESK",
        )
        return synthesis.model_copy(update={"packet": packet})

    def _repair_source_bounded_synthesis(
        self,
        *,
        synthesis: ResearchSynthesis,
        candidates: list[dict],
        invalid_ids: set[str],
    ) -> ResearchSynthesis:
        """Repair dependent conclusions only when removing an ungrounded source breaks validity."""

        schema_text = json.dumps(
            ResearchSynthesis.model_json_schema(), indent=2, ensure_ascii=False
        )
        system_text = (
            "本任务只修复 Research Desk 综合结果的来源边界。移除 source_id 不在允许列表中的所有"
            "来源及其发现。除非 schema 的不变量要求连带修改，否则保留其余来源记录、发现、筛选"
            "决定和措辞。如果移除后缺少支撑 knowledge_status 或共识状态所必需的证据，应审慎"
            "降级相应字段并说明仍未解决的问题。不得引入替代来源、新证据或新的实质主张。只返回 JSON。"
            "\n\n目标 JSON Schema：\n"
            + schema_text
            + self._structured_prose_contract()
        )
        allowed_ids = [str(item["source_id"]) for item in candidates]
        user_text = (
            "须移除的无效来源 ID：\n"
            + json.dumps(sorted(invalid_ids), indent=2, ensure_ascii=False)
            + "\n\n允许的来源 ID：\n"
            + json.dumps(allowed_ids, indent=2, ensure_ascii=False)
            + "\n\n待修复的综合结果：\n"
            + synthesis.model_dump_json(indent=2)
        )
        stage = "research_source_bounded_synthesis_repair"
        self.repo.events.append(
            "RESEARCH_SOURCE_BOUNDARY_REPAIR_REQUESTED",
            {
                "meeting_id": self.repo.meeting_id,
                "invalid_source_ids": sorted(invalid_ids),
                "allowed_source_count": len(allowed_ids),
            },
            actor="RESEARCH_DESK",
        )
        response = self._recorded_response(
            system_text=system_text,
            user_text=user_text,
            stage=stage,
        )
        if response is None:
            response = self.engine.invoke_participant(
                "RESEARCH_DESK",
                system_text=system_text,
                user_text=user_text,
                stage=stage,
                max_output_tokens=self.max_output_tokens,
            )
        repaired = self.engine.validate_structured_response(
            "RESEARCH_DESK",
            response=response,
            schema_model=ResearchSynthesis,
            stage=stage,
            semantic_requirement=(
                "证据包中的每个 source_id 都必须在允许的来源 ID 列表内；移除无效来源及其发现，"
                "不得添加证据。"
            ),
            repair_guidance=(
                "只允许以下候选 source_id："
                + json.dumps(allowed_ids, ensure_ascii=False)
                + "。删除其余来源、发现和筛选决定；不得保留或凭记忆替换无依据断言。"
            ),
            repair_diagnostic=lambda raw: self._synthesis_source_diagnostic(
                raw, candidates
            ),
            nonblocking_quality_failure_code="RESEARCH_SOURCE_BOUNDARY_REPAIR_SCHEMA_INVALID",
            max_output_tokens=self.max_output_tokens,
        )
        remaining_invalid = {
            source.source_id
            for source in repaired.packet.sources
            if source.source_id not in set(allowed_ids)
        }
        if remaining_invalid:
            raise ResearchQualityControlError(
                "RESEARCH_SOURCE_BOUNDARY_REPAIR_FAILED",
                "Research Desk source-boundary repair retained unretrieved sources: "
                + ", ".join(sorted(remaining_invalid)),
            )
        self.repo.events.append(
            "RESEARCH_SOURCE_BOUNDARY_REPAIR_SUCCEEDED",
            {
                "meeting_id": self.repo.meeting_id,
                "removed_source_ids": sorted(invalid_ids),
                "remaining_source_count": len(repaired.packet.sources),
                "repair_mode": "MODEL_DEPENDENCY_REPAIR",
            },
            actor="RESEARCH_DESK",
        )
        return repaired

    def _recorded_response(self, *, system_text: str, user_text: str, stage: str):
        """Use durable provider output when the engine supports interrupted-call recovery."""

        finder = getattr(self.engine, "find_recorded_response", None)
        if not callable(finder):
            return None
        return finder(
            "RESEARCH_DESK",
            system_text=system_text,
            user_text=user_text,
            stage=stage,
        )

    def _reconcile_screening_decisions(
        self,
        *,
        synthesis: ResearchSynthesis,
        request: ResearchRequest,
        normalized: NormalizedClaim,
        candidates: list[dict],
    ) -> ResearchSynthesis:
        """Complete only missing/duplicated rows instead of regenerating a long packet."""

        candidate_ids = [str(item["source_id"]) for item in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ResearchQualityControlError(
                "RESEARCH_RETRIEVER_DUPLICATE_SOURCE_IDS",
                "Research retriever returned duplicate candidate source_id values",
            )

        candidate_id_set = set(candidate_ids)
        decisions_by_id: dict[str, list[ScreeningDecision]] = {}
        extra_ids: list[str] = []
        for decision in synthesis.screening_decisions:
            if decision.source_id not in candidate_id_set:
                extra_ids.append(decision.source_id)
                continue
            decisions_by_id.setdefault(decision.source_id, []).append(decision)

        missing_ids = [source_id for source_id in candidate_ids if source_id not in decisions_by_id]
        duplicate_ids = [
            source_id
            for source_id in candidate_ids
            if len(decisions_by_id.get(source_id, [])) > 1
        ]
        repair_id_set = set(missing_ids) | set(duplicate_ids)
        repair_ids = [source_id for source_id in candidate_ids if source_id in repair_id_set]
        repaired_by_id: dict[str, ScreeningDecision] = {}

        if repair_ids:
            packet_source_ids = {source.source_id for source in synthesis.packet.sources}
            repair_candidates = []
            for item in candidates:
                source_id = str(item["source_id"])
                if source_id not in repair_id_set:
                    continue
                annotated = dict(item)
                annotated["already_in_evidence_packet"] = source_id in packet_source_ids
                repair_candidates.append(annotated)
            instruction_text = (
                "本任务只补齐 Research Desk 来源筛选记录：对每个给定的候选 source_id 恰好返回一条"
                "简洁的筛选决定，不得返回其他 source_id。判断候选来源是否属于已完成的证据综合。"
                "由于证据包已经冻结，included 字段必须等于给定的 already_in_evidence_packet 值。"
                "理由仅依据给定元数据，用一句简短的话说明。不得修改或重现证据包。只返回 JSON。"
            )
            instruction_text += self._structured_prose_contract()
            schema_text = json.dumps(
                ScreeningDecisionSupplement.model_json_schema(),
                indent=2,
                ensure_ascii=False,
            )
            system_text = instruction_text + "\n\n目标 JSON Schema：\n" + schema_text
            user_text = (
                "原始主张：\n"
                + request.claim
                + "\n\n规范化主张：\n"
                + normalized.normalized_claim
                + "\n\n待决定的候选来源：\n"
                + json.dumps(repair_candidates, indent=2, ensure_ascii=False)
                + "\n\n必须逐一处理的来源 ID（每项恰好一次）：\n"
                + json.dumps(repair_ids, indent=2, ensure_ascii=False)
            )
            repair_stage = "research_screening_decision_repair"
            response = self._recorded_response(
                system_text=system_text,
                user_text=user_text,
                stage=repair_stage,
            )
            if response is None:
                response = self._recorded_response(
                    system_text=instruction_text,
                    user_text=user_text + "\n\n目标 JSON Schema：\n" + schema_text,
                    stage=repair_stage,
                )
            if response is None:
                response = self.engine.invoke_participant(
                    "RESEARCH_DESK",
                    system_text=system_text,
                    user_text=user_text,
                    stage=repair_stage,
                    max_output_tokens=self.max_output_tokens,
                )
            try:
                supplement = self.engine.validate_structured_response(
                    "RESEARCH_DESK",
                    response=response,
                    schema_model=ScreeningDecisionSupplement,
                    stage=repair_stage,
                    semantic_requirement=(
                        "对每个必需的来源 ID 恰好返回一条筛选决定；不得重复，也不得加入未请求的来源 ID。"
                    ),
                    nonblocking_quality_failure_code="RESEARCH_SCREENING_REPAIR_SCHEMA_INVALID",
                    max_output_tokens=self.max_output_tokens,
                )
            except ResearchQualityControlError as exc:
                return self._quarantine_screening_items(
                    synthesis=synthesis,
                    candidate_ids=candidate_ids,
                    quarantine_ids=repair_ids,
                    reason_code=exc.code,
                )
            returned_ids = [item.source_id for item in supplement.screening_decisions]
            if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != repair_id_set:
                return self._quarantine_screening_items(
                    synthesis=synthesis,
                    candidate_ids=candidate_ids,
                    quarantine_ids=repair_ids,
                    reason_code="RESEARCH_SCREENING_REPAIR_INCOMPLETE",
                )
            repaired_by_id = {item.source_id: item for item in supplement.screening_decisions}
            if any(
                repaired_by_id[source_id].included != (source_id in packet_source_ids)
                for source_id in repair_ids
            ):
                return self._quarantine_screening_items(
                    synthesis=synthesis,
                    candidate_ids=candidate_ids,
                    quarantine_ids=repair_ids,
                    reason_code="RESEARCH_SCREENING_REPAIR_CHANGED_PACKET_MEMBERSHIP",
                )

        merged = [
            repaired_by_id[source_id]
            if source_id in repaired_by_id
            else decisions_by_id[source_id][0]
            for source_id in candidate_ids
        ]
        if repair_ids or extra_ids:
            self.repo.events.append(
                "RESEARCH_SCREENING_DECISIONS_RECONCILED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "candidate_source_count": len(candidate_ids),
                    "original_decision_count": len(synthesis.screening_decisions),
                    "repaired_source_ids": repair_ids,
                    "missing_source_ids": missing_ids,
                    "duplicate_source_ids": duplicate_ids,
                    "discarded_unrequested_source_ids": sorted(set(extra_ids)),
                },
                actor="RESEARCH_DESK",
            )
        return synthesis.model_copy(update={"screening_decisions": merged})

    def _quarantine_screening_items(
        self,
        *,
        synthesis: ResearchSynthesis,
        candidate_ids: list[str],
        quarantine_ids: list[str],
        reason_code: str,
    ) -> ResearchSynthesis:
        """Exclude defective screening rows and their dependent evidence, not the whole packet."""

        quarantine_set = set(quarantine_ids)
        finding_fields = (
            "supporting_evidence",
            "contradictory_evidence",
            "scope_limitations",
            "canonical_alternatives",
        )
        packet_update = {
            "sources": [
                source
                for source in synthesis.packet.sources
                if source.source_id not in quarantine_set
            ],
            **{
                field: [
                    finding
                    for finding in getattr(synthesis.packet, field)
                    if finding.source_id not in quarantine_set
                ]
                for field in finding_fields
            },
        }
        try:
            packet = EvidencePacket.model_validate(
                synthesis.packet.model_copy(update=packet_update).model_dump(mode="python")
            )
        except ValueError as exc:
            raise ResearchQualityControlError(
                "RESEARCH_ITEM_QUARANTINE_INVALIDATED_PACKET",
                "Quarantining defective screening items removed evidence required for a valid packet",
            ) from exc

        existing: dict[str, ScreeningDecision] = {}
        duplicates: set[str] = set()
        for decision in synthesis.screening_decisions:
            if decision.source_id in existing:
                duplicates.add(decision.source_id)
            else:
                existing[decision.source_id] = decision
        decisions = []
        for source_id in candidate_ids:
            if source_id in quarantine_set:
                decisions.append(
                    ScreeningDecision(
                        source_id=source_id,
                        included=False,
                        rationale=f"QC quarantine: {reason_code}.",
                    )
                )
            elif source_id in existing and source_id not in duplicates:
                decisions.append(existing[source_id])
            else:
                raise ResearchQualityControlError(
                    "RESEARCH_SCREENING_LEDGER_NOT_ISOLATABLE",
                    "A screening ledger defect remained outside the quarantined source items",
                )
        self.repo.events.append(
            "RESEARCH_SCREENING_ITEMS_QUARANTINED",
            {
                "meeting_id": self.repo.meeting_id,
                "reason_code": reason_code,
                "quarantined_source_ids": quarantine_ids,
                "removed_packet_source_count": sum(
                    source.source_id in quarantine_set
                    for source in synthesis.packet.sources
                ),
                "remaining_packet_source_count": len(packet.sources),
                "meeting_continues": True,
            },
            actor="RESEARCH_DESK",
        )
        return synthesis.model_copy(
            update={"packet": packet, "screening_decisions": decisions}
        )

    def _matching_cached_packets(
        self, fingerprint: str
    ) -> list[tuple[datetime, Path, EvidencePacket]]:
        root = self.repo.root / "public/research/evidence_packets"
        if not root.exists():
            return []
        matches: list[tuple[datetime, Path, EvidencePacket]] = []
        for path in root.glob("RP-*.json"):
            try:
                packet = EvidencePacket.model_validate_json(path.read_text(encoding="utf-8"))
                if packet.claim_fingerprint != fingerprint:
                    continue
                matches.append((packet.retrieved_at, path.relative_to(self.repo.root), packet))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(matches, key=lambda item: item[0], reverse=True)

    def _fresh_cached_packet(
        self, matching_packets: list[tuple[datetime, Path, EvidencePacket]]
    ) -> tuple[Path, EvidencePacket] | None:
        now = datetime.now(timezone.utc)
        for _, path, packet in matching_packets:
            if self.packet_cache.is_invalidated(packet.packet_id):
                continue
            # Existing packets remain immutable, but cannot satisfy a new
            # request without the now-required original-source reading pass.
            if not packet.source_reading_performed or not packet.source_recovery_performed:
                continue
            # A new access grant permits a new bounded pass on previously
            # unresolved evidence, without rewriting the original packet.
            if (institutional_access_allowed(self.repo) and not packet.institutional_access_allowed
                    and packet.knowledge_status in {KnowledgeStatus.UNRESOLVED, KnowledgeStatus.QUALIFIED}):
                continue
            if packet.cache_expires_at is not None and now <= packet.cache_expires_at:
                return path, packet
        return None

    @staticmethod
    def _validate_adversarial_query_trace(query_trace: list[dict]) -> None:
        required = {"supporting", "contradictory", "limitations", "alternatives"}
        observed = {
            str(item.get("purpose"))
            for item in query_trace
            if isinstance(item, dict) and str(item.get("query") or "").strip()
        }
        missing = sorted(required - observed)
        if missing:
            raise ResearchQualityControlError(
                "RESEARCH_ADVERSARIAL_RETRIEVAL_INCOMPLETE",
                "Research Desk retrieval is incomplete; missing adversarial query purposes: "
                + ", ".join(missing),
            )

    @staticmethod
    def _validate_source_bounded(
        synthesis: ResearchSynthesis,
        candidates: list[dict],
    ) -> None:
        candidate_ids = {str(item["source_id"]) for item in candidates}
        packet_ids = {source.source_id for source in synthesis.packet.sources}
        decision_ids = [decision.source_id for decision in synthesis.screening_decisions]
        if not packet_ids <= candidate_ids:
            raise ResearchQualityControlError(
                "RESEARCH_SYNTHESIS_UNRETRIEVED_SOURCE",
                "Research Desk synthesis cited sources that were not returned by the retriever",
            )
        if len(decision_ids) != len(set(decision_ids)) or set(decision_ids) != candidate_ids:
            raise ResearchQualityControlError(
                "RESEARCH_SCREENING_LEDGER_INCOMPLETE",
                "Research Desk synthesis must record exactly one screening decision per candidate source",
            )
        if not candidates and synthesis.packet.knowledge_status.value != "UNRESOLVED":
            raise ResearchQualityControlError(
                "RESEARCH_EMPTY_RETRIEVAL_NOT_UNRESOLVED",
                "An empty retrieval can only produce UNRESOLVED knowledge status",
            )

    @staticmethod
    def _fingerprint(claim: NormalizedClaim) -> str:
        normalized = unicodedata.normalize("NFKC", claim.normalized_claim).casefold().strip()
        scope = sorted(unicodedata.normalize("NFKC", x).casefold().strip() for x in claim.scope_terms)
        material = json.dumps({"claim": normalized, "scope": scope}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _assert_enabled(self) -> None:
        path = self.repo.root / "identity_private/meeting_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not manifest.get("research_enabled", False):
            raise ValueError("Research Desk is disabled for this meeting")
        if manifest.get("research_model") is None:
            raise ValueError("Research Desk is enabled but has no configured model")

    def _assert_requester(self, requester_id: str) -> None:
        # Governance-mandated freshness checks and model-prior verification may
        # be initiated by the shared service itself.  This is infrastructure,
        # not a new voting participant.
        if requester_id == "RESEARCH_DESK":
            return
        manifest = json.loads(
            (self.repo.root / "identity_private/meeting_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        if manifest.get("meeting_type") == "research" and requester_id == "HUMAN":
            return
        # The v0.7.1 academic Writer is a configured meeting participant but
        # does not appear in the representative registry. Its model-prior
        # checks use the same audited Research Desk pathway as representatives.
        if (requester_id == "WRITER"
                and manifest.get("deliverable_type") == "literature_review"
                and manifest.get("literature_writing_policy") in {"v071", "fast"}
                and manifest.get("writer_model")):
            return
        paths = [
            self.repo.root / "identity_private/representative_registry.json",
            self.repo.root / "identity_private/audit_member_registry.json",
        ]
        for path in paths:
            if not path.exists():
                continue
            records = json.loads(path.read_text(encoding="utf-8"))
            if any(
                record.get("representative_id") == requester_id
                or record.get("audit_member_id") == requester_id
                for record in records
            ):
                return
        raise ValueError(f"Research Desk requester is not a registered meeting participant: {requester_id}")

    def _record_request(
        self,
        *,
        request_id: str,
        request: ResearchRequest,
        normalized: NormalizedClaim,
        fingerprint: str,
        cache_hit: bool,
        packet_path: Path | None,
        status: str,
    ) -> None:
        path = Path("audit_private/research/requests") / f"{request_id}.json"
        self.repo.docs.write_once(
            path,
            json.dumps(
                {
                    "request_id": request_id,
                    "request": request.model_dump(mode="json"),
                    "normalized_claim": normalized.model_dump(mode="json"),
                    "claim_fingerprint": fingerprint,
                    "cache_hit": cache_hit,
                    "status": status,
                    "packet_path": str(packet_path) if packet_path is not None else None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
