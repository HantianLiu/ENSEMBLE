"""Exploratory literature search for report planning, separate from claim QC.

An exploratory answer is a source-linked research map, not an EvidencePacket
or a SOURCE_BACKED verdict. Search traces stay private; the answer and source
catalog are released only at the relevant planning barrier.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from project_ensemble.errors import (
    EmptyModelOutputError,
    InputContextLimitError,
    OutputLimitReachedError,
    RepresentativeUnavailableError,
    TransientProviderError,
)
from project_ensemble.research.models import EvidenceSource, EvidenceUseClass, FreshnessClass
from project_ensemble.research.retrievers import coerce_retrieval_result


class ResearchExplorationService:
    """Meeting-local exploratory research with replay, cache, and publication."""

    def __init__(self, desk, *, max_output_tokens: int | None = None):
        self.desk = desk
        self.repo = desk.repo
        self.engine = desk.engine
        self.max_output_tokens = max_output_tokens
        self._locks_guard = threading.Lock()
        self._query_locks: dict[str, threading.Lock] = {}

    @staticmethod
    def _fingerprint(question: str, queries: list[str]) -> str:
        normalized = {
            "question": " ".join(question.casefold().split()),
            "queries": [" ".join(query.casefold().split()) for query in queries],
        }
        return hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _lock_for(self, fingerprint: str) -> threading.Lock:
        with self._locks_guard:
            return self._query_locks.setdefault(fingerprint, threading.Lock())

    def searches_needed(
        self, question: str, search_queries: list[str], freshness_class: FreshnessClass
    ) -> int:
        """Estimate quota consumption; a fresh meeting-local cache hit costs zero."""
        fingerprint = self._fingerprint(question, search_queries)
        cache_root = (
            self.repo.root / "governance_private/literature_report/exploration/cache"
            / fingerprint
        )
        return 0 if self._fresh_cache(cache_root, freshness_class) is not None else len(search_queries)

    def answer(
        self,
        *,
        requester_id: str,
        cycle: int,
        turn: int,
        question_number: int,
        question: str,
        search_queries: list[str],
        freshness_class: FreshnessClass = FreshnessClass.VERSIONED,
    ) -> dict:
        """Answer one question; caller accounts for the consumed logical searches."""
        if not question.strip() or not search_queries or any(not item.strip() for item in search_queries):
            raise ValueError("exploration requires a question and nonempty search queries")
        safe_requester = re.sub(r"[^A-Za-z0-9_-]", "_", requester_id)
        answer_id = f"C{cycle:03d}-{safe_requester}-T{turn:02d}-Q{question_number:02d}"
        private_relative = (
            Path("governance_private/literature_report/exploration/answers")
            / f"{answer_id}.json"
        )
        private_path = self.repo.root / private_relative
        if private_path.exists():
            return json.loads(private_path.read_text(encoding="utf-8"))
        fingerprint = self._fingerprint(question, search_queries)
        with self._lock_for(fingerprint):
            if private_path.exists():
                return json.loads(private_path.read_text(encoding="utf-8"))
            cache_root = (
                self.repo.root
                / "governance_private/literature_report/exploration/cache"
                / fingerprint
            )
            cached = self._fresh_cache(cache_root, freshness_class)
            if cached is None:
                core = self._search_and_summarize(
                    answer_id=answer_id,
                    question=question,
                    search_queries=search_queries,
                    freshness_class=freshness_class,
                )
                self.repo.docs.write_once(
                    (cache_root / f"{answer_id}.json").relative_to(self.repo.root),
                    json.dumps(core, ensure_ascii=False, indent=2),
                )
                searches_used = len(search_queries)
                cache_hit = False
            else:
                core = cached
                # A crash after writing this answer's cache entry but before
                # writing its requester record is a replay, not a free hit.
                original_call = (cache_root / f"{answer_id}.json").exists()
                searches_used = len(search_queries) if original_call else 0
                cache_hit = not original_call
            record = {
                **core,
                "answer_id": answer_id,
                "requester_id": requester_id,
                "cycle": cycle,
                "turn": turn,
                "question_number": question_number,
                "searches_used": searches_used,
                "cache_hit": cache_hit,
                "freshness_class": freshness_class.value,
            }
            self.repo.docs.write_once(
                private_relative, json.dumps(record, ensure_ascii=False, indent=2)
            )
            self.repo.events.append(
                "RESEARCH_EXPLORATION_ANSWER_STAGED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "answer_id": answer_id,
                    "requester_id": requester_id,
                    "searches_used": searches_used,
                    "cache_hit": cache_hit,
                    "status": core["status"],
                },
                actor="RESEARCH_DESK",
            )
            return record

    def _fresh_cache(self, root: Path, freshness_class: FreshnessClass) -> dict | None:
        if not root.exists():
            return None
        for path in sorted(root.glob("*.json"), reverse=True):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("status") not in {"ANSWERED", "WEAK_EVIDENCE"}:
                continue
            if "readable_original_excerpt_count" not in record.get("retrieval_conditions", {}):
                # Older cached answers used search metadata only. Keep them
                # frozen, but make the next request use the new reading path.
                continue
            if not record.get("retrieval_conditions", {}).get("source_recovery_performed"):
                continue
            if not record.get("retrieval_conditions", {}).get("backend_ids"):
                # A transient backend outage is not reusable literature evidence.
                continue
            retrieved_at = datetime.fromisoformat(record["retrieved_at"])
            max_age = self.desk.freshness_windows[freshness_class]
            if datetime.now(timezone.utc) - retrieved_at <= timedelta(days=max_age):
                return record
        return None

    def _search_and_summarize(
        self,
        *,
        answer_id: str,
        question: str,
        search_queries: list[str],
        freshness_class: FreshnessClass,
    ) -> dict:
        audit_relative = (
            Path("audit_private/literature_report/exploration") / f"{answer_id}.json"
        )
        audit_path = self.repo.root / audit_relative
        if audit_path.exists():
            staged = json.loads(audit_path.read_text(encoding="utf-8"))
            if "core" in staged:
                return staged["core"]
        candidates: dict[str, dict] = {}
        query_trace: list[dict] = []
        successful_backends: list[str] = []
        failed_backends: list[str] = []
        for query in search_queries:
            try:
                result = coerce_retrieval_result(
                    self.desk.retriever.retrieve_exploratory(query),
                    default_backend_ids=getattr(
                        self.desk.retriever, "backend_ids", (type(self.desk.retriever).__name__,)
                    ),
                )
            except (TransientProviderError, AttributeError) as exc:
                failed_backends.extend(getattr(
                    self.desk.retriever, "backend_ids", (type(self.desk.retriever).__name__,)
                ))
                query_trace.append({
                    "query": query,
                    "status": "RETRIEVAL_UNAVAILABLE",
                    "error_type": type(exc).__name__,
                    "error_summary": str(exc)[:500],
                    "returned_source_ids": [],
                })
                continue
            query_trace.extend(result.query_trace)
            successful_backends.extend(result.effective_backend_ids)
            failed_backends.extend(result.failed_backend_ids)
            for candidate in result.candidates:
                candidates.setdefault(str(candidate["source_id"]), candidate)
        all_candidates = list(candidates.values())
        # The full set and rejection-free candidate history stay in the audit
        # layer. A bounded source catalog prevents prompt growth across turns.
        model_candidates, source_reading = self.desk.source_reader.prepare(
            all_candidates[:120], question=question, request_key=answer_id,
        )
        model_catalog = [
            {
                "source_id": item["source_id"],
                "title": item.get("title"),
                "authors": item.get("authors", []),
                "publication_year": item.get("publication_year"),
                "doi": item.get("doi"),
                "url": item.get("url"),
                "source_type": item.get("source_type"),
                "abstract_or_snippet": str(item.get("abstract") or "")[:1800],
                "full_text_url": item.get("full_text_url"),
                "full_text_is_public": bool(item.get("full_text_is_public")),
                "source_read": item.get("source_read"),
                "alternate_for_source_id": item.get("alternate_for_source_id"),
                "source_identity_status": item.get("source_identity_status"),
            }
            for item in model_candidates
        ]
        system_text = (
            "本任务是探索性文献地图，不是命题核验、代表或主席的决策。根据给定来源，以人类可读的"
            "自由结构文字回答问题，并在使用具体来源时标出其 source_id。鼓励兼顾其他研究路径、"
            "限制和可能的反证，但不强制制造冲突或固定四栏格式。材料较弱也应交付初步地图，"
            "区分主观的‘可能存在研究空白’与客观的检索范围、后端或全文获取限制；不得断言"
            "‘没有研究’。可以顺带建议缩小后续问题，但不强制打回，也不主动生成模块划分。"
            "只能依据给定来源描述文献，不能虚构来源、把摘要说成全文或把资料线索当作"
            "SOURCE_BACKED/CLEAR 结论。source_read.excerpts 是已读取原件的局部摘录，"
            "不是全文审阅证明；具体条款、数值、方法和例外只能在摘录确实支持时叙述。"
            "没有相关摘录时，标明该内容仍是待查线索，不得补写细节。答复简洁且可读。无需输出 JSON。"
            "如果原件不可读而系统找到了替代发布页或 PDF，这些标记为 UNVERIFIED_ALTERNATIVE"
            " 的文件只是独立候选。核对题名、作者／发布机构、版本／日期、标识和相关正文；"
            "不能假定两站同版或同等权威。引用替代来源自己的 source_id，并明说原件的访问限制。"
        )
        user_text = (
            "问题：\n" + question
            + "\n\n可用来源目录（仅选定原件的相关摘录进入本次上下文；其余为检索元数据）：\n"
            + json.dumps(model_catalog, ensure_ascii=False)
            + "\n\n检索条件摘要：\n"
            + json.dumps({
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "backend_ids": list(dict.fromkeys(successful_backends)),
                "failed_backend_ids": list(dict.fromkeys(failed_backends)),
                "candidate_count": len(all_candidates),
                "presented_candidate_count": len(model_candidates),
                "full_text_links_found": sum(bool(item.get("full_text_url")) for item in all_candidates),
            }, ensure_ascii=False)
        )
        answer_text = ""
        status = "ANSWERED" if all_candidates else "WEAK_EVIDENCE"
        if all_candidates:
            stage = f"research_exploration_answer:{answer_id}"
            try:
                response = self.engine.find_recorded_response(
                    "RESEARCH_DESK", system_text=system_text, user_text=user_text, stage=stage
                )
                if response is None:
                    # Several private planning dialogues may run concurrently;
                    # the Research Desk model still obeys its own provider gate.
                    with self.desk._model_gate:
                        response = self.engine.find_recorded_response(
                            "RESEARCH_DESK", system_text=system_text,
                            user_text=user_text, stage=stage,
                        ) or self.engine.invoke_participant(
                            "RESEARCH_DESK", system_text=system_text,
                            user_text=user_text, stage=stage,
                            max_output_tokens=self.max_output_tokens,
                        )
                answer_text = response.text.strip()
            except (
                TransientProviderError, RepresentativeUnavailableError,
                EmptyModelOutputError, OutputLimitReachedError, InputContextLimitError,
            ):
                status = "WEAK_EVIDENCE"
        if not answer_text:
            answer_text = (
                "本次未形成可靠的综合叙述；下列来源目录和检索条件仍可供规划参考。"
                "这可能是文献覆盖不足、检索受限或综合调用失败，不能据此断言研究空白。"
            )
        cited_ids = [
            str(item["source_id"]) for item in model_candidates
            if f"[{item['source_id']}]" in answer_text
        ]
        cited_tokens = re.findall(r"\[([^\]\n]{1,160})\]", answer_text)
        unknown_citations = sorted(set(cited_tokens) - {
            str(item["source_id"]) for item in model_candidates
        })
        if unknown_citations:
            status = "WEAK_EVIDENCE"
            answer_text = (
                "注意：综合文字包含未在本次检索目录中的引文标识，相关句子不得作为已核实证据。\n"
                + answer_text
            )
        # Archive cited sources; if the model supplied no machine-detectable
        # citation, retain the first twelve discovery hits as inspectable leads.
        archive_ids = cited_ids[:30] or [str(item["source_id"]) for item in model_candidates[:12]]
        public_catalog = [
            {
                "source_id": item["source_id"],
                "title": item.get("title"),
                "authors": item.get("authors", []),
                "publication_year": item.get("publication_year"),
                "doi": item.get("doi"),
                "url": item.get("url"),
                "source_type": item.get("source_type"),
                "abstract": item.get("abstract"),
                "full_text_url": item.get("full_text_url"),
                "full_text_is_public": bool(item.get("full_text_is_public")),
                "license": item.get("license"),
                "venue": item.get("venue"),
                "is_primary_source": item.get("is_primary_source"),
                "source_read_status": (item.get("source_read") or {}).get("status"),
                "source_read_record_path": (item.get("source_read") or {}).get("record_path"),
                "alternate_for_source_id": item.get("alternate_for_source_id"),
                "source_identity_status": item.get("source_identity_status"),
            }
            for item in model_candidates
        ]
        retrieved_at = datetime.now(timezone.utc).isoformat()
        core = {
            "fingerprint": self._fingerprint(question, search_queries),
            "question": question,
            "answer_text": answer_text,
            "status": status,
            "retrieved_at": retrieved_at,
            "freshness_class": freshness_class.value,
            "query_count": len(search_queries),
            "retrieval_conditions": {
                "backend_ids": list(dict.fromkeys(successful_backends)),
                "failed_backend_ids": list(dict.fromkeys(failed_backends)),
                "candidate_count": len(all_candidates),
                "presented_candidate_count": len(model_candidates),
                "full_text_links_found": sum(bool(item.get("full_text_url")) for item in all_candidates),
                "readable_original_excerpt_count": sum(
                    (item.get("source_read") or {}).get("status") == "READABLE_EXCERPT"
                    for item in model_candidates
                ),
                "source_recovery_performed": True,
                "scope_note": "探索性检索；不对主张或文献共识作出判定。",
            },
            "source_catalog": public_catalog,
            "archive_source_ids": archive_ids,
        }
        self.repo.docs.write_once(
            audit_relative,
            json.dumps({
                "answer_id": answer_id,
                "question": question,
                "search_queries": search_queries,
                "query_trace": query_trace,
                "all_candidates": all_candidates,
                "source_reading": source_reading,
                "model_candidate_source_ids": [item["source_id"] for item in model_candidates],
                "cited_source_ids": cited_ids,
                "unknown_citation_ids": unknown_citations,
                "archive_source_ids": archive_ids,
                "screening_note": "未引用的候选材料只是探索线索，并非已被否决的证据。",
                "answer_text": answer_text,
                "status": status,
                "core": core,
            }, ensure_ascii=False, indent=2),
        )
        return core

    def publish(self, records: list[dict], *, public_prefix: Path) -> list[dict]:
        """Release a complete planning window and archive its inspectable sources."""
        published: list[dict] = []
        for record in records:
            answer_id = record["answer_id"]
            public_relative = public_prefix / f"{answer_id}.json"
            path = self.repo.root / public_relative
            if path.exists():
                published.append(json.loads(path.read_text(encoding="utf-8")))
                continue
            candidates = record["source_catalog"]
            selected = [
                item for item in candidates
                if item["source_id"] in set(record["archive_source_ids"])
            ]
            archive_by_id: dict[str, dict] = {}
            for item in selected:
                source_id = str(item["source_id"])
                source_digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
                packet_id = f"EX-{answer_id}-{source_digest}"
                record_path = (
                    self.repo.root / "public/research/literature_bundle/records"
                    / f"{packet_id}-001.json"
                )
                if record_path.exists():
                    archive_record = json.loads(record_path.read_text(encoding="utf-8"))
                else:
                    source = EvidenceSource(
                        source_id=source_id,
                        title=str(item.get("title") or "无题来源"),
                        authors=list(item.get("authors") or []),
                        publication_year=item.get("publication_year"),
                        doi=item.get("doi"),
                        url=str(item.get("url") or source_id),
                        source_type=str(item.get("source_type") or "discovery_source"),
                        evidence_use_class=EvidenceUseClass.DISCOVERY_ONLY,
                    )
                    self.desk.literature_bundle.archive_sources(
                        packet_id=packet_id, sources=[source], candidates=[item]
                    )
                    archive_record = json.loads(record_path.read_text(encoding="utf-8"))
                archive_by_id[source_id] = archive_record
            catalog = []
            for item in candidates:
                source = archive_by_id.get(item["source_id"])
                catalog.append({
                    **item,
                    "archived_path": source.get("archived_path") if source else None,
                    "archive_status": source.get("archive_status") if source else "NOT_SELECTED",
                })
            public = {
                key: value for key, value in record.items()
                if key not in {"archive_source_ids", "fingerprint", "search_queries"}
            }
            public["source_catalog"] = catalog
            public["query_count"] = record["query_count"]
            self.repo.docs.write_once(
                public_relative, json.dumps(public, ensure_ascii=False, indent=2)
            )
            published.append(public)
        if records:
            self.desk.literature_bundle.rebuild_download_bundle()
        return published
