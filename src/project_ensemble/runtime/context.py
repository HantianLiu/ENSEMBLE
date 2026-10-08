from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_ensemble.errors import AccessDeniedError
from project_ensemble.storage.events import HashChainEventLog
from project_ensemble.runtime.prompt_contract import prompt_contract_version


@dataclass(frozen=True)
class RepresentativeContextSpec:
    common_rules: Path
    persona_runtime: Path
    current_stage_protocol: Path
    public_state_files: tuple[Path, ...] = ()
    research_evidence_files: tuple[Path, ...] = ()
    own_state_files: tuple[Path, ...] = ()
    representative_id: str | None = None
    stage: str | None = None
    meeting_root: Path | None = None
    governance_root: Path | None = None
    research_evidence_max_packets: int = 16
    research_evidence_max_chars: int = 64_000


class RepresentativeContextAssembler:
    """Assembles only the current stage view; it accepts no future-stage path collection."""

    def __init__(self) -> None:
        self._audit_logs: dict[Path, HashChainEventLog] = {}
        self._audit_logs_lock = threading.Lock()

    def _audit_log(self, meeting_root: Path) -> HashChainEventLog:
        root = meeting_root.resolve()
        with self._audit_logs_lock:
            if root not in self._audit_logs:
                self._audit_logs[root] = HashChainEventLog(
                    root / "governance_private/representative_file_access.jsonl"
                )
            return self._audit_logs[root]

    def _read(self, spec: RepresentativeContextSpec, path: Path, role: str) -> str:
        if spec.meeting_root is None:
            return path.read_text(encoding="utf-8")
        if not spec.representative_id or not spec.stage or spec.governance_root is None:
            raise AccessDeniedError(
                "audited Representative context requires representative_id, stage, and governance_root"
            )
        root = spec.meeting_root.resolve()
        log = self._audit_log(root)
        try:
            resolved = path.resolve(strict=True)
            relative_path, compartment = self._authorize(
                spec=spec,
                resolved=resolved,
                role=role,
            )
            raw = resolved.read_bytes()
            text = raw.decode("utf-8")
        except (AccessDeniedError, OSError, UnicodeError) as exc:
            log.append(
                "REPRESENTATIVE_FILE_ACCESS_DENIED",
                {
                    "representative_id": spec.representative_id,
                    "stage": spec.stage,
                    "context_role": role,
                    "requested_path": str(path),
                    "decision": "DENIED_BEFORE_CONTEXT_RELEASE",
                    "reason": str(exc),
                },
                actor=spec.representative_id,
            )
            if isinstance(exc, AccessDeniedError):
                raise
            raise AccessDeniedError(
                f"Representative file access denied for {path}: {exc}"
            ) from exc
        derivation_source = role == "PUBLIC_EVIDENCE_SOURCE"
        log.append(
            (
                "REPRESENTATIVE_FILE_DERIVATION_SOURCE_READ"
                if derivation_source
                else "REPRESENTATIVE_FILE_READ"
            ),
            {
                "representative_id": spec.representative_id,
                "stage": spec.stage,
                "context_role": role,
                "compartment": compartment,
                "path": relative_path,
                "decision": (
                    "AUTHORIZED_FOR_BOUNDED_DERIVATION"
                    if derivation_source
                    else "AUTHORIZED_AND_READ"
                ),
                "byte_count": len(raw),
                "content_sha256": hashlib.sha256(raw).hexdigest(),
            },
            actor=spec.representative_id,
        )
        return text

    @staticmethod
    def _authorize(
        *,
        spec: RepresentativeContextSpec,
        resolved: Path,
        role: str,
    ) -> tuple[str, str]:
        meeting_root = spec.meeting_root.resolve()  # type: ignore[union-attr]
        governance_root = spec.governance_root.resolve()  # type: ignore[union-attr]
        representative_id = str(spec.representative_id)
        if role in {"COMMON_RULES", "PERSONA_RUNTIME", "CURRENT_STAGE_PROTOCOL"}:
            if not resolved.is_relative_to(governance_root):
                raise AccessDeniedError("governance context path escapes the configured governance root")
            return str(resolved.relative_to(governance_root)), "GOVERNANCE_SOURCE"
        if not resolved.is_relative_to(meeting_root):
            raise AccessDeniedError("meeting context path escapes the meeting workspace")
        relative = resolved.relative_to(meeting_root)
        if role in {"PUBLIC_STATE", "PUBLIC_EVIDENCE_SOURCE"}:
            if not relative.is_relative_to(Path("public")):
                raise AccessDeniedError("a public-state context path is outside the public compartment")
            return str(relative), "PUBLIC"
        if role != "OWN_STATE":
            raise AccessDeniedError(f"unknown Representative context role: {role}")
        own_root = Path("representatives") / representative_id
        if relative.is_relative_to(own_root):
            return str(relative), "REPRESENTATIVE_OWN"
        parts = relative.parts
        allowed_private_record = (
            len(parts) >= 4
            and parts[0] == "governance_private"
            and relative.name == f"{representative_id}.json"
            and (
                parts[1:3] == ("general_principle", "positions")
                or parts[1:3] == ("detailed_clauses", "reviews")
                or parts[1:3] == ("detailed_clauses", "type_i_revision_inputs")
                or (parts[1:3] == ("general_principle", "ballots") and "votes" in parts)
            )
        )
        if allowed_private_record:
            return str(relative), "GOVERNANCE_PRIVATE_OWN_RECORD"
        raise AccessDeniedError(
            "own-state context path is not inside the Representative's directory or an approved own-record location"
        )

    def assemble(self, spec: RepresentativeContextSpec) -> str:
        common_text = self._read(spec, spec.common_rules, "COMMON_RULES")
        chinese = (
            spec.governance_root is not None
            and spec.governance_root.name == "governance"
        )
        new_contract = prompt_contract_version(spec.meeting_root) >= 2
        if new_contract:
            # Neutral labels are independent of where frozen governance lives.
            manifest = json.loads((spec.meeting_root / "identity_private/meeting_manifest.json").read_text(
                encoding="utf-8"))
            language = manifest.get("deliberation_language") or manifest.get("rendering_language")
            chinese = (language == "zh" or (
                language not in {"en", "fr"} and bool(re.search(
                    r"[\u4e00-\u9fff]", common_text))))
        sections = [
            ("共同规则" if chinese else "COMMON RULES", common_text),
            ("当前阶段" if chinese else "CURRENT STAGE", self._read(spec, spec.current_stage_protocol, "CURRENT_STAGE_PROTOCOL")),
        ]
        public_bodies: list[str] = []
        for p in spec.public_state_files:
            body = self._read(spec, p, "PUBLIC_STATE")
            public_bodies.append(body)
            sections.append((f"{'公开材料' if chinese else 'PUBLIC STATE'}: {p.name}", body))
        if spec.meeting_root is not None:
            advisory_path = spec.meeting_root / "public/continuation/advisory_context.md"
            if advisory_path.is_file() and advisory_path not in spec.public_state_files:
                advisory = self._read(spec, advisory_path, "PUBLIC_STATE")
                public_bodies.append(advisory)
                sections.append(("继承的建议性文书" if chinese else "INHERITED ADVISORY DOCUMENT", advisory))
        if spec.research_evidence_files:
            evidence = self._bounded_research_evidence(
                spec,
                query="\n".join(public_bodies),
            )
            if evidence:
                sections.append(("公开研究证据（限量相关视图）" if chinese else "PUBLIC RESEARCH EVIDENCE (BOUNDED RELEVANCE VIEW)", evidence))
        # Keep meeting-wide material as one stable prefix. Provider prompt caches are
        # prefix-based, so participant-specific persona and private state belong after it.
        sections.append(
            ("本次职能侧重" if chinese else ("TASK EMPHASIS" if new_contract else "YOUR PERSONA"),
             self._read(spec, spec.persona_runtime, "PERSONA_RUNTIME"))
        )
        for p in spec.own_state_files:
            sections.append((f"{'本人记录' if chinese else ('OWN RECORD' if new_contract else 'YOUR STATE')}: {p.name}",
                             self._read(spec, p, "OWN_STATE")))
        return "\n\n".join(f"## {title}\n{body.strip()}" for title, body in sections) + "\n"

    def _bounded_research_evidence(
        self,
        spec: RepresentativeContextSpec,
        *,
        query: str,
    ) -> str:
        """Return a bounded, task-relevant view without mutating the public evidence DB.

        Released snapshots remain the authoritative public record.  This method only
        controls how much of that record is copied into one provider request.
        """

        candidates: dict[str, tuple[int, dict[str, Any]]] = {}
        superseded: set[str] = set()
        for sequence, path in enumerate(spec.research_evidence_files):
            raw = self._read(spec, path, "PUBLIC_EVIDENCE_SOURCE")
            try:
                snapshot = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise AccessDeniedError(f"released evidence snapshot is invalid JSON: {path}") from exc
            round_id = str(snapshot.get("round_id") or path.parent.name)
            for packet in snapshot.get("packets", []):
                if not isinstance(packet, dict) or not packet.get("packet_id"):
                    continue
                enriched = dict(packet)
                enriched["_round_id"] = round_id
                candidates[str(packet["packet_id"])] = (sequence, enriched)
                superseded.update(str(x) for x in packet.get("supersedes_packet_ids", []))

        active = [value for key, value in candidates.items() if key not in superseded]
        if not active:
            return ""
        query_terms = self._search_terms(query)
        ranked: list[tuple[int, int, str, dict[str, Any]]] = []
        for sequence, packet in active:
            searchable = " ".join(
                str(packet.get(field, ""))
                for field in (
                    "normalized_claim",
                    "verification_question",
                    "scope_terms",
                    "search_scope",
                )
            )
            packet_terms = self._search_terms(searchable)
            score = len(query_terms & packet_terms)
            ranked.append((score, sequence, str(packet.get("packet_id")), packet))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))

        # Positive lexical matches are preferred.  A small recency fallback keeps
        # the public evidence service visible when Chinese/English wording differs.
        positive = [item for item in ranked if item[0] > 0]
        selected_pool = positive if positive else ranked[:4]
        selected_pool = selected_pool[: spec.research_evidence_max_packets]

        header = {
            "view": "BOUNDED_RELEVANCE_VIEW",
            "authoritative_record": "public/research/rounds/*/evidence_snapshot.json",
            "active_packet_count": len(active),
            "selected_packet_limit": spec.research_evidence_max_packets,
            "character_limit": spec.research_evidence_max_chars,
            "notice": (
                "Omission from this prompt view is not evidence of absence. "
                "The complete released evidence database remains public and auditable."
            ),
        }
        rendered_packets: list[dict[str, Any]] = []
        for score, _sequence, _packet_id, packet in selected_pool:
            compact = self._compact_packet(packet, relevance_score=score)
            candidate_header = dict(header)
            candidate_header["selected_packet_count"] = len(rendered_packets) + 1
            candidate_header["omitted_active_packet_count"] = len(active) - len(rendered_packets) - 1
            candidate_text = json.dumps(
                {"metadata": candidate_header, "packets": [*rendered_packets, compact]},
                indent=2,
                ensure_ascii=False,
            )
            if len(candidate_text) > spec.research_evidence_max_chars:
                compact = self._compact_packet(packet, relevance_score=score, severe=True)
                candidate_text = json.dumps(
                    {"metadata": candidate_header, "packets": [*rendered_packets, compact]},
                    indent=2,
                    ensure_ascii=False,
                )
            if len(candidate_text) > spec.research_evidence_max_chars:
                break
            rendered_packets.append(compact)
        header["selected_packet_count"] = len(rendered_packets)
        header["omitted_active_packet_count"] = len(active) - len(rendered_packets)
        released = json.dumps(
            {"metadata": header, "packets": rendered_packets},
            indent=2,
            ensure_ascii=False,
        )
        if spec.meeting_root is not None:
            released_bytes = released.encode("utf-8")
            self._audit_log(spec.meeting_root).append(
                "REPRESENTATIVE_DERIVED_CONTEXT_RELEASED",
                {
                    "representative_id": spec.representative_id,
                    "stage": spec.stage,
                    "context_role": "PUBLIC_RESEARCH_EVIDENCE",
                    "decision": "BOUNDED_RELEVANCE_VIEW_RELEASED",
                    "selected_packet_ids": [
                        str(packet.get("packet_id")) for packet in rendered_packets
                    ],
                    "active_packet_count": len(active),
                    "selected_packet_count": len(rendered_packets),
                    "character_limit": spec.research_evidence_max_chars,
                    "character_count": len(released),
                    "byte_count": len(released_bytes),
                    "content_sha256": hashlib.sha256(released_bytes).hexdigest(),
                },
                actor=spec.representative_id,
            )
        return released

    @staticmethod
    def _search_terms(text: str) -> set[str]:
        lowered = text.casefold()
        terms = set(re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", lowered))
        for run in re.findall(r"[\u3400-\u9fff]+", lowered):
            terms.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
        return terms

    @classmethod
    def _compact_packet(
        cls,
        packet: dict[str, Any],
        *,
        relevance_score: int,
        severe: bool = False,
    ) -> dict[str, Any]:
        def short(value: Any, limit: int) -> Any:
            if not isinstance(value, str) or len(value) <= limit:
                return value
            return value[: limit - 1].rstrip() + "…"

        finding_limit = 1 if severe else 2
        text_limit = 280 if severe else 700
        finding_fields = (
            "supporting_evidence",
            "contradictory_evidence",
            "scope_limitations",
            "canonical_alternatives",
        )
        compact: dict[str, Any] = {
            "packet_id": packet.get("packet_id"),
            "round_id": packet.get("_round_id"),
            "relevance_score": relevance_score,
            "original_claim": short(packet.get("original_claim"), text_limit),
            "normalized_claim": short(packet.get("normalized_claim"), text_limit),
            "verification_question": short(packet.get("verification_question"), text_limit),
            "scope_terms": packet.get("scope_terms", [])[:8],
            "retrieved_at": packet.get("retrieved_at"),
            "consensus": packet.get("consensus"),
            "knowledge_status": packet.get("knowledge_status"),
            "evidence_conflict_assessment": short(
                packet.get("evidence_conflict_assessment"), text_limit
            ),
            "unresolved_questions": [
                short(item, text_limit) for item in packet.get("unresolved_questions", [])[:2]
            ],
        }
        used_source_ids: set[str] = set()
        for field in finding_fields:
            findings = packet.get(field, [])
            compact[field] = []
            for finding in findings[:finding_limit]:
                item = {
                    "source_id": finding.get("source_id"),
                    "evidence_summary": short(finding.get("evidence_summary"), text_limit),
                    "applicability": short(finding.get("applicability"), text_limit),
                    "limitations": short(finding.get("limitations"), text_limit),
                }
                compact[field].append(item)
                if item["source_id"]:
                    used_source_ids.add(str(item["source_id"]))
            if len(findings) > finding_limit:
                compact[f"{field}_omitted_count"] = len(findings) - finding_limit
        compact["sources"] = [
            {
                "source_id": source.get("source_id"),
                "title": short(source.get("title"), 300),
                "publication_year": source.get("publication_year"),
                "doi": source.get("doi"),
                "url": source.get("url"),
                "evidence_use_class": source.get("evidence_use_class"),
                "archive_status": source.get("archive_status"),
                "archived_path": source.get("archived_path"),
            }
            for source in packet.get("sources", [])
            if str(source.get("source_id")) in used_source_ids
        ]
        return compact
