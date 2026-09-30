from __future__ import annotations

"""Version-gated, resumable literature-writing procedure for new v0.7.1 meetings.

The research and evidence pipeline remains in ``literature_report_execution``.
This module starts only after a module's evidence dossier has been frozen.
Meetings without ``literature_writing_policy=v071`` never enter this path.
"""

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.errors import ProviderError, ResearchRequestRejectedError
from project_ensemble.orchestration.consultations import (
    HumanConsultationIssue, HumanConsultationService,
)
from project_ensemble.orchestration.literature_report_execution import (
    ModuleDraft, WholeReportSynthesis, _CHAPTER_SOURCE_MARKER, _PACKET_MARKER,
    _effective_chapter_citation_catalog_path, _source_identity_key,
)
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.orchestration.readability_policy import reader_style_policy


class LiteratureWritingPaused(Exception):
    """A durable Human decision is needed; resuming must not redo frozen calls."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


_FACT_FIRST_OUTLINE_RULES = (
    "报告的组织单位是研究问题、观点和证据之间的关系，不是检索到的文件。"
    "提纲每一步先指出要回答的具体问题及预期比较维度，再说明哪些证据能回答、"
    "哪些定义或证据差异必须先澄清。不得以论文题名、文件名或法规名称充当章节框架。"
)

_FACT_FIRST_WRITING_RULES = (
    "输入中的 fact_first_index 是按具体发现组织的导航索引。先明确本章核心问题；"
    "正文先给出有边界的判断，再呈现关键证据并解释证据为何支持该判断。"
    "每段先说明事实或推论及其适用对象与条件，紧接具体 C 引文；"
    "不得以逐篇介绍论文、来源题名或检索入口代替发现。"
    "比较不同研究时沿同一维度讨论，先核对定义、时间范围、样本、方法和指标是否可比；"
    "说明差异是否真实、可能原因及其对核心问题的意义。证据冲突不能仅并排摆放，"
    "应分析可能原因并指出尚不能判断的部分。解释机制时写清条件、过程和结果。"
    "明确区分来源直接陈述的事实、多份来源综合得到的判断和作者推论。"
    "只有题名、元数据、搜索摘要或片段而未核对相关内容的来源，不得支撑细节性结论；"
    "已核对摘要中的明确陈述也不能外推为全文数据或方法细节。"
    "没有可提取发现的资料只保留在资料说明或参考文献，不用空泛引文填充正文。"
    "交稿前逐段自查：该段回答什么问题；删除引文后是否仍有具体分析；"
    "引文是否支持相邻断言；该段是否推进本章结论。仅介绍来源名称与大意的段落应改写或移出正文。"
    "事实卡只给出可引用文献编号；内部证据追溯由系统负责，不能写进读者正文。"
)


class OutlineStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    heading: str = Field(min_length=1, max_length=180)
    purpose: str = Field(min_length=1, max_length=600)
    evidence_boundary: str = Field(min_length=1, max_length=600)


class ModuleWritingOutline(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    steps: list[OutlineStep] = Field(min_length=1, max_length=16)
    required_definitions: list[str] = Field(default_factory=list, max_length=20)
    scope_notes: list[str] = Field(default_factory=list, max_length=16)


class OutlineBallot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    vote: Literal["YES", "NO"]
    revision_note: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def no_requires_reason(self) -> "OutlineBallot":
        if self.vote == "NO" and not self.revision_note:
            raise ValueError("a negative outline vote needs one concrete structural reason")
        return self


class GlossaryTerm(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    term: str = Field(min_length=1, max_length=120)
    explanation_mode: Literal["NATURAL_LANGUAGE", "FORMULA"]
    explanation: str = Field(min_length=1, max_length=5000)
    formula: str | None = Field(default=None, max_length=2400)
    source_citation_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def formula_is_explicit(self) -> "GlossaryTerm":
        if self.explanation_mode == "FORMULA" and not self.formula:
            raise ValueError("a formula-level term requires the mathematical expression")
        return self


class OutlineDeviation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    outline_step_number: int = Field(ge=1, le=16)
    # Legacy frozen drafts may still use packet IDs. New Writer prompts use
    # only reader-facing source IDs; the orchestrator resolves provenance.
    new_evidence_packet_ids: list[str] = Field(default_factory=list, max_length=8)
    new_evidence_citation_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def has_evidence(self) -> "OutlineDeviation":
        if not self.new_evidence_packet_ids and not self.new_evidence_citation_ids:
            raise ValueError("outline deviation requires cited evidence")
        return self


class WriterChapter(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    draft: ModuleDraft
    glossary_additions: list[GlossaryTerm] = Field(default_factory=list, max_length=64)
    outline_deviations: list[OutlineDeviation] = Field(default_factory=list, max_length=12)
    revision_responses: list[dict[str, str]] = Field(default_factory=list, max_length=36)


class ScienceIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    location_excerpt: str = Field(min_length=1, max_length=500)
    questioned_claim: str = Field(min_length=1, max_length=700)
    why_it_matters: str = Field(min_length=1, max_length=1200)
    evidence_packet_ids: list[str] = Field(default_factory=list, max_length=12)
    suggested_response: str | None = Field(default=None, max_length=1200)


class ScienceChecklist(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issues: list[ScienceIssue] = Field(default_factory=list, max_length=12)
    glossary_corrections: list[str] = Field(default_factory=list, max_length=24)


class ScienceIssueCluster(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_issue_ids: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=500)


class ScienceIssueGrouping(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    clusters: list[ScienceIssueCluster]


class ChairIssueDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issue_id: str
    action: Literal["ACCEPT_FOR_RESPONSE", "REJECT"]
    rationale: str = Field(min_length=1, max_length=900)


class ChairIssueDocket(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decisions: list[ChairIssueDecision]


class RescueVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    vote: Literal["YES", "NO"]
    anonymous_note: str | None = Field(default=None, max_length=500)


class OutlineEvidenceVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    vote: Literal["YES", "NO"]


class LocalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Literal["PASS", "MATERIAL_PROBLEM"]
    problem: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def problem_matches_status(self) -> "LocalCheck":
        if (self.status == "MATERIAL_PROBLEM") != bool(self.problem):
            raise ValueError("a material problem needs one concrete explanation")
        return self


class ConclusionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    body_markdown: str = Field(min_length=1)


def _freeze(runner, relative: Path, payload: BaseModel | dict) -> Path:
    path = runner.repo.root / relative
    value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    encoded = json.dumps(value, indent=2, ensure_ascii=False)
    if path.is_file():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError(f"frozen v0.7.1 writing artifact conflicts: {relative}")
    else:
        runner.repo.docs.write_once(relative, encoded)
    return path


def _frozen_or_call(runner, relative: Path, schema: type[BaseModel],
                    participant_id: str, stage: str, system: str, user: dict) -> BaseModel:
    path = runner.repo.root / relative
    if path.is_file():
        return schema.model_validate_json(path.read_text(encoding="utf-8"))
    if participant_id == "WRITER":
        # One final boundary protects every Writer call, including fast-mode
        # planning and later reader-facing repairs, from legacy packet handles.
        user = _writer_visible_payload(user, {"sources": []})
    value = runner._invoke_service(participant_id, stage=stage, schema=schema,
                                   system=system, user=user)
    _freeze(runner, relative, value)
    if participant_id == "WRITER" and relative.name.startswith("writer_v"):
        getattr(runner.engine.progress, "artifact_committed", lambda *_args: None)(
            "WRITER", "初稿及本轮检索结果已完整落盘",
        )
    return value


_PACKET_METADATA_LINE = re.compile(
    r"^\s*(?:工作元数据[^\n：:]{0,80}|证据包(?:编号|标识|ID)?(?:备案)?|"
    r"(?:internal|research|evidence)\s+packet\s+ids?)\s*[：:]\s*"
    r"(?:\[\s*RP-[A-Z0-9]+\s*\]\s*)+$",
    re.IGNORECASE,
)


def _mechanically_repair_writer_citations(
    chapter: WriterChapter, catalog: dict,
) -> tuple[WriterChapter, list[dict[str, str]]]:
    """Fix presentation-only citation defects without assigning ambiguous sources."""

    packet_sources: dict[str, set[str]] = {}
    for source in catalog.get("sources", []):
        for packet_id in source.get("packet_ids", []):
            packet_sources.setdefault(packet_id, set()).add(source["citation_id"])
    body = chapter.draft.body_markdown
    operations: list[dict[str, str]] = []
    if _CHAPTER_SOURCE_MARKER.search(body + "\n" + chapter.draft.short_summary):
        retained = []
        for line in body.splitlines(keepends=True):
            if _PACKET_METADATA_LINE.fullmatch(line.rstrip("\r\n")):
                operations.append({"operation": "REMOVE_INTERNAL_PACKET_METADATA_LINE",
                                   "original": line.rstrip("\r\n")})
                continue
            retained.append(line)
        body = "".join(retained)

    def replace_packet(match: re.Match[str]) -> str:
        packet_id = match.group(1)
        sources = packet_sources.get(packet_id, set())
        if len(sources) != 1:
            return match.group(0)
        citation_id = next(iter(sources))
        operations.append({"operation": "UNIQUE_PACKET_TO_SOURCE",
                           "original": match.group(0), "replacement": f"[{citation_id}]"})
        return f"[{citation_id}]"

    body = _PACKET_MARKER.sub(replace_packet, body)
    summary = _PACKET_MARKER.sub(replace_packet, chapter.draft.short_summary)
    if not operations:
        return chapter, []
    draft = ModuleDraft.model_validate({
        **chapter.draft.model_dump(mode="python"),
        "body_markdown": body, "short_summary": summary,
    })
    return chapter.model_copy(update={"draft": draft}), operations


def _recheck_writer_citation_evidence(
    runner, module, version: int, dossier: dict, catalog: dict,
    chapter: WriterChapter,
) -> tuple[dict, list[dict]]:
    """Refresh the *original questions* behind unresolved citations.

    The initial dossier and catalog stay frozen.  A supplementary catalog is
    append-only and gives the writer usable IDs for newly retrieved sources.
    """
    base = _module_base(module) / "writing_v071"
    relative = base / f"writer_v{version}_citation_recheck.json"
    path = runner.repo.root / relative
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        return saved["catalog"], saved["rechecked_claims"]
    prose = chapter.draft.body_markdown + "\n" + chapter.draft.short_summary
    packet_ids = list(dict.fromkeys(_PACKET_MARKER.findall(prose)))
    if not packet_ids:
        # An invented chapter ID can still be traced through the draft's
        # declared packet provenance, without guessing what it should cite.
        packet_ids = list(dict.fromkeys(chapter.draft.cited_packet_ids))
    by_packet = {item["packet_id"]: item for item in dossier.get("packets", [])}
    refreshed = []
    revised = json.loads(json.dumps(catalog))
    sources = revised["sources"]
    source_by_key = {
        _source_identity_key(item.get("doi"), item["url"]): item
        for item in sources
    }
    chapter_number = int(module.module_id.split("-")[1])
    for index, packet_id in enumerate(packet_ids, start=1):
        original = by_packet.get(packet_id)
        if not original:
            refreshed.append({"original_packet_id": packet_id,
                              "status": "ORIGINAL_PACKET_NOT_IN_DOSSIER"})
            continue
        claim = original.get("original_claim") or original.get("normalized_claim")
        if not claim:
            refreshed.append({"original_packet_id": packet_id,
                              "status": "ORIGINAL_QUESTION_UNAVAILABLE"})
            continue
        request_id = f"{module.module_id}-V071-WRITER-CITATION-V{version}-{index:03d}"
        try:
            packet = runner._research_or_restore_model_prior(
                request_id=request_id, requester_id="WRITER", claim=claim,
                force_refresh=True,
            )
        except ResearchRequestRejectedError as exc:
            refreshed.append({"original_packet_id": packet_id,
                              "status": "RECHECK_REJECTED",
                              "reason": str(exc)})
            continue
        except ProviderError as exc:
            refreshed.append({"original_packet_id": packet_id,
                              "status": "RECHECK_PROVIDER_UNAVAILABLE",
                              "reason": str(exc)})
            continue
        refreshed.append({
            "original_packet_id": packet_id,
            "new_packet_id": packet.packet_id,
            "original_question": claim,
            "status": "RECHECKED",
            "knowledge_status": packet.knowledge_status.value,
            "consensus": packet.consensus.value,
            "findings": [
                {"source_id": finding.source_id,
                 "summary": finding.evidence_summary[:900],
                 "limitations": finding.limitations[:500]}
                for findings in (packet.supporting_evidence,
                                 packet.contradictory_evidence,
                                 packet.scope_limitations)
                for finding in findings[:6]
            ],
        })
        for source in packet.sources:
            source_data = source.model_dump(mode="json")
            key = _source_identity_key(source_data.get("doi"), source_data["url"])
            if key in source_by_key:
                entry = source_by_key[key]
                if packet.packet_id not in entry["packet_ids"]:
                    entry["packet_ids"].append(packet.packet_id)
                continue
            entry = {
                "citation_id": f"C{chapter_number:05d}-{len(sources) + 1:05d}",
                "source_id": source_data["source_id"],
                "packet_ids": [packet.packet_id],
                "title": source_data["title"],
                "authors": source_data.get("authors", []),
                "publication_year": source_data.get("publication_year"),
                "doi": source_data.get("doi"), "url": source_data["url"],
            }
            sources.append(entry)
            source_by_key[key] = entry
    _freeze(runner, relative, {
        "original_catalog_sha256": hashlib.sha256(
            json.dumps(catalog, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "catalog": revised,
        "rechecked_claims": refreshed,
        "policy": "SUPPLEMENT_ONLY; ORIGINAL_DOSSIER_AND_CATALOG_UNCHANGED",
    })
    return revised, refreshed


def _science_librarians(runner, *, stage: str | None = None) -> list[dict]:
    librarians = [item for item in runner.active
                  if item["runtime"]["persona"] == Persona.LIBRARIAN.value]
    if not librarians:
        raise ValueError("v0.7.1 scientific review needs at least one active Librarian")
    # One scientific seat, with the other model(s) serving as ordered standbys.
    # Keep this Human ruling separate from the meeting's pinned governance
    # package: older frozen ballots are never rewritten when software changes.
    private = (Path("governance_private/literature_report/fast")
               if hasattr(runner, "_fast_root") else
               Path("governance_private/literature_report/writing_v071"))
    policy_relative = private / "librarian_seat_policy.json"
    policy_path = runner.repo.root / policy_relative
    if policy_path.is_file():
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    else:
        policy = {"mode": "PRIMARY_WITH_FAILURE_STANDBY",
                  "primary_id": librarians[0]["representative_id"],
                  "standby_ids": [item["representative_id"] for item in librarians[1:]],
                  "authority": "HUMAN_RULE_FOR_LIBRARIAN_BALLOTS",
                  "frozen_prior_ballots_unchanged": True}
        _freeze(runner, policy_relative, policy)
        events = getattr(runner.repo, "events", None)
        if events is not None:
            events.append("LIBRARIAN_SEAT_POLICY_FROZEN", {
                "meeting_id": runner.repo.meeting_id, "record_path": str(policy_relative),
                **policy,
            }, actor="orchestrator")
    by_id = {item["representative_id"]: item for item in librarians}
    active_relative = private / "librarian_standby_activation.json"
    active_path = runner.repo.root / active_relative
    if not active_path.is_file() and stage and policy["standby_ids"]:
        # On resume after a provider exhausted its output before producing a
        # final answer, the already-frozen standby response may be used. The
        # failed exchange is immutable evidence that this is not a voluntary
        # second vote or an ordinary Ctrl+C interruption.
        failed_record = None
        for exchange_path in sorted((runner.repo.root / "governance_private/provider_exchanges").glob("*.json")):
            try:
                exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (exchange.get("participant_id") != policy["primary_id"]
                    or exchange.get("stage") != stage):
                continue
            response = exchange.get("response") or {}
            choices = (response.get("raw") or {}).get("choices") or []
            if response.get("text") or not choices or choices[0].get("finish_reason") != "length":
                continue
            failed_record = {"reason": "PRIMARY_OUTPUT_LIMIT_REACHED_WITHOUT_FINAL_TEXT",
                             "failed_exchange_id": exchange.get("exchange_id")}
            break
        if failed_record is None:
            event_path = runner.repo.root / "governance_private/events.jsonl"
            if event_path.is_file():
                last_exchange_stage = None
                for line in event_path.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    payload = event.get("payload") or {}
                    if payload.get("participant_id") == policy["primary_id"]:
                        if event.get("event_type") == "PROVIDER_EXCHANGE_RECORDED":
                            last_exchange_stage = payload.get("stage")
                        elif (event.get("event_type") in {
                            "MODEL_INPUT_CONTEXT_PREFLIGHT", "MODEL_INPUT_CHARACTER_PREFLIGHT"
                        } and payload.get("decision") == "PAUSE"):
                            last_exchange_stage = payload.get("stage")
                        elif (event.get("event_type") == "PROVIDER_CALL_DIAGNOSTIC"
                              and payload.get("stage") == stage):
                            failed_record = {"reason": "PRIMARY_PROVIDER_CALL_FAILED",
                                             "failed_diagnostic_path": payload.get("record_path")}
                    if (event.get("event_type") == "HUMAN_INTERVENTION_REQUIRED"
                            and policy["primary_id"] in str(payload.get("summary") or "")
                            and last_exchange_stage == stage
                            and payload.get("reason_code") in {
                                "SCHEMA_INVALID_MODEL_OUTPUT_AFTER_REPAIR",
                                "MODEL_OUTPUT_EMPTY_AFTER_RETRIES",
                                "MODEL_INPUT_CONTEXT_BUDGET_EXCEEDED",
                                "MODEL_INPUT_CHARACTER_BUDGET_EXCEEDED",
                            }):
                        failed_record = {"reason": payload["reason_code"]}
        standby_id = next((item for item in policy["standby_ids"] if item in by_id), None)
        if standby_id and failed_record:
            _freeze(runner, active_relative, {
                "primary_id": policy["primary_id"], "standby_id": standby_id,
                "failed_stage": stage, **failed_record,
            })
            events = getattr(runner.repo, "events", None)
            if events is not None:
                events.append("LIBRARIAN_STANDBY_ACTIVATED", {
                    "meeting_id": runner.repo.meeting_id,
                    "primary_id": policy["primary_id"], "standby_id": standby_id,
                    "failed_stage": stage, "reason": failed_record["reason"],
                    "record_path": str(active_relative),
                }, actor="orchestrator")
    if active_path.is_file():
        standby_id = json.loads(active_path.read_text(encoding="utf-8"))["standby_id"]
        if standby_id not in by_id:
            raise ValueError("activated standby Librarian is no longer active")
        return [by_id[standby_id]]
    if policy["primary_id"] not in by_id:
        raise ValueError("primary Librarian is unavailable without an activated standby")
    return [by_id[policy["primary_id"]]]


def _module_base(module) -> Path:
    return Path("public/literature_report/modules") / module.module_id


def _science_review_evidence(dossier: dict, chapter: WriterChapter | None = None,
                             *, packet_ids: set[str] | None = None,
                             max_chars: int = 280_000,
                             cited_source_ids: set[str] | None = None) -> dict:
    """Give reviewers cited findings, not the entire archival source inventory.

    The complete immutable dossier remains on disk. Each packet declares any
    omitted findings so a compact review cannot masquerade as exhaustive.
    """
    cited_ids = (set(packet_ids) if packet_ids is not None else
                 set(chapter.draft.cited_packet_ids) if chapter is not None else set())
    packets = dossier.get("packets", [])
    selected = ([packet for packet in packets if packet.get("packet_id") in cited_ids]
                if cited_ids else list(packets))
    if not selected and packet_ids is None:
        # An uncited chapter still needs review, but a huge dossier must not
        # exceed the model's character limit before any review can start.
        selected = packets[:16]
    compact = []
    cited_sources = cited_source_ids or set()
    # Bound the complete task view, not merely each individual packet. A
    # dossier can contain many independently reasonable packets yet overflow
    # a provider's character limit when they are combined.
    remaining = max_chars - 2500
    packet_budget = max(1200, remaining // max(1, len(selected)))
    for packet in selected:
        sources = {source["source_id"]: source for source in packet.get("sources", [])}
        item = {
            "packet_id": packet["packet_id"],
            "normalized_claim": str(packet.get("normalized_claim") or "")[:500],
            "knowledge_status": packet.get("knowledge_status"),
            "consensus": packet.get("consensus"),
            "findings": [],
            "omitted_finding_count": 0,
            "omitted_cited_source_finding_count": 0,
            "unresolved_questions": [str(value)[:250] for value in packet.get("unresolved_questions", [])[:3]],
        }
        base_size = len(json.dumps(item, ensure_ascii=False))
        if base_size > remaining:
            break
        remaining -= base_size
        packet_remaining = max(0, packet_budget - base_size)
        finding_groups = ("supporting_evidence", "contradictory_evidence", "scope_limitations")
        ordered_findings = []
        # Keep different evidence directions interleaved, but move findings
        # from sources actually cited by the chapter to the front. Otherwise
        # a late finding in a large packet can be silently truncated while
        # a reviewer declares the citation unsupported.
        for index in range(max((len(packet.get(key, [])) for key in finding_groups), default=0)):
            for key in finding_groups:
                if index < len(packet.get(key, [])):
                    ordered_findings.append((key, packet[key][index]))
        ordered_findings.sort(key=lambda pair: pair[1].get("source_id") not in cited_sources)
        for key, finding in ordered_findings:
            source = sources.get(finding.get("source_id"), {})
            card = {
                "direction": key,
                "source_id": finding.get("source_id"),
                "source_title": str(source.get("title") or "")[:160],
                "source_year": source.get("publication_year"),
                "source_use_class": source.get("evidence_use_class"),
                "source_archive_status": source.get("archive_status"),
                "finding": str(finding.get("evidence_summary") or "")[:700],
                "applicability": str(finding.get("applicability") or "")[:450],
                "limitations": str(finding.get("limitations") or "")[:450],
            }
            proposed = json.dumps(item, ensure_ascii=False) + json.dumps(card, ensure_ascii=False)
            card_size = len(json.dumps(card, ensure_ascii=False))
            if (len(proposed) > packet_budget or card_size > remaining
                    or card_size > packet_remaining):
                item["omitted_finding_count"] += 1
                if finding.get("source_id") in cited_sources:
                    item["omitted_cited_source_finding_count"] += 1
            else:
                item["findings"].append(card)
                remaining -= card_size
                packet_remaining -= card_size
        compact.append(item)
    return {
        "source_dossier_path": "public/literature_report/modules/"
        + dossier.get("module_id", "") + "/research/evidence_dossier.json",
        "selected_packet_count": len(compact),
        "omitted_packet_count": max(0, len(packets) - len(compact)),
        "requested_packet_count": len(selected),
        "omitted_requested_packet_count": max(0, len(selected) - len(compact)),
        "packets": compact,
        "scope_note": ("这是供科学审阅的有界摘录，不是完整证据档案；"
                       "如果任何被引用来源的发现被省略，不得仅凭本视图断言该来源不存在或不支持论断。"),
    }


def _writer_visible_payload(value, catalog: dict):
    """Keep packet handles in the orchestrator, never in Writer model input."""
    citation_by_source = {
        source["source_id"]: source["citation_id"]
        for source in catalog.get("sources", []) if source.get("source_id")
    }

    def sanitize(item):
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if "packet_id" in key or key == "source_dossier_path":
                    continue
                if key == "source_id":
                    citation = citation_by_source.get(str(child))
                    if citation:
                        result["citation_id"] = citation
                    continue
                result[key] = sanitize(child)
            return result
        if isinstance(item, list):
            return [sanitize(child) for child in item]
        if isinstance(item, str):
            # Older frozen drafts can contain literal internal handles. This
            # changes only the bounded prompt view, never the frozen artifact.
            return re.sub(r"\[?RP-[A-Z0-9]+\]?", "[来源待核]", item)
        return item

    return sanitize(value)


def _writer_evidence_view(dossier: dict, catalog: dict) -> dict:
    view = _science_review_evidence(dossier)
    view.pop("source_dossier_path", None)
    view["knowledge_cards"] = view.pop("packets")
    for old, new in (
        ("selected_packet_count", "selected_knowledge_card_count"),
        ("omitted_packet_count", "omitted_knowledge_card_count"),
        ("requested_packet_count", "requested_knowledge_card_count"),
        ("omitted_requested_packet_count", "omitted_requested_knowledge_card_count"),
    ):
        if old in view:
            view[new] = view.pop(old)
    view["scope_note"] = (
        "有界知识卡视图；按具体判断、适用范围和证据限制写作。"
        "引用只使用随卡片给出的 C 文献编号；省略项不等于没有证据。"
    )
    return _writer_visible_payload(view, catalog)


def _prompt_fact_index(index: dict, *, max_chars: int = 230_000) -> dict:
    """Bound the persisted finding index without changing its frozen record."""
    cards = []
    budget = max_chars - 2000
    for original in index.get("cards", []):
        card = {key: str(original.get(key) or "")[:limit] for key, limit in (
            ("claim", 400), ("finding", 650), ("direction", 30),
            ("applicability", 350), ("limitations", 350),
            ("citation_id", 50), ("source_access_basis", 60),
            ("knowledge_status", 30), ("consensus", 30),
        )}
        size = len(json.dumps(card, ensure_ascii=False))
        if size > budget:
            break
        cards.append(card)
        budget -= size
    gaps = []
    for original in index.get("unresolved_questions", []):
        gap = {"claim": str(original.get("claim") or "")[:300],
               "questions": [str(q)[:250] for q in original.get("questions", [])[:3]]}
        size = len(json.dumps(gap, ensure_ascii=False))
        if size > budget:
            break
        gaps.append(gap)
        budget -= size
    return {"module_id": index.get("module_id"), "cards": cards,
            "omitted_card_count": index.get("omitted_card_count", 0)
            + len(index.get("cards", [])) - len(cards),
            "unresolved_questions": gaps,
            "omitted_gap_count": len(index.get("unresolved_questions", [])) - len(gaps),
            "scope_note": "有界写作索引；被省略的发现和缺口仍在冻结证据档案中。"}


def _prompt_citation_catalog(catalog: dict, *, max_chars: int = 160_000) -> dict:
    """Keep every valid citation ID while bounding source metadata."""
    originals = catalog.get("sources", [])
    sources = [{"citation_id": source["citation_id"]} for source in originals]
    essential_size = sum(len(json.dumps(item, ensure_ascii=False)) for item in sources)
    budget = max_chars - 1000 - essential_size
    if budget < 0:
        raise ValueError("citation ID catalog exceeds the bounded writing context")
    # Reserve space for every citation ID before enriching any one source.
    for index, source in enumerate(originals):
        entry = sources[index]
        source_budget = budget // (len(originals) - index)
        initial_size = len(json.dumps(entry, ensure_ascii=False))
        for field, limit in (("title", 120), ("publication_year", 8),
                             ("doi", 100), ("url", 180)):
            value = source.get(field)
            if value is not None:
                candidate = {**entry, field: str(value)[:limit]}
                if len(json.dumps(candidate, ensure_ascii=False)) - initial_size <= source_budget:
                    entry = candidate
        budget -= len(json.dumps(entry, ensure_ascii=False)) - initial_size
        sources[index] = entry
    return {"sources": sources, "source_count": len(sources),
            "scope_note": "本表列出全部可用文献编号，缩短的元数据不替代冻结目录。"}


def _prompt_glossary(glossary: list[dict], *, max_chars: int = 60_000) -> dict:
    """Retain the term registry while bounding accumulated explanations."""
    terms = [{"term": str(item.get("term") or "")[:120],
              "explanation_mode": item.get("explanation_mode")} for item in glossary]
    essential_size = sum(len(json.dumps(item, ensure_ascii=False)) for item in terms)
    budget = max_chars - 1000 - essential_size
    if budget < 0:
        raise ValueError("rolling glossary term registry exceeds the bounded writing context")
    for index, original in enumerate(glossary):
        entry = terms[index]
        share = budget // (len(glossary) - index)
        initial_size = len(json.dumps(entry, ensure_ascii=False))
        for field, limit in (("explanation", 400), ("formula", 300)):
            value = original.get(field)
            if value:
                candidate = {**entry, field: str(value)[:limit]}
                if len(json.dumps(candidate, ensure_ascii=False)) - initial_size <= share:
                    entry = candidate
        budget -= len(json.dumps(entry, ensure_ascii=False)) - initial_size
        terms[index] = entry
    return {"terms": terms, "omitted_term_count": len(glossary) - len(terms),
            "scope_note": "滚动术语索引；说明和公式可能缩短，完整版本在冻结术语表。"}


def _fact_first_index(runner, module, dossier_path: Path, catalog_path: Path) -> dict:
    """Put substantive findings before bibliography in new meetings' writer input.

    This is a loss-limited navigation index, not a new evidence verdict. The
    immutable dossier retains complete findings and all omitted entries.
    """
    relative = _module_base(module) / "research/fact_first_index.json"
    path = runner.repo.root / relative
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    citation_by_key = {
        _source_identity_key(entry.get("doi"), entry["url"]): entry["citation_id"]
        for entry in catalog.get("sources", [])
    }
    cards = []
    gaps = []
    for packet in dossier.get("packets", []):
        source_by_id = {source["source_id"]: source for source in packet.get("sources", [])}
        citation_by_source = {
            source["source_id"]: citation_by_key.get(
                _source_identity_key(source.get("doi"), source["url"])
            )
            for source in packet.get("sources", [])
        }
        for direction, field in (
            ("SUPPORTING", "supporting_evidence"),
            ("CONTRADICTORY", "contradictory_evidence"),
            ("LIMITATION", "scope_limitations"),
            ("ALTERNATIVE", "canonical_alternatives"),
        ):
            for finding in packet.get(field, []):
                citation_id = citation_by_source.get(finding.get("source_id"))
                if not citation_id:
                    continue
                source = source_by_id.get(finding.get("source_id"), {})
                source_type = source.get("source_type")
                access_basis = (
                    "LOCAL_EXTRACTED_EXCERPT"
                    if source_type == "human_supplied_document"
                    else ("SEARCH_RESULT_SNIPPET" if source_type == "web_page"
                          else "METADATA_OR_ABSTRACT")
                )
                cards.append({
                    "claim": packet["normalized_claim"],
                    "finding": finding["evidence_summary"],
                    "direction": direction,
                    "applicability": finding["applicability"],
                    "limitations": finding["limitations"],
                    "citation_id": citation_id,
                    "source_access_basis": access_basis,
                    "packet_id_for_audit_only": packet["packet_id"],
                    "knowledge_status": packet["knowledge_status"],
                    "consensus": packet["consensus"],
                })
        if packet.get("unresolved_questions"):
            gaps.append({"claim": packet["normalized_claim"],
                         "questions": packet["unresolved_questions"]})
    payload = {
        "module_id": module.module_id,
        "policy": "Finding-first writing index; source IDs remain in the separate frozen citation catalog.",
        "card_count": len(cards),
        "cards": cards[:120],
        "omitted_card_count": max(0, len(cards) - 120),
        "unresolved_questions": gaps,
        "source_dossier_path": str(dossier_path.relative_to(runner.repo.root)),
    }
    _freeze(runner, relative, payload)
    return payload


def _literature_step(runner, module, stage: str, detail: str) -> None:
    index, total = runner._v071_progress
    runner.engine.progress.literature_step(
        section_id=module.module_id, section_index=index, section_total=total,
        title=module.title, stage=stage, detail=detail,
    )


def _outline_vote_round(runner, module, outline: ModuleWritingOutline,
                        round_number: int, team: list[dict]) -> dict:
    base = _module_base(module) / "writing_v071"
    private = Path("governance_private/literature_report/writing_v071") / module.module_id
    voters = [item for item in team if item["runtime"]["persona"] != Persona.SYSTEMS_INTEGRATOR.value]
    responses: list[dict] = []
    for record in voters:
        rid = record["representative_id"]
        relative = private / f"outline_vote_r{round_number}_{rid}.json"
        ballot = _frozen_or_call(
            runner, relative, OutlineBallot, rid,
            f"literature_v071_outline_vote_{module.module_id}_r{round_number}",
            "只审查模块写作提纲的行文结构、必要覆盖面和证据边界；不要提前写正文，"
            "也不要把提纲表决当成事实真伪表决。NO 必须指出一条具体可修改的问题。只返回 JSON。",
            {"module": module.model_dump(mode="json"), "outline": outline.model_dump(mode="json")},
        )
        responses.append({"representative_id": rid, **ballot.model_dump(mode="json")})
    yes = sum(item["vote"] == "YES" for item in responses)
    required = 3 if round_number == 1 else 2
    payload = {"round": round_number, "eligible_count": len(voters),
               "required_yes": required, "yes": yes, "adopted": yes >= required,
               "ballots": responses}
    _freeze(runner, base / f"outline_vote_r{round_number}.json", payload)
    return payload


def _approve_outline(runner, module, module_index: int, dossier_path: Path) -> Path:
    base = _module_base(module) / "writing_v071"
    approved = runner.repo.root / base / "approved_outline.json"
    if approved.is_file():
        return approved
    team = runner._select_drafting_team(module.module_id, "v071_outline", module_index)
    builder = next(item for item in team
                   if item["runtime"]["persona"] == Persona.SYSTEMS_INTEGRATOR.value)
    rid = builder["representative_id"]
    dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
    prior: dict | None = None
    for version in (1, 2, 3):
        _literature_step(runner, module, "outline", f"第 {version}/3 稿 · Builder 提纲与结构票")
        runner.engine.status.phase = MeetingPhase.LITERATURE_MODULE_DRAFTING
        runner.engine.progress.status(
            MeetingPhase.LITERATURE_MODULE_DRAFTING,
            f"{module.module_id} · Builder 写作提纲第 {version}/3 稿；仅规划结构，不写正文",
        )
        relative = base / f"outline_v{version}.json"
        outline = _frozen_or_call(
            runner, relative, ModuleWritingOutline, rid,
            f"literature_v071_outline_{module.module_id}_v{version}",
            "提交模块学术综述的写作提纲，不写正文或预先裁定科学结论。"
            "每步只说明行文目的、需解释的量或方法以及证据边界。"
            "不要堆砌文献清单，不要引入内部流程编号。"
            "dossier_evidence_view 是有界索引；省略项不表示没有相关证据，不据此作穷尽性断言。"
            + (_FACT_FIRST_OUTLINE_RULES if runner._writing_preferences().get("fact_first_writing") else "")
            + "只返回 JSON。",
            {"module": module.model_dump(mode="json"),
             "dossier_evidence_view": _science_review_evidence(dossier),
             "previous_vote": prior},
        )
        if version == 3:
            decision = {"policy": "THIRD_OUTLINE_AUTOMATICALLY_ADOPTED", "version": version}
            break
        prior = _outline_vote_round(runner, module, outline, version, team)
        if prior["adopted"]:
            decision = {"policy": "STRUCTURAL_BALLOT", "version": version,
                        "yes": prior["yes"], "required_yes": prior["required_yes"]}
            break
    _freeze(runner, base / "approved_outline.json", {
        "outline": outline.model_dump(mode="json"), "decision": decision,
        "nonbinding_style_and_scope_notes": [
            item["revision_note"] for item in (prior or {}).get("ballots", [])
            if item["vote"] == "NO" and item.get("revision_note")
        ] if decision["version"] == 2 else [],
    })
    return approved


def _writer_chapter(runner, module, version: int, dossier_path: Path,
                    outline_path: Path, *, previous: Path | None = None,
                    dispositions: Path | None = None,
                    review_path: Path | None = None) -> tuple[Path, WriterChapter]:
    base = _module_base(module) / "writing_v071"
    runner._ensure_chapter_citation_catalog(module, dossier_path)
    catalog_path = _effective_chapter_citation_catalog_path(
        runner.repo.root, module.module_id, before_writer_version=version,
    )
    fact_first = bool(runner._writing_preferences().get("fact_first_writing", False))
    prior_summaries = []
    for path in sorted((runner.repo.root / "public/literature_report/modules").glob("RM-*/module_outcome.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item["module_id"] < module.module_id:
            prior_summaries.append({"title": item["title"], "summary": item["short_summary"]})
    glossary_files = sorted(
        path for path in (runner.repo.root / "public/literature_report/writing_v071").glob(
            "glossary_after_RM-*.json"
        ) if path.stem.removeprefix("glossary_after_") < module.module_id
    )
    glossary = (json.loads(glossary_files[-1].read_text(encoding="utf-8"))
                if glossary_files else [])
    profile_files = sorted((runner.repo.root / "public/literature_report").glob(
        "audience_profile-*.json"
    ))
    audience_profile = (json.loads(profile_files[-1].read_text(encoding="utf-8"))
                        if profile_files else {})
    previous_payload = (
        WriterChapter.model_validate_json(previous.read_text(encoding="utf-8")).model_dump(mode="json")
        if previous is not None else None
    )
    decisions = json.loads(dispositions.read_text(encoding="utf-8")) if dispositions else None
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path else None
    revision_docket = []
    if review is not None:
        original_review = review
        if "reviews" not in original_review:
            original_path = (runner.repo.root / "public/literature_report/fast" /
                             module.module_id / "science_review_v1.json")
            if original_path.is_file():
                original_review = json.loads(original_path.read_text(encoding="utf-8"))
        for review_number, item in enumerate(original_review.get("reviews", []), 1):
            checklist = item.get("checklist") or {}
            for issue_number, issue in enumerate(checklist.get("issues", []), 1):
                revision_docket.append({
                    "issue_id": f"SCI-{review_number}-{issue_number}",
                    "location_excerpt": issue.get("location_excerpt"),
                    "questioned_claim": issue.get("questioned_claim"),
                    "why_it_matters": issue.get("why_it_matters"),
                    "suggested_response": issue.get("suggested_response"),
                    "status": "REQUIRES_EVIDENCE_CHECK_OR_REVISION",
                })
            for issue_number, issue in enumerate(checklist.get("glossary_corrections", []), 1):
                revision_docket.append({"issue_id": f"GLOSS-{review_number}-{issue_number}",
                                        "requested_correction": issue,
                                        "status": "REQUIRES_GLOSSARY_CHECK_OR_REVISION"})
        for vote_number, vote in enumerate(review.get("votes", []), 1):
            for issue_number, problem in enumerate(vote.get("remaining_material_problems", []), 1):
                revision_docket.append({"issue_id": f"LATEST-{vote_number}-{issue_number}",
                                        "current_objection": problem,
                                        "status": "PRIORITY_UNRESOLVED"})
    relative = base / f"writer_v{version}.json"
    _literature_step(runner, module, "draft", f"主笔第 {version} 稿")
    runner.engine.status.phase = MeetingPhase.LITERATURE_MODULE_DRAFTING
    runner.engine.progress.status(
        MeetingPhase.LITERATURE_MODULE_DRAFTING,
        f"{module.module_id} · 学术主笔撰写第 {version} 稿；使用已通过的提纲和本模块证据",
    )
    dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    evidence_input = (
        {"fact_first_index": _prompt_fact_index(
            _fact_first_index(runner, module, dossier_path, catalog_path))}
        if fact_first else
        {"module_evidence_view": _writer_evidence_view(dossier, catalog)}
    )
    chapter = _frozen_or_call(
        runner, relative, WriterChapter, "WRITER",
        f"literature_v071_writer_{module.module_id}_v{version}",
        "执行学术综述写作任务，不扮演会议代表或主席。按已通过提纲写当前模块。"
        "用清楚的因果、条件、转折和证据关系解释研究，而非制度条文口吻；"
        "涉及物理量或定量指标时，保留有来源的数学定义、符号、单位和测量窗口。"
        "数学排版是交稿纪律：凡含 =、≡、≈ 等等号或等价关系的完整公式，"
        "都要单独成行并使用 $$…$$，不要夹在正文句子里。"
        "不含等号的短变量或公式可以行内使用 \\(…\\)，"
        "希腊字母应在数学标记内写作 \\alpha、\\beta、\\gamma 等 TeX 命令，"
        "不要在普通正文裸写 Unicode 希腊字母；上下标也必须放在数学标记内。"
        "积分、求和或较长公式即使没有等号也宜单独成行。"
        "不要把一个公式拆成几个互不闭合的数学片段；中文积分域写在明确的下标中，"
        "例如 \\int_{\\mathrm{盒宽}}；JSON 字符串中的反斜杠必须正确转义。"
        "每个正文论断使用冻结目录中的具体 C 文献编号；知识卡的内部追溯由系统维护。"
        "输出中的 cited_packet_ids 留空，由系统根据正文 C 引文回填；"
        "若提出提纲偏离，只填写 new_evidence_citation_ids，不填写内部证据编号。"
        "只使用本模块证据；前文只用摘要和术语表，不能自行联网。"
        "必须逐条核对 revision_docket 中的科学异议，尤其 PRIORITY_UNRESOLVED。"
        "若异议成立，删除或改正原句，不可只添加笼统免责声明却保留未证实断言；"
        "若异议本身错误，必须找到具体的冻结来源支持或运行日期依据，不能为了迎合异议删去正确内容。"
        "在 revision_responses 中逐项写 issue_id、处理方式（已修订/有据保留/仍待查）和简短理由；"
        "这些回应只供内部复核，不能写进正文。保留未解决的证据边界。"
        "同时维护独立于正文篇幅预算的读者术语表。对本章首次出现、目标读者可能不熟悉的"
        "概念、方法、可观测量、模型参数、缩写和重要数学符号逐项给出真正可独立阅读的解释；"
        "先按‘缺少解释是否阻碍读者理解本章关键方法或判断’排序，而不是按术语是否常见于教科书排序。"
        "优先解释本章特有的算法与几何构造、控制参数、边界/邻接规则、操作性定义和关键缩写；"
        "对读者已熟悉的基础概念只在它的特殊用法可能造成误解时收录。"
        "正文用英文、希腊字母或不同译名表示同一概念时，在同一词条列出这些别名；"
        "解释方法名时写清输入、构造步骤、参数如何改变输出及本章适用边界，不能只解释名称中的普通词。"
        "不要只摘录含有该词的一句话，也不要用同义词循环定义。写清对象、定义或操作口径、"
        "适用条件，以及易与之混淆的概念；需要数学定义时给出完整公式、各符号含义与单位。"
        "必要的术语解释可比正文更详细，不为节省正文目标字数而省略；但不得编造未核实事实。"
        "有具体来源时在 source_citation_ids 中列本章冻结目录里的 C 文献 ID；"
        "若现有证据不足，须在正文与术语解释中明确限定，不能把猜测写成定义。"
        "已解释术语不要重复冗长定义。"
        "读者专业度按各学科的文字锚点逐一判断：当新概念超过对应学科读者的预期知识时，"
        "首次承担论证作用时给出足够继续阅读的解释，并在术语表保留完整定义；"
        "不要因为一个词高频出现就自动收录，也不要遗漏少见但决定结论的专门构造。"
        "正文首先给出每段的中心判断，再展开关键依据和必要边界。"
        "用完整主谓关系和必要的因果、比较、转折连接代替压缩名词链。"
        "路标应说明当前比较什么、为何转向下一步、哪些结论仍待解决；复杂处可更密集，"
        "但不得用路标新增因果推论或提升结论强度。"
        "同一证据边界第一次完整说明；后文无新边界时简短指回，不机械重复"
        "‘现有可核对材料’‘本章不能据此宣布’等审计腔。"
        "保留真正的异议、不确定性与限定；压缩的是重复，不是分歧。"
        "来源只读到摘要、次要实现约束等辅助信息可另写单行 Markdown 引用块，"
        "格式为 > **来源状态**：说明 或 > **实现说明**：说明；HTML 将其显示为小号[注]侧栏，"
        "PDF 保留原说明。只有正文已足以防止读者误解时才这样分层；"
        "凡会改变结论适用范围或证据强度的限定必须仍在正文直接可见。"
        "本章正文开头先用一段自然语言介绍本章将出现的关键新概念及它们与核心问题的关系；"
        "不要复制术语表、罗列词条或提前宣称未论证的结论。"
        "若读者对相关学科自评专业度为 1 或 2，或行文活泼性为 4 或 5，"
        "可用简短、贴切的类比帮助入门，但必须紧接准确的科学定义并说明类比的边界；"
        "类比不能替代公式、操作定义或证据。"
        "模块正文不要写总标题、全文章目录或其他模块正文。"
        "RM-01、RM-xx 等编号仅供内部流程定位；正文、章节标题、摘要和术语表"
        "一律使用读者能理解的章节主题，不输出这些内部编号。"
        "证据、文献目录和滚动术语表均为有界输入视图；省略项不等于证据不存在。"
        + (_FACT_FIRST_WRITING_RULES if fact_first else "")
        + "只返回 JSON。",
        _writer_visible_payload({"module": module.model_dump(mode="json"),
         "approved_outline": json.loads(outline_path.read_text(encoding="utf-8")),
         **evidence_input,
         "chapter_citation_catalog": _prompt_citation_catalog(catalog),
         "preceding_frozen_module_summaries": prior_summaries[-12:],
         "rolling_glossary": _prompt_glossary(glossary),
         "reader_proficiency_by_discipline": audience_profile.get("disciplines", {}),
         "reader_style_policy": reader_style_policy(
             runner._writing_preferences(), audience_profile.get("disciplines", {})),
         "previous_draft": previous_payload,
         "review_decisions": decisions,
         "prior_science_checklists": review,
         "revision_docket": revision_docket,
         "writing_preferences": runner._writing_preferences()}, catalog),
    )
    def citation_issues(candidate: WriterChapter) -> list[str]:
        prose = candidate.draft.body_markdown + "\n" + candidate.draft.short_summary
        used = set(_CHAPTER_SOURCE_MARKER.findall(prose))
        allowed_citations = {entry["citation_id"] for entry in catalog["sources"]}
        problems = []
        if _PACKET_MARKER.search(prose):
            problems.append("正文使用了内部证据编号；必须换成本章可引用的 C 文献编号")
        unknown = sorted(used - allowed_citations)
        if unknown:
            problems.append(f"文献编号不在本章冻结目录：{unknown}")
        unknown_glossary = sorted({citation_id for entry in candidate.glossary_additions
                                   for citation_id in entry.source_citation_ids} - allowed_citations)
        if unknown_glossary:
            problems.append(f"术语表来源编号不在本章冻结目录：{unknown_glossary}")
        if candidate.draft.cited_packet_ids and not used:
            problems.append("声明了证据来源，却没有任何正文 C 文献编号")
        return problems

    source_chapter = chapter
    chapter, mechanical_operations = _mechanically_repair_writer_citations(chapter, catalog)
    if mechanical_operations:
        _freeze(runner, base / f"writer_v{version}_citation_mechanical.json", {
            "source_sha256": hashlib.sha256(source_chapter.model_dump_json().encode()).hexdigest(),
            "operations": mechanical_operations,
            "result": chapter.model_dump(mode="json"),
            "policy": "PRESENTATION_ONLY; FROZEN_MODEL_RESPONSE_UNCHANGED",
        })
    problems = citation_issues(chapter)
    for attempt in range(1, 4):
        if not problems:
            break
        prior = chapter
        relative = (base / f"writer_v{version}_citation_repair.json" if attempt == 1 else
                    base / f"writer_v{version}_citation_repair_{attempt:02d}.json")
        chapter = _frozen_or_call(
            runner, relative, WriterChapter,
            "WRITER", f"literature_v071_writer_{module.module_id}_v{version}_citation_repair_{attempt}",
            "只修正本轮明确列出的文献引用和来源编号错误，不改科学论断、公式和结构。"
            "C 文献编号可用 [C...] 或【C...】；不要输出内部证据编号或来源备案行。"
            "只能使用冻结文献目录；若一张知识卡对应多篇文献，须据相邻具体论断选择真正支持它的来源，"
            "不能机械选第一篇。没有合格来源则限定或撤回该论断。返回完整修正后的 JSON。",
            _writer_visible_payload({
                "validation_problems": problems, "previous_draft": prior.model_dump(mode="json"),
                "chapter_citation_catalog": _prompt_citation_catalog(catalog),
            }, catalog),
        )
        chapter, operations = _mechanically_repair_writer_citations(chapter, catalog)
        if operations:
            _freeze(runner, base / f"writer_v{version}_citation_repair_{attempt:02d}_mechanical.json", {
                "operations": operations, "result": chapter.model_dump(mode="json"),
                "policy": "PRESENTATION_ONLY; FROZEN_MODEL_RESPONSE_UNCHANGED",
            })
        problems = citation_issues(chapter)
    if problems:
        # Several failed text-only corrections mean the source relationship,
        # rather than the citation syntax, may be missing. Re-run the original
        # packet questions and give the writer a supplementary source catalog.
        catalog, rechecked_claims = _recheck_writer_citation_evidence(
            runner, module, version, dossier, catalog, chapter,
        )
        if any(item["status"] == "RECHECKED" for item in rechecked_claims):
            catalog_path = _freeze(
                runner, base / f"writer_v{version}_citation_rechecked_catalog.json", catalog,
            )
            for attempt in range(1, 4):
                if not problems:
                    break
                relative = base / f"writer_v{version}_citation_recheck_repair_{attempt:02d}.json"
                chapter = _frozen_or_call(
                    runner, relative, WriterChapter, "WRITER",
                    f"literature_v071_writer_{module.module_id}_v{version}_citation_recheck_repair_{attempt}",
                    "已按原问题重新核查来源。只针对 validation_problems 修正引文；"
                    "用补充目录中的 C 文献编号与具体论断对齐，不输出内部证据编号。"
                    "新证据若仍不足，明确收窄或撤回相应断言，不猜测对应关系。"
                    "不改无关科学内容和结构。返回完整 JSON。",
                    _writer_visible_payload({"validation_problems": problems,
                     "previous_draft": chapter.model_dump(mode="json"),
                     "rechecked_claims": rechecked_claims,
                     "chapter_citation_catalog": _prompt_citation_catalog(catalog)}, catalog),
                )
                chapter, _ = _mechanically_repair_writer_citations(chapter, catalog)
                problems = citation_issues(chapter)
    human_cycle = 1
    while problems:
        service = HumanConsultationService(runner.repo)
        issue_id = f"HC-LW-{module.module_id}-CITATION-V{version}-C{human_cycle:02d}"
        service.open_issue(HumanConsultationIssue(
            issue_id=issue_id, meeting_id=runner.repo.meeting_id,
            reason_code="LITERATURE_WRITER_CITATION_REPAIR_REQUIRED",
            stage="LITERATURE_WRITER_CITATION_REPAIR",
            question=("主笔已多次按反馈修复，并按原证据包问题重新核查来源，"
                      "但仍有不能安全确认的引文对应关系。旧证据和已完成模块均保留；"
                      "可先更换主笔模型，再请求继续修复。"),
            options=["RETRY_WRITER_CITATION_REPAIR", "PAUSE_FOR_MANUAL_REVIEW"],
            affected_items=[module.module_id],
            context={"problems": problems, "catalog_path": str(catalog_path.relative_to(runner.repo.root)),
                     "last_draft_path": str(relative), "human_cycle": human_cycle},
        ))
        resolution = service.resolution(issue_id)
        if resolution is None:
            raise LiteratureWritingPaused("LITERATURE_WRITER_CITATION_REPAIR_REQUIRED")
        if resolution.decision != "RETRY_WRITER_CITATION_REPAIR":
            raise LiteratureWritingPaused("LITERATURE_WRITER_CITATION_REPAIR_REQUIRED")
        # A new immutable attempt follows the Human authorization. It can use
        # a replacement runtime selected before resume without redoing drafts.
        for retry_number in range(1, 4):
            if not problems:
                break
            relative = base / (
                f"writer_v{version}_citation_human_c{human_cycle:02d}_retry_{retry_number:02d}.json"
            )
            chapter = _frozen_or_call(
                runner, relative,
                WriterChapter, "WRITER",
                f"literature_v071_writer_{module.module_id}_v{version}_citation_human_c{human_cycle:02d}_retry_{retry_number}",
                "人类授权继续修复引文。逐项处理 validation_problems；仅用冻结目录中的确切来源，"
                "不得在正文输出内部证据编号；保持已核实的科学内容不变。只返回 JSON。",
                _writer_visible_payload({
                    "validation_problems": problems,
                    "previous_draft": chapter.model_dump(mode="json"),
                    "chapter_citation_catalog": _prompt_citation_catalog(catalog),
                }, catalog),
            )
            chapter, _ = _mechanically_repair_writer_citations(chapter, catalog)
            problems = citation_issues(chapter)
        human_cycle += 1
    _evaluate_outline_deviations(runner, module, version, chapter, outline_path, dossier_path)
    prior_draft = previous_payload["draft"] if previous_payload else None
    previous_module_draft = ModuleDraft.model_validate(prior_draft) if prior_draft else None
    # A Writer sees C source IDs but not internal RP packet IDs. Some providers
    # consequently put the visible C IDs in the legacy cited_packet_ids field.
    # Resolve those against the frozen source catalog before model-prior checks,
    # whose generic packet validator otherwise mistakes them for unknown RP IDs.
    source_normalized_draft = runner._validate_chapter_source_citations(
        module, chapter.draft, catalog_path,
        previous_draft=previous_module_draft,
    )
    draft = runner._verify_model_prior_claims(
        module=module, version=f"v071-v{version}", draft=source_normalized_draft,
        requester_id="WRITER",
    )
    draft = runner._validate_chapter_source_citations(
        module, draft, catalog_path,
        previous_draft=previous_module_draft,
    )
    runner._validate_citations(draft.cited_packet_ids)
    frozen = chapter.model_copy(update={"draft": draft})
    normalized_relative = base / f"writer_v{version}_validated.json"
    _freeze(runner, normalized_relative, frozen)
    draft_relative = _module_base(module) / "drafts" / f"v071-v{version}.json"
    draft_path = _freeze(runner, draft_relative, draft)
    return draft_path, frozen


def _evaluate_outline_deviations(runner, module, version: int,
                                 chapter: WriterChapter, outline_path: Path,
                                 dossier_path: Path) -> Path:
    relative = _module_base(module) / "writing_v071" / f"outline_deviations_v{version}.json"
    path = runner.repo.root / relative
    if path.is_file():
        return path
    outline = ModuleWritingOutline.model_validate(
        json.loads(outline_path.read_text(encoding="utf-8"))["outline"]
    )
    dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
    packets = {item["packet_id"]: item for item in dossier.get("packets", [])}
    catalog_path = _effective_chapter_citation_catalog_path(runner.repo.root, module.module_id)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    sources_by_citation = {source["citation_id"]: source for source in catalog["sources"]}
    outcomes = []
    for index, item in enumerate(chapter.outline_deviations, start=1):
        if item.outline_step_number > len(outline.steps):
            raise ValueError("Writer outline deviation refers to a non-existent step")
        unknown_citations = set(item.new_evidence_citation_ids) - sources_by_citation.keys()
        if unknown_citations:
            raise ValueError(f"Writer outline deviation cites unknown literature: {sorted(unknown_citations)}")
        evidence_ids = list(dict.fromkeys([
            *item.new_evidence_packet_ids,
            *(packet_id for citation_id in item.new_evidence_citation_ids
              for packet_id in sources_by_citation[citation_id]["packet_ids"]),
        ]))
        for packet_id in evidence_ids:
            if packet_id not in packets:
                packets[packet_id] = runner._load_packet(packet_id).model_dump(mode="json")
        if not set(evidence_ids) <= packets.keys():
            raise ValueError("Writer outline deviation cites evidence outside the module dossier")
        librarians = _science_librarians(
            runner, stage=f"literature_v071_outline_deviation_{module.module_id}_v{version}_{index:02d}",
        )
        votes = []
        for record in librarians:
            rid = record["representative_id"]
            vote = _frozen_or_call(
                runner,
                Path("governance_private/literature_report/writing_v071") / module.module_id
                / f"outline_deviation_v{version}_{index:02d}_{rid}.json",
                OutlineEvidenceVote, rid,
                f"literature_v071_outline_deviation_{module.module_id}_v{version}_{index:02d}",
                "只判断后来取得的证据是否足以构成对已通过提纲讨论范围的有效质疑。"
                "这不是对新科学结论真伪或新正文的表决，只投 YES 或 NO。只返回 JSON。",
                {"outline_step": outline.steps[item.outline_step_number - 1].model_dump(mode="json"),
                 "writer_deviation": item.model_dump(mode="json"),
                 "local_evidence": _science_review_evidence(
                     {**dossier, "packets": list(packets.values())},
                     packet_ids=set(evidence_ids), max_chars=80_000)},
            )
            votes.append({"representative_id": rid, "vote": vote.vote})
        yes = sum(vote["vote"] == "YES" for vote in votes)
        outcomes.append({"item": item.model_dump(mode="json"), "yes": yes,
                         "required_yes": len(librarians) // 2 + 1,
                         "effective_challenge": yes > len(librarians) / 2,
                         "effect": "WRITER_MAY_LOCALLY_DEVIATE" if yes > len(librarians) / 2
                         else "ADVISORY_PREFER_ORIGINAL_SCOPE_NOT_MANDATORY",
                         "votes": votes})
    _freeze(runner, relative, {"module_id": module.module_id, "version": version,
                               "outcomes": outcomes})
    return path


def _science_round(runner, module, version: int, chapter: WriterChapter,
                   dossier_path: Path, prior_docket: Path | None) -> Path:
    _literature_step(runner, module, "science", f"第 {version}/2 轮 · 智库长密封科学清单")
    base = _module_base(module) / "writing_v071"
    public_relative = base / f"science_round_{version}.json"
    public_path = runner.repo.root / public_relative
    if public_path.is_file():
        return public_path
    librarians = _science_librarians(
        runner, stage=f"literature_v071_science_{module.module_id}_r{version}",
    )
    private = Path("governance_private/literature_report/writing_v071") / module.module_id
    prior = json.loads(prior_docket.read_text(encoding="utf-8")) if prior_docket else None
    dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
    # A full chapter plus an unrestricted evidence excerpt can consume the
    # provider's entire reasoning allowance before it emits a checklist.
    # Keep this review view smaller than the archival dossier; omitted counts
    # remain explicit so an omission cannot be mistaken for absent evidence.
    review_evidence = _science_review_evidence(dossier, chapter, max_chars=120_000)
    profile_files = sorted((runner.repo.root / "public/literature_report").glob(
        "audience_profile-*.json"))
    audience_profile = (json.loads(profile_files[-1].read_text(encoding="utf-8"))
                        if profile_files else {})
    prior_glossaries = sorted(
        path for path in (runner.repo.root / "public/literature_report/writing_v071").glob(
            "glossary_after_RM-*.json")
        if path.stem.removeprefix("glossary_after_") < module.module_id
    )
    known_terms = ([item.get("term", "") for item in json.loads(
        prior_glossaries[-1].read_text(encoding="utf-8"))]
        if prior_glossaries else [])
    complete: dict[str, ScienceChecklist] = {}

    def worker(record: dict) -> tuple[str, ScienceChecklist]:
        rid = record["representative_id"]
        relative = private / f"science_round_{version}_{rid}.json"
        review = _frozen_or_call(
            runner, relative, ScienceChecklist, rid,
            f"literature_v071_science_{module.module_id}_r{version}",
            "核查当前模块草稿的科学性和来源边界。每条异议仅针对一个具体位置和一个科学判断；"
            "指出为何重要及关联证据，不重写整章。纯风格意见不要充当科学异议。"
            "若上一轮已解决，不重复提出；若返修稿仍有问题，指向当前文本。"
            "检查术语表是否遗漏本章理解所必需的概念，解释是否真正定义对象、条件和符号，"
            "优先检查读者专业度较低的学科中、本章关键结论所依赖的专门算法、几何构造、参数与判据；"
            "不要因为解释了相关普通词或相近方法，就视为关键方法本身已有定义。"
            "以及开头类比是否误导；把可定位的缺漏或错误写入 glossary_corrections。"
            "不要为了增加词条数量而重复常识或已定义术语。"
            "review_evidence 只包含正文所引证据包的有界发现摘录；"
            "omitted_finding_count 大于零表示还有未呈递发现，不能据此断言原档案没有证据。"
            "若局部信息不足，指出待核查的具体陈述，不臆断完整文献。"
            "只报告最可能改变本章结论或证据边界的具体问题；不要逐包复述或输出推理过程。"
            "每项异议的理由和建议各用简短句子表达；无实质问题返回空清单。只返回 JSON。",
            {"module": module.model_dump(mode="json"),
             "current_draft": chapter.draft.model_dump(mode="json"),
             "glossary_additions": [x.model_dump(mode="json") for x in chapter.glossary_additions],
             "known_glossary_terms": known_terms,
             "reader_proficiency_by_discipline": audience_profile.get("disciplines", {}),
             "reader_style_policy": reader_style_policy(
                 runner._writing_preferences(), audience_profile.get("disciplines", {})),
             "review_evidence": review_evidence, "prior_round_and_dispositions": prior},
        )
        valid = [issue for issue in review.issues
                 if issue.location_excerpt in chapter.draft.body_markdown]
        if len(valid) != len(review.issues):
            runner.repo.events.append("LITERATURE_SCIENCE_ISSUE_LOCATION_REJECTED", {
                "module_id": module.module_id, "round": version, "participant_id": rid,
                "invalid_count": len(review.issues) - len(valid),
                "reason": "excerpt not present in the current draft",
            }, actor="orchestrator")
        return rid, review.model_copy(update={"issues": valid})

    def persist(result: tuple[str, ScienceChecklist]) -> None:
        complete[result[0]] = result[1]

    restored = {}
    for record in librarians:
        rid = record["representative_id"]
        path = runner.repo.root / private / f"science_round_{version}_{rid}.json"
        if path.is_file():
            original = ScienceChecklist.model_validate_json(path.read_text(encoding="utf-8"))
            restored[rid] = original.model_copy(update={
                "issues": [item for item in original.issues
                           if item.location_excerpt in chapter.draft.body_markdown]
            })
    complete.update(restored)
    pending = [record for record in librarians if record["representative_id"] not in restored]
    run_bounded_representative_lanes(
        pending, worker, runner.engine.model_concurrency_limit,
        on_result=persist, progress=runner.engine.progress,
        batch_title=f"{module.module_id} · 第 {version}/2 轮 · 全体智库长密封科学审阅",
        progress_records=librarians, completed_participant_ids=restored,
    )
    entries = []
    private_authorship = []
    for record in librarians:
        rid = record["representative_id"]
        checklist = complete[rid]
        label = f"L{len(entries)+1:02d}"
        entries.append({"reviewer_label": label, **checklist.model_dump(mode="json")})
        private_authorship.append({"reviewer_label": label, "representative_id": rid})
    _freeze(runner, private / f"science_authorship_r{version}.json", private_authorship)
    _freeze(runner, public_relative, {"module_id": module.module_id, "round": version,
                                     "reviews": entries, "status": "FROZEN_RELEASED_ATOMICALLY"})
    return public_path


def _issue_docket(runner, module, version: int, review_path: Path) -> Path:
    base = _module_base(module) / "writing_v071"
    docket_relative = base / f"issue_docket_r{version}.json"
    path = runner.repo.root / docket_relative
    if path.is_file():
        return path
    reviews = json.loads(review_path.read_text(encoding="utf-8"))["reviews"]
    raw: list[dict] = []
    for review in reviews:
        for issue in review["issues"]:
            raw.append({"source_issue_id": f"SCI-R{version}-S{len(raw)+1:03d}",
                        "reviewer_label": review["reviewer_label"], "issue": issue})
    _freeze(runner, base / f"raw_issues_r{version}.json", raw)
    grouped = []
    if raw:
        grouping = _frozen_or_call(
            runner, base / f"chair_issue_grouping_r{version}.json",
            ScienceIssueGrouping, "CHAIR",
            f"literature_v071_issue_grouping_{module.module_id}_r{version}",
            "只把同一正文位置、同一被质疑科学判断的异议归为一项。"
            "不同位置或不同科学判断不可强行合并；每条源异议恰好出现一次。"
            "保留原始提出者和原文到审计层，不自行裁定异议科学真伪。只返回 JSON。",
            {"module": module.model_dump(mode="json"), "raw_issues": raw},
        )
        all_ids = [source_id for cluster in grouping.clusters for source_id in cluster.source_issue_ids]
        expected_ids = [item["source_issue_id"] for item in raw]
        if len(all_ids) != len(set(all_ids)) or set(all_ids) != set(expected_ids):
            raise ValueError("Chair science grouping must partition every raw issue exactly once")
        by_id = {item["source_issue_id"]: item for item in raw}
        for cluster in grouping.clusters:
            members = [by_id[item] for item in cluster.source_issue_ids]
            grouped.append({
                "issue_id": f"SCI-R{version}-{len(grouped)+1:03d}",
                "issue": members[0]["issue"],
                "source_issue_ids": cluster.source_issue_ids,
                "proposer_labels": list(dict.fromkeys(item["reviewer_label"] for item in members)),
                "grouping_rationale": cluster.rationale,
            })
    _freeze(runner, docket_relative, {"module_id": module.module_id, "round": version,
                                      "issues": grouped})
    return path


def _rescue_rejection(runner, module, version: int, issue: dict,
                      authorship: dict[str, str], librarians: list[dict]) -> dict:
    private = Path("governance_private/literature_report/writing_v071") / module.module_id
    issue_id = issue["issue_id"]
    relative = private / f"rescue_r{version}_{issue_id}.json"
    path = runner.repo.root / relative
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if hasattr(runner, "active") and len(librarians) == 1:
        librarians = _science_librarians(
            runner, stage=f"literature_v071_rescue_{module.module_id}_r{version}_{issue_id}",
        )
    proposers = set(issue["proposer_labels"])
    if len(proposers) > len(librarians) / 2:
        outcome = {"issue_id": issue_id, "adopted": True,
                   "basis": "INDEPENDENT_LIBRARIAN_MAJORITY_AUTOMATIC_RESCUE",
                   "proposer_count": len(proposers), "eligible_count": len(librarians)}
        _freeze(runner, relative, outcome)
        return outcome
    excluded = {authorship[label] for label in proposers}
    voters = [record for record in librarians if record["representative_id"] not in excluded]
    if not voters:
        voters = [{"representative_id": "WRITER"}]
        fallback = True
    else:
        fallback = False
    votes = []
    for voter in voters:
        rid = voter["representative_id"]
        vote = _frozen_or_call(
            runner, private / f"rescue_r{version}_{issue_id}_{rid}.json",
            RescueVote, rid,
            f"literature_v071_rescue_{module.module_id}_r{version}_{issue_id}",
            "这只是程序性救济票：判断主席打回的具体科学异议是否应交主笔回应，"
            "不是判断该异议科学上为真，也不裁定正文措辞。只投 YES 或 NO；附言匿名。",
            {"issue": issue["issue"], "chair_rejection": issue.get("chair_rejection"),
             "proposer_count": len(proposers), "eligible_count": len(voters)},
        )
        votes.append({"participant_id": rid, **vote.model_dump(mode="json")})
    yes = sum(item["vote"] == "YES" for item in votes)
    threshold = len(voters) // 2 + 1
    outcome = {"issue_id": issue_id, "adopted": yes >= threshold,
               "basis": "WRITER_PROCEDURAL_FALLBACK" if fallback else "NON_PROPOSING_LIBRARIAN_VOTE",
               "yes": yes, "required_yes": threshold, "eligible_count": len(voters),
               "anonymous_notes": [item["anonymous_note"] for item in votes if item["anonymous_note"]]}
    _freeze(runner, relative, outcome)
    return outcome


def _dispose_issues(runner, module, version: int, docket_path: Path) -> Path:
    base = _module_base(module) / "writing_v071"
    relative = base / f"dispositions_r{version}.json"
    path = runner.repo.root / relative
    if path.is_file():
        return path
    docket = json.loads(docket_path.read_text(encoding="utf-8"))
    issues = docket["issues"]
    if not issues:
        _freeze(runner, relative, {"module_id": module.module_id, "round": version,
                                   "decisions": []})
        return path
    chair = _frozen_or_call(
        runner, base / f"chair_dispositions_r{version}.json", ChairIssueDocket,
        "CHAIR", f"literature_v071_issue_disposition_{module.module_id}_r{version}",
        "逐条处理科学异议。ACCEPT_FOR_RESPONSE 只要求主笔回应和必要返修，"
        "不等于认定异议为真；REJECT 必须具体说明依据。"
        "主席不自行增写科学内容或更改冻结证据。只返回 JSON。",
        {"module": module.model_dump(mode="json"), "issue_docket": docket},
    )
    by_id = {item.issue_id: item for item in chair.decisions}
    if len(by_id) != len(chair.decisions) or set(by_id) != {item["issue_id"] for item in issues}:
        raise ValueError("Chair must decide each frozen science issue exactly once")
    authorship = {item["reviewer_label"]: item["representative_id"] for item in
                  json.loads((runner.repo.root / "governance_private/literature_report/writing_v071"
                              / module.module_id / f"science_authorship_r{version}.json").read_text(encoding="utf-8"))}
    # The review's frozen authorship, not today's standby rule, determines its
    # rescue electorate. This preserves the original threshold for rounds
    # already sealed under the earlier all-Librarian procedure.
    authored_ids = set(authorship.values())
    librarians = [record for record in runner.active
                  if record["representative_id"] in authored_ids]
    if len(librarians) != len(authored_ids):
        raise ValueError("a frozen science reviewer is missing from the active roster")
    decisions = []
    for issue in issues:
        item = by_id[issue["issue_id"]]
        decision = {"issue_id": issue["issue_id"], "action": item.action,
                    "chair_rationale": item.rationale, "issue": issue["issue"]}
        if item.action == "REJECT":
            decision["rescue"] = _rescue_rejection(
                runner, module, version,
                {**issue, "chair_rejection": item.rationale}, authorship, librarians,
            )
            if decision["rescue"]["adopted"]:
                decision["action"] = "ACCEPT_FOR_RESPONSE"
        decisions.append(decision)
    _freeze(runner, relative, {"module_id": module.module_id,
                               "round": version, "decisions": decisions})
    return path


def _changed_paragraphs(before: str, after: str) -> list[dict]:
    original = [item.strip() for item in re.split(r"\n\s*\n", before) if item.strip()]
    revised = [item.strip() for item in re.split(r"\n\s*\n", after) if item.strip()]
    changes = []
    for tag, a0, a1, b0, b1 in SequenceMatcher(None, original, revised).get_opcodes():
        if tag != "equal":
            changes.append({"action": tag, "old": "\n\n".join(original[a0:a1]),
                            "new": "\n\n".join(revised[b0:b1])})
    return changes


def _local_science_check(runner, module, prior: WriterChapter,
                         final: WriterChapter, dossier_path: Path,
                         *, check_number: int = 1) -> Path:
    _literature_step(runner, module, "science", f"第 {check_number} 次局部返修核验 · 只读改动段落")
    base = _module_base(module) / "writing_v071"
    relative = base / f"final_local_science_check_{check_number}.json"
    path = runner.repo.root / relative
    if path.is_file():
        return path
    changes = _changed_paragraphs(prior.draft.body_markdown, final.draft.body_markdown)
    prior_glossary = [item.model_dump(mode="json") for item in prior.glossary_additions]
    final_glossary = [item.model_dump(mode="json") for item in final.glossary_additions]
    glossary_changed = prior_glossary != final_glossary
    if not changes and not glossary_changed:
        _freeze(runner, relative, {"status": "PASS", "reason": "NO_CHANGED_PARAGRAPHS_OR_GLOSSARY",
                                   "checks": []})
        return path
    dossiers = json.loads(dossier_path.read_text(encoding="utf-8"))
    relevant_ids = set(prior.draft.cited_packet_ids) | set(final.draft.cited_packet_ids)
    glossary_citations = {citation_id for term in [*prior.glossary_additions,
                                                     *final.glossary_additions]
                          for citation_id in term.source_citation_ids}
    citation_catalog = (runner.repo.root / _module_base(module)
                        / "research/chapter_citation_catalog.json")
    if glossary_citations and citation_catalog.is_file():
        for source in json.loads(citation_catalog.read_text(encoding="utf-8")).get("sources", []):
            if source["citation_id"] in glossary_citations:
                relevant_ids.update(source.get("packet_ids", []))
    review_evidence = _science_review_evidence(
        dossiers, packet_ids=relevant_ids, max_chars=180_000,
    )
    runner.engine.status.phase = MeetingPhase.LITERATURE_MODULE_REVIEW
    runner.engine.progress.status(
        MeetingPhase.LITERATURE_MODULE_REVIEW,
        f"{module.module_id} · 第 {check_number} 次局部科学复核；只核对改动段落、术语表与相关证据",
    )
    checks = []
    for record in _science_librarians(
        runner, stage=f"literature_v071_local_check_{module.module_id}_{check_number}",
    ):
        rid = record["representative_id"]
        check = _frozen_or_call(
            runner,
            Path("governance_private/literature_report/writing_v071") / module.module_id
            / f"local_check_{check_number}_{rid}.json",
            LocalCheck, rid, f"literature_v071_local_check_{module.module_id}_{check_number}",
            "只核对第二轮科学审阅之后改动的段落、术语定义及其引用证据。"
            "检查返修是否忠实回应异议且没有引入新的实质错误；"
            "review_evidence 是相关证据的有界摘录，省略项不等于没有证据。"
            "不要重开整章审阅或要求扩展研究范围。只返回 JSON。",
            {"module": module.model_dump(mode="json"), "changed_paragraphs": changes,
             "review_evidence": review_evidence,
             "prior_glossary_additions": prior_glossary,
             "final_glossary_additions": final_glossary},
        )
        checks.append({"representative_id": rid, **check.model_dump(mode="json")})
    status = "PASS" if all(item["status"] == "PASS" for item in checks) else "MATERIAL_PROBLEM"
    _freeze(runner, relative, {"status": status, "changes_sha256": hashlib.sha256(
        json.dumps({"paragraphs": changes, "glossary": final_glossary if glossary_changed else None},
                   ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest(), "checks": checks})
    return path


def _resolve_local_science_check(runner, module, prior: WriterChapter,
                                 final: WriterChapter, dossier_path: Path,
                                 outline_path: Path) -> tuple[Path, WriterChapter, Path]:
    """Bounded local remedy; unresolved findings are an explicit Human pause."""
    base = _module_base(module)
    for check_number in (1, 2):
        check_path = _local_science_check(
            runner, module, prior, final, dossier_path, check_number=check_number,
        )
        check = json.loads(check_path.read_text(encoding="utf-8"))
        if check["status"] == "PASS":
            return (runner.repo.root / base / "drafts" / (
                "v071-v3.json" if check_number == 1 else "v071-v4.json"
            ), final, check_path)
        issue_id = f"HC-LW-{module.module_id}-LOCAL-{check_number:02d}"
        service = HumanConsultationService(runner.repo)
        service.open_issue(HumanConsultationIssue(
            issue_id=issue_id, meeting_id=runner.repo.meeting_id,
            reason_code="LITERATURE_LOCAL_SCIENCE_CHECK_REQUIRED",
            stage="LITERATURE_MODULE_REVIEW",
            question=("智库长发现最终局部返修仍可能存在实质科学问题。"
                      "请选择要求主笔只修正这些段落，或在人类知悉风险后保留当前文本。"),
            options=(["RETURN_TO_WRITER_LOCAL_REPAIR", "PUBLISH_WITH_DISCLOSED_LIMITATION"]
                     if check_number == 1 else
                     ["PAUSE_FOR_MANUAL_REVIEW", "PUBLISH_WITH_DISCLOSED_LIMITATION"]),
            affected_items=[module.module_id],
            context={"check_path": str(check_path.relative_to(runner.repo.root)),
                     "problems": [item["problem"] for item in check.get("checks", [])
                                  if item.get("status") == "MATERIAL_PROBLEM"]},
        ))
        resolution = service.resolution(issue_id)
        if resolution is None:
            raise LiteratureWritingPaused("LITERATURE_LOCAL_SCIENCE_CHECK_REQUIRED")
        if resolution.decision == "PUBLISH_WITH_DISCLOSED_LIMITATION":
            return runner.repo.root / base / "drafts" / (
                "v071-v3.json" if check_number == 1 else "v071-v4.json"
            ), final, check_path
        if resolution.decision == "PAUSE_FOR_MANUAL_REVIEW":
            # Older clients persisted "stay paused" as a final resolution.
            # Give those meetings a fresh, append-only consultation instead of
            # replaying the same exhausted decision forever on every resume.
            followup_id = f"{issue_id}-REOPEN"
            service.open_issue(HumanConsultationIssue(
                issue_id=followup_id, meeting_id=runner.repo.meeting_id,
                reason_code="LITERATURE_LOCAL_SCIENCE_CHECK_REOPENED",
                stage="LITERATURE_MODULE_REVIEW",
                question=("此前选择了保持人工审阅，尚未决定是否采用当前稿。"
                          "请查看已记录的具体科学疑点；如知悉风险后愿保留当前稿，"
                          "可批准附限制说明继续。否则不作选择，会议继续暂停。"),
                options=["PUBLISH_WITH_DISCLOSED_LIMITATION", "PAUSE_FOR_MANUAL_REVIEW"],
                affected_items=[module.module_id],
                context={"check_path": str(check_path.relative_to(runner.repo.root)),
                         "problems": [item["problem"] for item in check.get("checks", [])
                                      if item.get("status") == "MATERIAL_PROBLEM"]},
            ))
            followup = service.resolution(followup_id)
            if followup is None:
                raise LiteratureWritingPaused("LITERATURE_LOCAL_SCIENCE_CHECK_REOPENED")
            if followup.decision == "PUBLISH_WITH_DISCLOSED_LIMITATION":
                return runner.repo.root / base / "drafts" / "v071-v4.json", final, check_path
            raise ValueError("invalid reopened Human local science resolution")
        if resolution.decision != "RETURN_TO_WRITER_LOCAL_REPAIR":
            raise ValueError("invalid Human local science resolution")
        prior = final
        _, final = _writer_chapter(
            runner, module, 4, dossier_path, outline_path,
            previous=runner.repo.root / base / "writing_v071/writer_v3_validated.json",
            review_path=check_path,
        )
    raise AssertionError("local science check loop exhausted")


def _freeze_glossary(runner, module, chapter: WriterChapter) -> Path:
    base = Path("public/literature_report/writing_v071")
    relative = base / f"glossary_after_{module.module_id}.json"
    path = runner.repo.root / relative
    if path.is_file():
        return path
    prior_paths = sorted(
        item for item in (runner.repo.root / base).glob("glossary_after_RM-*.json")
        if item.stem.removeprefix("glossary_after_") < module.module_id
    )
    prior = json.loads(prior_paths[-1].read_text(encoding="utf-8")) if prior_paths else []
    by_term = {item["term"].casefold(): item for item in prior}
    for term in chapter.glossary_additions:
        # The Writer may use an English name, symbol, or alternative translation
        # in the chapter; exact string equality silently drops such definitions.
        # Relevance is checked by the independent science reviewers instead.
        by_term[term.term.casefold()] = term.model_dump(mode="json")
    _freeze(runner, relative, list(by_term.values()))
    return path


def run_module_v071(runner, outline, module, module_index: int, dossier_path: Path) -> dict:
    """Run the new writing stage; prior retrieval and existing meetings are untouched."""
    base = _module_base(module)
    outcome_path = runner.repo.root / base / "module_outcome.json"
    if outcome_path.is_file():
        return json.loads(outcome_path.read_text(encoding="utf-8"))
    runner._v071_progress = (module_index, len(outline.modules) if outline is not None else module_index)
    approved_outline = _approve_outline(runner, module, module_index, dossier_path)
    draft1, chapter1 = _writer_chapter(runner, module, 1, dossier_path, approved_outline)
    runner.engine.status.phase = MeetingPhase.LITERATURE_MODULE_REVIEW
    runner.engine.progress.status(MeetingPhase.LITERATURE_MODULE_REVIEW,
                                  f"{module.module_id} · 第 1/2 轮智库长科学异议清单")
    review1 = _science_round(runner, module, 1, chapter1, dossier_path, None)
    docket1 = _issue_docket(runner, module, 1, review1)
    decisions1 = _dispose_issues(runner, module, 1, docket1)
    first_decisions = json.loads(decisions1.read_text(encoding="utf-8"))["decisions"]
    first_glossary = any(item["glossary_corrections"] for item in
                         json.loads(review1.read_text(encoding="utf-8"))["reviews"])
    if any(item["action"] == "ACCEPT_FOR_RESPONSE" for item in first_decisions) or first_glossary:
        draft2, chapter2 = _writer_chapter(
            runner, module, 2, dossier_path, approved_outline,
            previous=runner.repo.root / base / "writing_v071/writer_v1_validated.json",
            dispositions=decisions1,
            review_path=review1,
        )
    else:
        draft2 = _freeze(runner, base / "drafts/v071-v2.json", chapter1.draft)
        _freeze(runner, base / "writing_v071/writer_v2_validated.json", chapter1)
        chapter2 = chapter1
    runner.engine.progress.status(MeetingPhase.LITERATURE_MODULE_REVIEW,
                                  f"{module.module_id} · 第 2/2 轮智库长科学异议清单；避免重复已解决问题")
    review2 = _science_round(runner, module, 2, chapter2, dossier_path, decisions1)
    docket2 = _issue_docket(runner, module, 2, review2)
    decisions2 = _dispose_issues(runner, module, 2, docket2)
    second_decisions = json.loads(decisions2.read_text(encoding="utf-8"))["decisions"]
    second_glossary = any(item["glossary_corrections"] for item in
                          json.loads(review2.read_text(encoding="utf-8"))["reviews"])
    if any(item["action"] == "ACCEPT_FOR_RESPONSE" for item in second_decisions) or second_glossary:
        final_path, final = _writer_chapter(
            runner, module, 3, dossier_path, approved_outline,
            previous=runner.repo.root / base / "writing_v071/writer_v2_validated.json",
            dispositions=decisions2,
            review_path=review2,
        )
    else:
        final_path, final = draft2, chapter2
    if final_path == draft2:
        # A skipped third Writer revision has no changed paragraphs to recheck.
        check_path = _local_science_check(runner, module, chapter2, final, dossier_path)
    else:
        final_path, final, check_path = _resolve_local_science_check(
            runner, module, chapter2, final, dossier_path, approved_outline,
        )
    _literature_step(runner, module, "citation", "确定性引文与术语核验；冻结模块")
    _freeze_glossary(runner, module, final)
    outcome = {"module_id": module.module_id, "title": module.title,
               "status": "ADOPTED", "draft_path": str(final_path.relative_to(runner.repo.root)),
               "short_summary": final.draft.short_summary,
               "cited_packet_ids": final.draft.cited_packet_ids,
               "unresolved_ids": final.draft.unresolved_ids,
               "dissents_path": None,
               "writing_policy": "v071", "approved_outline_path": str(approved_outline.relative_to(runner.repo.root)),
               "local_science_check_path": str(check_path.relative_to(runner.repo.root)),
               "local_science_check_status": json.loads(
                   check_path.read_text(encoding="utf-8")
               )["status"],
               "glossary_path": str((Path("public/literature_report/writing_v071")
                                      / f"glossary_after_{module.module_id}.json"))}
    _freeze(runner, base / "module_outcome.json", outcome)
    runner.repo.events.append("LITERATURE_MODULE_FROZEN_V071",
                              {"meeting_id": runner.repo.meeting_id, **outcome}, actor="orchestrator")
    return outcome


def finalize_report_v071(runner, outline, completed: list[dict], contested: int):
    """Use the same Writer for short global framing, then publish frozen modules."""
    runner.engine.status.phase = MeetingPhase.LITERATURE_REPORT_SYNTHESIS
    runner.engine.progress.status(
        MeetingPhase.LITERATURE_REPORT_SYNTHESIS,
        "学术主笔正在根据冻结模块摘要撰写全篇导读；模块正文不重新生成",
    )
    base = Path("public/literature_report/synthesis")
    relative = base / "v071_writer.json"
    summaries = [{"module_id": item["module_id"], "title": item["title"],
                  "short_summary": item["short_summary"], "status": item["status"]}
                 for item in completed]
    glossary_path = (runner.repo.root / completed[-1]["glossary_path"]
                     if completed and completed[-1].get("glossary_path") else None)
    glossary_terms = ([item.get("term", "") for item in json.loads(
        glossary_path.read_text(encoding="utf-8"))]
        if glossary_path and glossary_path.is_file() else [])
    synopsis = _frozen_or_call(
        runner, relative, WholeReportSynthesis, "WRITER",
        "literature_v071_whole_report_framing",
        "根据已经冻结的模块摘要组织一篇文章的标题、可选摘要、引言和必要的结尾。"
        "body_sections 必须恰有一个 MODULES 插入槽；模块正文将机械插入。"
        "不默认加跨模块综合章，只有批准的总纲安排了才写。"
        "摘要与导言是快速导读，科学核校强度低于正文，应提示读者以审阅过的正文为准。"
        "不要在导读或跨模块综合中突然引入术语表未解释的关键专门方法名；"
        "确需使用时，就地用简短语言说明其对象、操作含义和适用边界，不得据此增添未经核查的新事实。"
        "不得扩写未验证事实、代写或改写冻结模块。只返回 JSON。",
        {"approved_global_outline": outline.model_dump(mode="json"),
         "frozen_module_summaries": summaries,
         "frozen_glossary_terms": glossary_terms,
         "writing_preferences": runner._writing_preferences()},
    )
    if not synopsis.body_sections or sum(item.kind == "MODULES" for item in synopsis.body_sections) != 1:
        raise ValueError("v0.7.1 report framing must contain exactly one MODULES slot")
    synopsis = runner._verify_synthesis_model_priors(
        synopsis, requester_id="WRITER", version="v071"
    )
    approved_relative = base / "v071_approved.json"
    approved_path = runner.repo.root / approved_relative
    if approved_path.is_file():
        approved = WholeReportSynthesis.model_validate_json(
            approved_path.read_text(encoding="utf-8")
        )
    else:
        sections = list(synopsis.body_sections)
        conclusion_indices = [index for index, item in enumerate(sections)
                              if item.kind == "TEXT" and re.search(
                                  r"结论|展望|conclusion|perspective|conclusion|perspective",
                                  item.heading, re.IGNORECASE)]
        if conclusion_indices:
            index = conclusion_indices[-1]
            conclusion = sections[index]
            reviews = []
            for record in _science_librarians(runner, stage="literature_v071_conclusion_review"):
                rid = record["representative_id"]
                review = _frozen_or_call(
                    runner,
                    Path("governance_private/literature_report/writing_v071/final")
                    / f"conclusion_review_{rid}.json",
                    LocalCheck, rid, "literature_v071_conclusion_review",
                    "只审查结尾是否忠实概括冻结模块、有无越过证据边界。"
                    "不要要求扩写新课题或重开模块；只返回 JSON。",
                    {"conclusion": conclusion.body_markdown,
                     "frozen_module_summaries": summaries},
                )
                reviews.append({"representative_id": rid, **review.model_dump(mode="json")})
            _freeze(runner, base / "v071_conclusion_review.json",
                    {"reviews": reviews, "single_round_only": True})
            objections = [item["problem"] for item in reviews
                          if item["status"] == "MATERIAL_PROBLEM"]
            if objections:
                revised = _frozen_or_call(
                    runner, base / "v071_conclusion_revision.json", ConclusionRevision,
                    "WRITER", "literature_v071_conclusion_revision",
                    "只局部修正结尾，以忠实概括冻结章节；不能增加新事实或新引文。"
                    "这是单轮科学审阅后的限域修订，不重开全文审阅。只返回 JSON。",
                    {"original_conclusion": conclusion.body_markdown,
                     "objections": objections, "frozen_module_summaries": summaries},
                )
                sections[index] = conclusion.model_copy(update={
                    "body_markdown": revised.body_markdown
                })
        approved = synopsis.model_copy(update={"body_sections": sections})
        _freeze(runner, approved_relative, approved)
    report = runner._assemble_report_markdown(
        outline, completed, approved_path, footnotes=[], freeze_supplemental_trace=True,
    )
    # The source modules and bibliography are frozen.  Only deterministic
    # publication assembly occurs here; Human re-review is a later, explicit
    # successor workflow and never overwrites this first deliverable.
    return runner._publish(report, completed, contested)
