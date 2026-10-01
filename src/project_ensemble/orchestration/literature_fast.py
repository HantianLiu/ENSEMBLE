"""A separate, resumable fast literature workflow without a Chair.

The frozen writing policy selects this runner; older literature meetings never
enter it. All model outputs, research requests and Human decisions have stable
paths so an interrupted run resumes rather than repeating completed work.
"""

from __future__ import annotations

import json
import hashlib
import copy
import re
import time
import threading
from datetime import datetime, timezone
from contextlib import nullcontext
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import deque
from itertools import count
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.errors import (
    ForcedModelReplacementRequested,
    ProviderContentRejectedError, ProviderError, RepresentativeUnavailableError, ResearchQualityControlError,
    ResearchRequestRejectedError, TransientProviderError, OpenAlexDailyQuotaExhausted,
)
from project_ensemble.orchestration.consultations import (
    HumanConsultationIssue, HumanConsultationResolution, HumanConsultationService,
)
from project_ensemble.orchestration.literature_report import (
    ClusteredResearchOutline, FrozenResearchOutline, OutlineModule,
)
from project_ensemble.orchestration.literature_report_execution import (
    LiteratureReportExecutionResult, LiteratureReportExecutionRunner,
    ReaderFacingLineRepair, WholeReportSynthesis,
    _effective_chapter_citation_catalog_path,
)
from project_ensemble.orchestration.literature_style import (
    is_identifier_only_rewrite, leaked_internal_identifiers,
)
from project_ensemble.orchestration.literature_writing_v071 import (
    GlossaryTerm, LiteratureWritingPaused, ModuleWritingOutline, ScienceChecklist, WriterChapter,
    _literature_step, _science_librarians,
    _freeze, _freeze_glossary, _frozen_or_call, _science_review_evidence,
    _writer_chapter, _writer_visible_payload,
)
from project_ensemble.runtime.progress import TaskProgressItem
from project_ensemble.runtime.draining_pool import DrainingThreadPoolExecutor
from project_ensemble.runtime.fast_scope_consultation import (
    read_scope_decisions, unique_scope_changes,
)
from project_ensemble.runtime.model_replacements import ModelReplacementService, current_runtime_for
from project_ensemble.runtime.research_fallbacks import ResearchFallbacks
from project_ensemble.research.models import ClaimSourceDomain, NormalizedClaim, ResearchRequest, ResearchStage
from project_ensemble.research.exploration import ResearchExplorationService
from project_ensemble.research.retrievers import (
    PolicyResearchRetriever, ResearchRetrievalResult, coerce_retrieval_result,
    openalex_unavailable_reason,
)

DEFAULT_FAST_SEARCH_REPAIR_DIRECTION = (
    "保留原科学问题、核验范围和四类证据方向；把每条检索式缩短为少量核心主题词或短语，"
    "删去多余修饰词和复杂嵌套布尔结构。默认不用 * 或 ? 通配符；优先用明确的 OR 词形变体。"
    "只有确有必要且 OpenAlex 支持时才使用通配符。不得改变命题或把检索方向合并。"
)


def _policy_retriever(retriever) -> PolicyResearchRetriever | None:
    if isinstance(retriever, PolicyResearchRetriever):
        return retriever
    for child in getattr(retriever, "retrievers", ()):
        if isinstance(child, PolicyResearchRetriever):
            return child
    return None


def _markdown_paragraph_spans(text: str) -> list[tuple[int, int, str]]:
    """Return 1-based-ready Markdown blocks separated by blank lines.

    Fenced code blocks are kept intact even when they contain empty lines. The
    returned offsets cover only the non-whitespace block content, so replacing
    one block leaves the original spacing between blocks untouched.
    """
    spans: list[tuple[int, int, str]] = []
    offset = 0
    block_start: int | None = None
    block_end: int | None = None
    fence_char: str | None = None
    fence_length = 0

    def finish_block() -> None:
        if block_start is not None and block_end is not None:
            spans.append((block_start, block_end, text[block_start:block_end]))

    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        fence = re.match(r"^\s*(`{3,}|~{3,})", content)
        if fence and fence_char is None:
            marker = fence.group(1)
            fence_char, fence_length = marker[0], len(marker)
        elif fence and fence_char is not None:
            marker = fence.group(1)
            if (marker[0] == fence_char and len(marker) >= fence_length
                    and not content[fence.end():].strip()):
                fence_char, fence_length = None, 0

        is_separator = not content.strip() and fence_char is None
        if is_separator:
            finish_block()
            block_start = block_end = None
        else:
            if block_start is None:
                block_start = offset
            block_end = offset + len(content)
        offset += len(line)

    finish_block()
    return spans


class FastTaskbook(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    hard_constraints: list[str] = Field(default_factory=list, max_length=20)
    preferences: list[str] = Field(default_factory=list, max_length=20)
    open_questions: list[str] = Field(default_factory=list, max_length=20)
    provisional_assumptions: list[str] = Field(default_factory=list, max_length=20)
    working_definitions: list[str] = Field(default_factory=list, max_length=30)
    global_evidence_requirements: list[str] = Field(min_length=1, max_length=24)
    completion_standard: str = Field(min_length=1, max_length=1800)
    outline: ClusteredResearchOutline


class FastPlanningTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["ASK", "SEARCH", "PROPOSE"]
    question: str | None = Field(default=None, max_length=1000)
    search_queries: list[str] = Field(default_factory=list, max_length=2)
    source_domain: ClaimSourceDomain = ClaimSourceDomain.GENERAL
    taskbook: FastTaskbook | None = None

    @model_validator(mode="after")
    def one_action(self) -> "FastPlanningTurn":
        if self.action == "ASK" and (not self.question or self.taskbook is not None or self.search_queries):
            raise ValueError("ASK requires one question and no taskbook")
        if self.action == "SEARCH" and (not self.question or self.taskbook is not None
                                         or not self.search_queries
                                         or any(not query.strip() for query in self.search_queries)):
            raise ValueError("SEARCH requires one question and one or two search queries")
        if self.action == "PROPOSE" and (self.taskbook is None or self.question is not None
                                          or self.search_queries):
            raise ValueError("PROPOSE requires a taskbook and no question")
        return self


class FastBreadthSearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=1000)
    search_queries: list[str] = Field(default_factory=list, max_length=8)
    source_domain: ClaimSourceDomain = ClaimSourceDomain.GENERAL

    @model_validator(mode="after")
    def distinct_queries(self) -> "FastBreadthSearchPlan":
        if any(not query.strip() for query in self.search_queries):
            raise ValueError("breadth-search queries must be nonempty")
        if len(set(query.casefold() for query in self.search_queries)) != len(self.search_queries):
            raise ValueError("breadth-search queries must be distinct")
        return self


class FastSplitModuleProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=160)
    central_problem: str = Field(min_length=1, max_length=700)
    included_scope: list[str] = Field(default_factory=list, max_length=8)
    excluded_scope: list[str] = Field(default_factory=list, max_length=8)
    evidence_needs: list[str] = Field(min_length=1, max_length=8)
    dependencies: list[str] = Field(default_factory=list, max_length=8)


class FastSplitProposal(BaseModel):
    """One independent, nonbinding module-split proposal."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    core_problem: str = Field(min_length=1, max_length=1200)
    hard_constraints_seen: list[str] = Field(default_factory=list, max_length=20)
    proposed_modules: list[FastSplitModuleProposal] = Field(min_length=1, max_length=12)
    cross_module_synthesis: str = Field(min_length=1, max_length=1000)
    unresolved_scope_questions: list[str] = Field(default_factory=list, max_length=10)
    planning_caveat: str = Field(min_length=1, max_length=1000)


class FastModulePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    module_id: str
    core_problem: str = Field(min_length=1, max_length=1000)
    boundary: str = Field(min_length=1, max_length=1000)
    approved_scope_changes: list[str] = Field(default_factory=list, max_length=9)
    subquestions_and_priorities: list[str] = Field(min_length=1, max_length=16)
    mandatory_evidence: list[str] = Field(min_length=1, max_length=16)
    optional_evidence: list[str] = Field(default_factory=list, max_length=16)
    preferred_primary_sources: list[str] = Field(default_factory=list, max_length=12)
    search_terms_and_synonyms: list[str] = Field(default_factory=list, max_length=24)
    definition_or_method_differences: list[str] = Field(default_factory=list, max_length=12)
    verification_and_conflict_handling: str = Field(min_length=1, max_length=1600)
    cross_module_interfaces: list[str] = Field(default_factory=list, max_length=12)
    completion_standard: str = Field(min_length=1, max_length=1200)
    report_back_scope_questions: list[str] = Field(default_factory=list, max_length=8)
    proposed_scope_change: str | None = Field(default=None, max_length=1200)
    writing_outline: ModuleWritingOutline


class FastQueryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # True means this is the last batch, not that the current batch is empty.
    finished: bool = False
    claims: list[str] = Field(default_factory=list, max_length=6)
    glossary_claims: list[str] = Field(default_factory=list, max_length=6)
    reason_to_continue_or_stop: str = Field(min_length=1, max_length=900)

    @model_validator(mode="after")
    def valid_batch(self) -> "FastQueryBatch":
        combined = [*self.claims, *self.glossary_claims]
        if len(combined) > 6 or len(set(combined)) != len(combined):
            raise ValueError("module and glossary claims must be distinct, at most six in total")
        if not self.finished and not combined:
            raise ValueError("unfinished batches need at least one claim")
        return self


class FastResearchDeskFailure(Exception):
    """One question failed; other durable answers in its batch remain valid."""

    def __init__(self, module_id: str, round_number: int, index: int, cause: Exception):
        self.module_id = module_id
        self.round_number = round_number
        self.index = index
        self.request_id = f"FAST-{module_id}-{round_number}-{index:02d}"
        self.cause = cause
        super().__init__(
            f"{module_id} round {round_number} question {index}: "
            f"{type(cause).__name__}: {cause}"
        )


class FastResearchDeskRetry(Exception):
    """Human chose to retry missing questions after current calls finish."""


class FastResearchDeskPause(Exception):
    """Human chose to pause after other independent calls finish."""


class FastWholeModuleRewriteRequested(Exception):
    """Human requested a full module rewrite after local patching failed."""


class FastResolutionVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    resolved: bool
    remaining_material_problems: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def explanation_matches(self) -> "FastResolutionVote":
        if self.resolved and self.remaining_material_problems:
            raise ValueError("resolved vote cannot list remaining problems")
        if not self.resolved and not self.remaining_material_problems:
            raise ValueError("unresolved vote needs a concrete remaining problem")
        return self


class FastEvidenceAppealVote(FastResolutionVote):
    resolved_objection_explanations: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def explains_correction(self) -> "FastEvidenceAppealVote":
        if self.resolved and not self.resolved_objection_explanations:
            raise ValueError("a resolved evidence appeal must explain why the earlier objections fail")
        return self


class FastLocalScienceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    old_text: str | None = Field(default=None, min_length=1)
    new_text: str = Field(min_length=1)
    objection_numbers: list[int] = Field(min_length=1, max_length=8)
    paragraph_number: int | None = Field(default=None, ge=1)
    target_field: Literal["body_markdown", "short_summary"] | None = None
    replace_entire_field: bool = False

    @model_validator(mode="after")
    def valid_objection_numbers(self) -> "FastLocalScienceEdit":
        if self.paragraph_number is not None:
            if self.target_field == "short_summary" or self.replace_entire_field:
                raise ValueError("paragraph edits target body_markdown only")
        elif self.replace_entire_field:
            if self.target_field != "short_summary" or self.old_text is not None:
                raise ValueError("whole-field replacement is available for short_summary only")
        elif self.old_text is None:
            raise ValueError("a text edit needs either a paragraph number or exact old text")
        if any(number < 1 for number in self.objection_numbers):
            raise ValueError("objection numbers must be positive")
        if len(set(self.objection_numbers)) != len(self.objection_numbers):
            raise ValueError("objection numbers must be unique within an edit")
        return self


class FastLocalScienceGlossaryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    term: str = Field(min_length=1, max_length=120)
    field: Literal["explanation", "formula"]
    expected_text: str | None = Field(default=None, min_length=1)
    new_text: str = Field(min_length=1)
    objection_numbers: list[int] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def valid_objection_numbers(self) -> "FastLocalScienceGlossaryEdit":
        if any(number < 1 for number in self.objection_numbers):
            raise ValueError("objection numbers must be positive")
        if len(set(self.objection_numbers)) != len(self.objection_numbers):
            raise ValueError("objection numbers must be unique within a glossary edit")
        return self


class FastLocalScienceGlossaryAddition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    entry: GlossaryTerm
    objection_numbers: list[int] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def valid_objection_numbers(self) -> "FastLocalScienceGlossaryAddition":
        if any(number < 1 for number in self.objection_numbers):
            raise ValueError("objection numbers must be positive")
        if len(set(self.objection_numbers)) != len(self.objection_numbers):
            raise ValueError("objection numbers must be unique within a glossary addition")
        return self


class FastLocalScienceRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    edits: list[FastLocalScienceEdit] = Field(default_factory=list, max_length=12)
    glossary_edits: list[FastLocalScienceGlossaryEdit] = Field(default_factory=list, max_length=12)
    glossary_additions: list[FastLocalScienceGlossaryAddition] = Field(
        default_factory=list, max_length=8,
    )

    @model_validator(mode="after")
    def has_local_change(self) -> "FastLocalScienceRepair":
        if not self.edits and not self.glossary_edits and not self.glossary_additions:
            raise ValueError("a local science repair must contain a text or glossary change")
        return self


class FastSearchQueries(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    supporting_query: str = Field(min_length=1)
    contradictory_query: str = Field(min_length=1)
    limitations_query: str = Field(min_length=1)
    alternatives_query: str = Field(min_length=1)


def request_fast_research_rollback(repo, request_id: str, direction: str) -> Path:
    """Append a Human request to redo one unfinished claim's search planning."""
    if not re.fullmatch(r"FAST-RM-[0-9]+-[1-3]-[0-9]{2}", request_id):
        raise ValueError("manual research rollback requires a fast-workflow claim ID")
    direction = direction.strip()
    if not direction:
        raise ValueError("manual research rollback needs a troubleshooting direction")
    parts = request_id.split("-")
    outcome = (repo.root / "public/literature_report/fast" / f"{parts[1]}-{parts[2]}"
               / f"research_round_{parts[3]}_{parts[4]}.json")
    if outcome.is_file():
        raise ValueError("completed research cannot be rolled back from the failure menu")
    stage_root = Path("audit_private/research/fast_stages")
    existing = sorted((repo.root / stage_root).glob(f"{request_id}-rollback-[0-9][0-9][0-9][0-9].json"))
    version = len(existing) + 1
    relative = stage_root / f"{request_id}-rollback-{version:04d}.json"
    record = {
        "request_id": request_id, "version": version, "direction": direction,
        "authorized_by": "HUMAN", "created_at": datetime.now(timezone.utc).isoformat(),
        "policy": "APPEND_ONLY_REPLAN_UNFINISHED_CLAIM; PRIOR_ARTIFACTS_PRESERVED",
    }
    repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
    repo.events.append("FAST_RESEARCH_MANUAL_ROLLBACK_REQUESTED", {
        "meeting_id": repo.meeting_id, "request_id": request_id,
        "version": version, "record_path": str(relative),
    }, actor="HUMAN")
    return repo.root / relative


class FastWholeSynthesis(WholeReportSynthesis):
    @model_validator(mode="before")
    @classmethod
    def exclude_module_ids_from_citation_metadata(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        identifiers = value.get("cited_packet_ids")
        if not isinstance(identifiers, list):
            return value
        return {
            **value,
            "cited_packet_ids": [
                identifier for identifier in identifiers
                if not (isinstance(identifier, str)
                        and re.fullmatch(r"RM-[0-9]+", identifier))
            ],
        }

    @model_validator(mode="after")
    def use_frozen_module_insertion(self) -> "FastWholeSynthesis":
        if self.body_sections:
            raise ValueError("fast synthesis must use fixed module insertion, not authored body_sections")
        if self.model_prior_claims:
            raise ValueError("fast synthesis cannot publish new MODEL_PRIOR claims")
        return self


class FastLiteratureRunner(LiteratureReportExecutionRunner):
    """Writer-led workflow. A Chair is never configured or called."""

    def _fast_root(self) -> Path:
        return Path("public/literature_report/fast")

    def _read_json(self, relative: Path) -> dict:
        return json.loads((self.repo.root / relative).read_text(encoding="utf-8"))

    def _planning_search(self, *, key: str, question: str, queries: list[str],
                         cycle: int, turn: int, requester_id: str = "FAST_WRITER",
                         source_domain: ClaimSourceDomain = ClaimSourceDomain.GENERAL) -> dict:
        """One replayable discovery-only search; never a formal evidence verdict."""
        relative = self._fast_root() / "planning_search" / f"{key}.json"
        if (self.repo.root / relative).is_file():
            return self._read_json(relative)
        if not queries or self.research_desk is None:
            result = {"status": "NOT_RUN", "answer_text": "未执行探索性检索。",
                      "searches_used": 0, "source_catalog": []}
        else:
            service = ResearchExplorationService(
                self.research_desk, max_output_tokens=self.max_output_tokens,
            )
            record = service.answer(
                requester_id=requester_id, cycle=cycle, turn=turn, question_number=1,
                question=question, search_queries=queries,
                source_domain=source_domain,
            )
            result = service.publish(
                [record], public_prefix=self._fast_root() / "planning_search/sources",
            )[0]
        summary = {
            "status": result["status"], "question": question,
            "search_queries": queries, "searches_used": result["searches_used"],
            "source_domain": source_domain.value,
            "answer_text": result["answer_text"][:12000],
            "sources": [
                {"source_id": item.get("source_id"), "title": item.get("title"),
                 "url": item.get("url")}
                for item in result.get("source_catalog", [])[:16]
            ],
            "evidence_status": "DISCOVERY_ONLY; 第 3 步须重新核查关键论断",
        }
        _freeze(self, relative, summary)
        return summary

    def _parallel_split_proposals(self, task: dict) -> list[dict]:
        """Independently search and propose; expose only anonymous content to Writer."""
        configured = self.manifest.get("fast_planner_models") or []
        if not configured:
            return []  # Compatibility for meetings initialized before this policy.
        if not 2 <= len(configured) <= 3:
            raise ValueError("fast planning requires two or three configured proposers")
        root = self._fast_root() / "split_proposals"
        task_ids = [f"fast-split-{index}" for index in range(1, len(configured) + 1)]
        self.engine.progress.status(
            MeetingPhase.RESEARCH_PLANNING,
            f"快速文献调研 · 步骤 1/5：{len(configured)} 名提议者并行检索并拆分问题",
        )
        self.engine.progress.task_batch_started(
            [TaskProgressItem(
                task_id=task_ids[index - 1], participant_id=f"FAST_PLANNER_{index}",
                initial_state="completed" if (self.repo.root / root / f"proposal_{index}.json").is_file()
                else "pending", completion_requires_commit=True,
            ) for index in range(1, len(configured) + 1)],
            title="快速文献调研 · 匿名模块拆分提议",
        )

        def one(index: int) -> dict:
            participant = f"FAST_PLANNER_{index}"
            task_id = task_ids[index - 1]
            proposal_path = root / f"proposal_{index}.json"
            if (self.repo.root / proposal_path).is_file():
                proposal = FastSplitProposal.model_validate(self._read_json(proposal_path))
                self.engine.progress.task_finished(task_id, detail="提议已恢复落盘")
                return proposal.model_dump(mode="json")
            self.engine.progress.task_started(task_id, "拟定一次广度检索")
            plan = _frozen_or_call(
                self, root / f"search_plan_{index}.json", FastBreadthSearchPlan,
                participant, f"fast_split_search_plan_{index}",
                "你只负责为一份研究任务提出独立的模块拆分方案，不参与之后的主笔、审阅或表决。"
                "先规划一次广度优先探索，最多八条互不重复的检索式，兼顾不同定义、反例与范围。"
                "检索只是帮助确定问题结构，不是正式核查；不得预设科学结论。只返回 JSON。"
                "学术论文、科学理论或研究方法的问题将 source_domain 设为 ACADEMIC，优先 OpenAlex；"
                "法规、政策、新闻或一般网页问题选其他适当类别，不为增加候选而重复调用付费网页搜索。",
                {"original_task": task, "search_limit": 8},
            )
            self.engine.progress.task_started(task_id, "探索性检索中；尚非正式证据核查")
            discovery = self._planning_search(
                key=f"split_proposer_{index}", question=plan.question,
                queries=plan.search_queries, cycle=100 + index, turn=0,
                requester_id=participant, source_domain=plan.source_domain,
            )
            self.engine.progress.task_started(task_id, "依据检索线索提交独立拆分方案")
            proposal = _frozen_or_call(
                self, proposal_path, FastSplitProposal, participant,
                f"fast_split_proposal_{index}",
                "只提出任务拆分方案，不起草报告或正式证据判断。按要解决的问题划分模块，"
                "说明每个模块存在的理由、纳入和排除范围、所需证据及模块依赖；"
                "给跨模块综合留位置，但不能把关键问题推给它。"
                "用户硬约束不可改变；探索性结果只作为规划线索。"
                "不写出你的模型、供应商、角色或身份，也不猜测其他提议者。只返回 JSON。",
                {"original_task": task, "exploratory_search": discovery},
            )
            self.engine.progress.task_finished(task_id, detail="独立拆分方案已落盘")
            return proposal.model_dump(mode="json")

        results: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=len(configured)) as pool:
            futures = {pool.submit(one, index): index for index in range(1, len(configured) + 1)}
            for future in futures:
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception:
                    self.engine.progress.task_finished(
                        task_ids[index - 1], failed=True,
                        detail="拆分提议尚未落盘；已完成的其他提议可恢复复用",
                    )
                    raise
        # No participant IDs, model names, search-result file paths or identities.
        anonymous = []
        for index in sorted(results):
            search_path = self._fast_root() / "planning_search" / f"split_proposer_{index}.json"
            discovery = self._read_json(search_path) if (self.repo.root / search_path).is_file() else {}
            anonymous.append({
                "anonymous_proposal": index,
                "content": results[index],
                "discovery_only": {
                    key: discovery[key] for key in ("question", "answer_text", "sources", "evidence_status")
                    if key in discovery
                },
            })
        return anonymous

    def _breadth_search(self, stage: int, context: dict) -> dict:
        key = f"step_{stage}_breadth"
        relative = self._fast_root() / "planning_search" / f"{key}.json"
        if (self.repo.root / relative).is_file():
            return self._read_json(relative)
        plan = _frozen_or_call(
            self, self._fast_root() / "planning_search" / f"{key}_plan.json",
            FastBreadthSearchPlan, "WRITER", f"fast_planning_breadth_{stage}",
            "为快速文献调研规划阶段做一次广度优先探索。最多提交 8 条互不重复的检索式，"
            "覆盖问题的主要路径、不同定义及可能反例；如确无必要可提交空列表。"
            "这是制定任务书/执行单的背景线索，不是正式证据核查，不能据此下确定性结论。"
            "学术论文、科学理论或研究方法选 source_domain=ACADEMIC，优先 OpenAlex；"
            "法规、政策、新闻或一般网页问题选其他适当类别。"
            "只返回 JSON。",
            context,
        )
        return self._planning_search(
            key=key, question=plan.question, queries=plan.search_queries,
            cycle=stage, turn=0, source_domain=plan.source_domain,
        )

    def _effective_scope_view(self, module_id: str) -> dict:
        """Present a deduplicated execution view without rewriting frozen votes."""
        source = self._fast_root() / module_id / "scope_decision.json"
        frozen = self._read_json(source)
        changes = unique_scope_changes(frozen.get("item_decisions", []))
        if not changes:
            return frozen
        return {
            "decision": frozen["decision"],
            "proposed_scope_change": "\n".join(changes),
            "effective_scope_changes": changes,
            "frozen_audit_source_path": str(source),
        }

    def _effective_fast_outline(self, module_id: str) -> Path:
        """Give a not-yet-drafted module one copy of each approved scope change."""
        source = (Path("public/literature_report/modules") / module_id
                  / "writing_v071/approved_outline.json")
        source_path = self.repo.root / source
        raw = self._read_json(source)
        scope = raw.get("decision", {}).get("scope_change", {})
        changes = unique_scope_changes(scope.get("item_decisions", []))
        if len(changes) == sum(item.get("decision") == "CHANGE"
                               for item in scope.get("item_decisions", [])):
            return source_path
        writing_base = source_path.parent
        if list(writing_base.glob("writer_v*_validated.json")):
            # Do not change the input of an already-frozen writer draft.
            return source_path
        effective = json.loads(json.dumps(raw, ensure_ascii=False))
        effective_change = "\n".join(changes)
        effective["decision"]["scope_change"]["proposed_scope_change"] = effective_change
        effective["decision"]["scope_change"]["effective_scope_changes"] = changes
        effective["decision"]["scope_change"]["frozen_audit_source_path"] = str(source)
        old_change = scope.get("proposed_scope_change")
        if isinstance(old_change, str):
            effective["outline"]["scope_notes"] = [
                note.replace(old_change, effective_change) if old_change in note else note
                for note in effective["outline"].get("scope_notes", [])
            ]
        destination = self._fast_root() / module_id / "approved_outline_effective.json"
        return _freeze(self, destination, effective)

    def _consult(self, issue_id: str, stage: str, question: str,
                 options: list[str], context: dict) -> HumanConsultationResolution:
        service = HumanConsultationService(self.repo)
        # A Human may already have resolved this stable consultation ID in a
        # previous process.  Return that immutable ruling before reconstructing
        # the issue: retry/resume can carry newer evidence paths in ``context``
        # even though the question and decision are unchanged.
        resolution = service.resolution(issue_id)
        if resolution is not None:
            return resolution
        service.open_issue(HumanConsultationIssue(
            issue_id=issue_id, meeting_id=self.repo.meeting_id,
            reason_code=stage + "_HUMAN_REQUIRED", stage=stage,
            question=question, options=options, context=context,
        ))
        resolution = service.resolution(issue_id)
        if resolution is None:
            raise LiteratureWritingPaused(stage + "_HUMAN_REQUIRED")
        return resolution

    def _taskbook(self) -> FastTaskbook:
        frozen = self._fast_root() / "approved_taskbook.json"
        if (self.repo.root / frozen).exists():
            approved = FastTaskbook.model_validate(self._read_json(frozen))
            _freeze(self, Path("public/literature_report/frozen_research_outline.json"),
                    FrozenResearchOutline.model_validate({
                        **approved.outline.model_dump(mode="json"), "review_dispositions": [],
                    }))
            return approved
        task = self._read_json(Path("public/task.json"))
        anonymous_split_proposals = self._parallel_split_proposals(task)
        dialogue: list[dict] = []
        last_candidate: dict | None = None
        lookup_budget = 4
        for cycle in count(1):
            relative = self._fast_root() / f"planning_turn_{cycle:02d}.json"
            self.engine.progress.status(
                MeetingPhase.RESEARCH_PLANNING,
                f"快速文献调研 · 步骤 1/5：学术主笔参考匿名方案制定任务书 · 第 {cycle} 轮",
            )
            task_id = f"fast-taskbook-writer-{cycle}"
            self.engine.progress.task_batch_started(
                [TaskProgressItem(task_id=task_id, participant_id="WRITER",
                                  initial_state="completed" if (self.repo.root / relative).is_file()
                                  else "pending", completion_requires_commit=True)],
                title="快速文献调研 · 学术主笔独立拟定任务书",
            )
            if not (self.repo.root / relative).is_file():
                self.engine.progress.task_started(task_id, "主笔正在处理匿名拆分方案")
            turn = _frozen_or_call(
                self, relative, FastPlanningTurn, "WRITER",
                f"fast_taskbook_turn_{cycle:02d}",
                "以学术研究合作者方式逐步澄清任务，不扮演主席。每次只问一个会改变任务书的关键问题。"
                "已知事项不重复询问；若重要边界已经明确，直接提交完整任务书。"
                "区分硬约束、偏好、开放问题与暂作假设。模块按待解决的问题划分，"
                "写明各自边界与依赖，不按资料、国家或年代机械切分，不预设研究结论。"
                "模块数可为 1–12；不要习惯性压成六个。如果不同关键问题需要独立证据、"
                "方法或结论边界，就分别设模块；也不要为了凑数量拆散同一问题。"
                "若需澄清陌生主题，可选择 SEARCH 做少量轻量查证；这只是寻找线索，不要求"
                "在此阶段完成正式证据证明。题目对话累计最多 4 条检索式。"
                "SEARCH 时学术论文、科学理论或研究方法选 source_domain=ACADEMIC；"
                "法规、政策、新闻或一般网页问题选其他适当类别。"
                "在 outline.proposed_disciplines 中提出本题涉及的学科门类供人类确认；"
                "不得自行推断读者在各学科的专业度。"
                "outline.modules[*].cross_module_links 只能填写本次提纲中实际存在的 RM-xx 模块编号；"
                "不能用‘命题 2 的主责模块’等文字代替编号。无法确定对应模块时留空，"
                "并在文字说明中标出这项未定依赖，不得猜测编号。"
                "每个模块的 source_submission_refs 填 [\"WRITER\"]，这是记录来源而不是文献引注。"
                "若提供匿名拆分提议，它们只是独立的非约束性参考；不得推断或透露提议者身份，"
                "不得机械投票、取平均或拼接。你必须独立形成自己的任务书，用户硬约束绝不可动。"
                "可采纳、合并或拒绝提议，但不必逐条交代；仍可向人类澄清关键范围。"
                "请保留用户纠正前提的空间，只输出 JSON。",
                {"original_task": task, "dialogue": dialogue[-12:],
                 "previous_candidate": last_candidate,
                 "anonymous_split_proposals": anonymous_split_proposals,
                 "remaining_lightweight_searches": lookup_budget,
                 "exploratory_search_rule": "需要外部背景才能准确提问时，可用 SEARCH 提交一个问题及最多两条检索式；"
                 "整个题目对话最多四条检索。搜索只供任务设计，不代表严格证明。"},
            )
            if turn.action == "SEARCH":
                self.engine.progress.task_finished(task_id, detail="探索性检索问题已落盘")
                assert turn.question is not None
                queries = turn.search_queries[:lookup_budget]
                if queries:
                    found = self._planning_search(
                        key=f"dialogue_{cycle:02d}", question=turn.question,
                        queries=queries, cycle=0, turn=cycle,
                        source_domain=turn.source_domain,
                    )
                    lookup_budget -= len(queries)
                    dialogue.append({"writer_lookup": turn.question,
                                     "exploratory_result": found})
                else:
                    dialogue.append({"writer_lookup": turn.question,
                                     "exploratory_result": "轻量查证额度已用完；请继续确定任务书。"})
                continue
            if turn.action == "ASK":
                self.engine.progress.task_finished(task_id, detail="澄清问题已落盘；等待人类回答")
                assert turn.question is not None
                issue_id = f"HC-FAST-PLAN-{cycle:02d}"
                resolution = self._consult(
                    issue_id, "FAST_TASKBOOK_QUESTION", turn.question,
                    ["USE_HUMAN_WORDING", "USE_WRITER_DEFAULT"], {"cycle": cycle},
                )
                dialogue.append({"writer_question": turn.question,
                                 "human_answer": (resolution.human_wording
                                                  if resolution.decision == "USE_HUMAN_WORDING"
                                                  else "此项由主笔采用合理默认值，并在暂作假设中明确说明")})
                continue
            assert turn.taskbook is not None
            candidate = self._fast_root() / f"candidate_{cycle:02d}.json"
            if (self.repo.root / candidate).is_file():
                proposed = FastTaskbook.model_validate(self._read_json(candidate))
            elif self.research_desk is not None:
                breadth = self._breadth_search(1, {
                    "original_task": task,
                    "dialogue": dialogue[-12:],
                    "provisional_taskbook": turn.taskbook.model_dump(mode="json"),
                })
                proposed = _frozen_or_call(
                    self, self._fast_root() / f"candidate_{cycle:02d}_after_search.json",
                    FastTaskbook, "WRITER", f"fast_taskbook_after_search_{cycle:02d}",
                    "依据初步任务书和探索性检索地图完善任务书。搜索结果仅是规划线索，"
                    "不得把未正式核查的内容写成确定结论；保留用户已经明确的范围与约束。"
                    "模块数可为 1–12；如果人类要求增加模块数量，应调整问题边界并满足目标数量，"
                    "不要只在原模块下增加小节，也不要拆出空洞模块。"
                    "如搜索没有提供有用材料，保持原任务书。只返回 JSON。",
                    {"original_task": task,
                     "provisional_taskbook": turn.taskbook.model_dump(mode="json"),
                     "anonymous_split_proposals": anonymous_split_proposals,
                     "exploratory_breadth_search": breadth},
                )
            else:
                proposed = turn.taskbook
            _freeze(self, candidate, proposed)
            self.engine.progress.task_finished(task_id, detail="候选任务书已落盘；等待人类确认")
            last_candidate = proposed.model_dump(mode="json")
            resolution = self._consult(
                f"HC-FAST-APPROVE-{cycle:02d}", "FAST_TASKBOOK_APPROVAL",
                "请检查任务书与模块划分；批准后才开始正式检索。若不批准，说明需要如何修改。",
                ["APPROVE_TASKBOOK", "REVISE_TASKBOOK"],
                {"cycle": cycle, "taskbook_path": str(candidate),
                 "module_titles": [m.title for m in proposed.outline.modules],
                 "proposed_disciplines": proposed.outline.proposed_disciplines,
                 "article_skeleton": proposed.outline.article_skeleton},
            )
            if resolution.decision == "APPROVE_TASKBOOK":
                _freeze(self, Path("public/literature_report/frozen_research_outline.json"),
                        FrozenResearchOutline.model_validate({
                            **proposed.outline.model_dump(mode="json"),
                            "review_dispositions": [],
                        }))
                _freeze(self, frozen, proposed)
                return proposed
            dialogue.append({"candidate_path": str(candidate),
                             "human_revision": resolution.rationale})

    def _module_plan(self, taskbook: FastTaskbook, module: OutlineModule) -> FastModulePlan:
        base = self._fast_root() / module.module_id
        plan = _frozen_or_call(
            self, base / "execution_plan.json", FastModulePlan, "WRITER",
            f"fast_module_plan_{module.module_id}",
            "依据已批准任务书，为指定模块制定可执行研究计划与简洁写作提纲。"
            "不得改变全局目标、扩大或缩小模块范围，不得把偶然搜到的文献反向变成研究问题。"
            "若确有必要更改局部范围，只在 proposed_scope_change 单独说明具体改变，"
            "未经人类批准不得将该改变写入普通子问题、检索路线或写作提纲。"
            "report_back_scope_questions 只列真正需要人类裁定的独立范围边界；"
            "同一问题的具体情形应合并成一条，普通研究子问题不得伪装成范围变更。"
            "区分必须与可选证据；列出一手来源类型、同义检索词、定义/方法差异、"
            "负面结果和证据缺口的处理方式。完成标准不是固定文献篇数。"
            "提纲只规定行文步骤与需要覆盖的内容，不抢先写正文或预设结论。只返回 JSON。",
            {"approved_taskbook": taskbook.model_dump(mode="json"),
             "assigned_module": module.model_dump(mode="json"),
             "planning_search": self._read_json(
                 self._fast_root() / "planning_search/step_2_breadth.json"
             ) if (self.repo.root / self._fast_root()
                   / "planning_search/step_2_breadth.json").is_file() else None},
        )
        if plan.module_id != module.module_id:
            raise ValueError("fast module plan changed its assigned module ID")
        if plan.approved_scope_changes:
            raise ValueError("writer cannot approve scope changes in its own module plan")
        scope_decision = {"decision": "NO_CHANGE", "proposed_scope_change": None}
        if plan.report_back_scope_questions or plan.proposed_scope_change:
            issue_id = f"HC-FAST-SCOPE-{module.module_id}"
            question = ("模块计划发现可能影响批准范围的问题；请逐条决定修改、不修改，"
                        "或授权学术主笔代裁。所有条目处理完毕后才继续。")
            existing_issue = (self.repo.root / "human_private/consultations"
                              / f"{issue_id}.issue.json")
            if existing_issue.is_file():
                # A live meeting may already have frozen the older wording.
                # Keep that issue immutable while presenting the new itemized UI.
                question = HumanConsultationIssue.model_validate_json(
                    existing_issue.read_text(encoding="utf-8")
                ).question
            resolution = self._consult(
                issue_id, "FAST_SCOPE_QUESTION", question,
                (["KEEP_APPROVED_SCOPE", "APPROVE_SCOPE_CHANGE"]
                 if plan.proposed_scope_change else ["KEEP_APPROVED_SCOPE", "KEEP_PAUSED"]),
                {"module_id": module.module_id,
                 "questions": plan.report_back_scope_questions,
                 "proposed_scope_change": plan.proposed_scope_change},
            )
            if resolution.decision == "KEEP_PAUSED":
                raise LiteratureWritingPaused("FAST_SCOPE_QUESTION_HUMAN_REQUIRED")
            item_count = len(plan.report_back_scope_questions) + bool(plan.proposed_scope_change)
            item_decisions = read_scope_decisions(self.repo, issue_id, item_count)
            if item_decisions is not None:
                changes = unique_scope_changes(item_decisions)
                scope_decision = {
                    "decision": "ITEMIZED_CHANGE" if changes else "ITEMIZED_KEEP",
                    "proposed_scope_change": "\n".join(changes) if changes else None,
                    "item_decisions": item_decisions,
                    "human_reason": resolution.rationale,
                }
            else:
                # Older non-interactive resolutions remain valid and resumable.
                scope_decision = {"decision": resolution.decision,
                                  "proposed_scope_change": (plan.proposed_scope_change
                                                            if resolution.decision == "APPROVE_SCOPE_CHANGE"
                                                            else None),
                                  "human_reason": resolution.rationale}
        frozen_scope_path = self.repo.root / base / "scope_decision.json"
        if frozen_scope_path.is_file():
            # Old, already-frozen modules retain their exact prior interpretation.
            scope_decision = self._read_json(base / "scope_decision.json")
        _freeze(self, base / "scope_decision.json", scope_decision)
        writing_outline = plan.writing_outline
        if scope_decision["proposed_scope_change"]:
            change_note = "人类批准的局部范围变更：" + scope_decision["proposed_scope_change"]
            notes = [*writing_outline.scope_notes, change_note]
            # Preserve the approved change even when the model filled every
            # optional note slot in its original (still immutable) plan.
            writing_outline = writing_outline.model_copy(update={
                "scope_notes": notes[-16:],
            })
            plan = plan.model_copy(update={
                "approved_scope_changes": (
                    unique_scope_changes(scope_decision.get("item_decisions", []))
                    or [scope_decision["proposed_scope_change"]]
                ),
                "writing_outline": writing_outline,
            })
        _freeze(self, Path("public/literature_report/modules") / module.module_id
                / "writing_v071/approved_outline.json",
                {"outline": writing_outline.model_dump(mode="json"),
                 "decision": {"policy": "FAST_WRITER_PLAN", "human_approved_taskbook": True,
                              "scope_change": scope_decision}})
        return plan

    def _research_round(self, taskbook: FastTaskbook, module: OutlineModule,
                        plan: FastModulePlan, round_number: int,
                        previous: list[dict]) -> FastQueryBatch:
        base = self._fast_root() / module.module_id
        compact = []
        for outcome in previous:
            packet = self._load_packet(outcome["packet_id"]) if outcome.get("packet_id") else None
            compact.append({"claim": outcome["claim"], "status": outcome["status"],
                            "knowledge_status": packet.knowledge_status.value if packet else None,
                            "consensus": packet.consensus.value if packet else None,
                            "supporting_findings": [item.evidence_summary[:350]
                                                    for item in packet.supporting_evidence[:2]] if packet else [],
                            "contradictory_findings": [item.evidence_summary[:350]
                                                       for item in packet.contradictory_evidence[:2]] if packet else [],
                            "scope_limitations": [item.evidence_summary[:350]
                                                  for item in packet.scope_limitations[:2]] if packet else [],
                            "unresolved_questions": list(packet.unresolved_questions)[:3] if packet else []})
        batch = _frozen_or_call(
            self, base / f"query_round_{round_number}.json", FastQueryBatch, "WRITER",
            f"fast_research_queries_{module.module_id}_{round_number}",
            "仅为当前模块提交可由外部资料核验的具体事实主张，不要求 Research Desk 裁决内部流程。"
            "第一次提出基础查询；后两轮仅针对已有结果的关键缺口、反证或方法差异。"
            "finished=true 表示本批核查完后不再开启下一轮；仍可在本批提交最多六项新问题，"
            "这些问题会先交 Research Desk 核查。若无需再查，可设 finished=true 且留空问题列表。"
            "无法回答时明确记为 UNRESOLVED。"
            "不得重复已回答的同一命题；仅执行人类明确批准的范围变更。"
            "若 approved_scope_changes 与旧 boundary 相冲突，以前者为准；不可默默沿用被修改的旧边界。"
            "若本章必需的术语、物理量或方法缺少可靠定义，可在 glossary_claims 单独提交"
            "可由外部资料核查的具体定义命题，Research Desk 会像普通问题一样查证并纳入证据包。"
            "命题须指出适用对象与条件，不提交‘某术语是什么’之类无边界的问题。"
            "这类查询与普通 claims 合计每轮最多六项，不为填满额度而重复提问。"
            "最多三轮查询，允许提前结束。只返回 JSON。",
            {"round": round_number, "max_rounds": 3,
             "module": module.model_dump(mode="json"),
             "plan": plan.model_dump(mode="json"),
             "scope_decision": self._effective_scope_view(module.module_id),
             "prior_outcomes": compact[-18:],
             "global_evidence_requirements": taskbook.global_evidence_requirements},
        )
        return batch

    def _research_claim(self, module: OutlineModule, round_number: int,
                        index: int, claim: str, *,
                        normalized_claim: NormalizedClaim | None = None,
                        retrieval_result: ResearchRetrievalResult | None = None,
                        prepared_sources: tuple[list[dict], list[dict]] | None = None) -> dict:
        relative = self._fast_root() / module.module_id / f"research_round_{round_number}_{index:02d}.json"
        if (self.repo.root / relative).is_file():
            saved = self._read_json(relative)
            if saved["claim"] != claim:
                raise ValueError("frozen fast research outcome conflicts with query")
            return saved
        request_id = f"FAST-{module.module_id}-{round_number}-{index:02d}"
        rollback = self._latest_fast_research_rollback(request_id)
        research_request_id = (
            f"{request_id}-ROLLBACK-{rollback[0]:04d}" if rollback else request_id
        )
        rechecked = (self.repo.root / "audit_private/research/fast_stages" /
                     f"{request_id}-retrieval-recheck.json").is_file()
        fallbacks = ResearchFallbacks(self.repo)
        one_time_target = fallbacks.request_target(request_id)
        initial_context = (
            self.engine.temporary_runtime("RESEARCH_DESK", *one_time_target)
            if one_time_target else nullcontext()
        )
        try:
            try:
                with initial_context:
                    packet = self._research_or_restore_model_prior(
                        request_id=research_request_id, requester_id="WRITER", claim=claim,
                        normalized_claim=normalized_claim, retrieval_result=retrieval_result,
                        prepared_sources=prepared_sources, force_refresh=bool(rechecked or rollback),
                        refresh_reason=("Human-requested manual rollback of an unfinished research item"
                                        if rollback else None))
            except OpenAlexDailyQuotaExhausted:
                raise
            except (ProviderError, RepresentativeUnavailableError):
                # A meeting-wide fallback is conditional on this particular
                # request failing. It never changes another in-flight request.
                if not fallbacks.has_meeting_rule():
                    raise
                source = one_time_target or current_runtime_for(self.repo, "RESEARCH_DESK")
                target = fallbacks.failure_target(source)
                if target is None or target == source:
                    raise
                self.repo.events.append(
                    "RESEARCH_DESK_FALLBACK_APPLIED",
                    {"meeting_id": self.repo.meeting_id, "request_id": request_id,
                     "source": list(source), "target": list(target)},
                    actor="orchestrator",
                )
                with self.engine.temporary_runtime("RESEARCH_DESK", *target):
                    packet = self._research_or_restore_model_prior(
                        request_id=research_request_id, requester_id="WRITER", claim=claim,
                        normalized_claim=normalized_claim, retrieval_result=retrieval_result,
                        prepared_sources=prepared_sources, force_refresh=bool(rechecked or rollback),
                        refresh_reason=("Human-requested manual rollback of an unfinished research item"
                                        if rollback else None))
            result = {"claim": claim, "status": "PACKET", "packet_id": packet.packet_id}
        except (ResearchRequestRejectedError, ResearchQualityControlError) as exc:
            result = {"claim": claim, "status": "UNRESOLVED", "packet_id": None,
                      "reason": str(exc)[:1200]}
        _freeze(self, relative, result)
        return result

    def _fast_supplement_path(self, module_id: str, round_number: int,
                              index: int) -> Path:
        return (self._fast_root() / module_id /
                f"research_round_{round_number}_{index:02d}_openalex_supplement.json")

    def _needs_openalex_supplement(self, module_id: str, round_number: int,
                                   index: int) -> bool:
        if (self.repo.root / self._fast_supplement_path(module_id, round_number, index)).is_file():
            return False
        request_id = f"FAST-{module_id}-{round_number}-{index:02d}"
        relative = Path("audit_private/research/fast_stages") / f"{request_id}-retrieval.json"
        if not (self.repo.root / relative).is_file():
            return False
        saved = self._read_json(relative)
        return (
            "openalex" in saved.get("failed_backend_ids", [])
            and "openalex" not in saved.get("effective_backend_ids", [])
            and any(item.get("backend_id") == "openalex"
                    and "429" in str(item.get("error_summary", ""))
                    for item in saved.get("query_trace", []))
        )

    def _supplement_frozen_fast_claim(
        self, module: OutlineModule, round_number: int, index: int, claim: str,
        *, normalized_claim: NormalizedClaim,
        retrieval_result: ResearchRetrievalResult,
        prepared_sources: tuple[list[dict], list[dict]],
    ) -> dict:
        relative = self._fast_supplement_path(module.module_id, round_number, index)
        if (self.repo.root / relative).is_file():
            return self._read_json(relative)
        if "openalex" not in retrieval_result.effective_backend_ids:
            # A confirmed daily limit may still be in force. Keep the old
            # packet, and leave this supplement eligible for a later resume.
            original = (self._fast_root() / module.module_id /
                        f"research_round_{round_number}_{index:02d}.json")
            return self._read_json(original)
        request_id = f"FAST-{module.module_id}-{round_number}-{index:02d}-OA-SUPP"
        packet = self._research_or_restore_model_prior(
            request_id=request_id, requester_id="WRITER", claim=claim,
            normalized_claim=normalized_claim, retrieval_result=retrieval_result,
            prepared_sources=prepared_sources, force_refresh=True,
        )
        result = {"claim": claim, "status": "PACKET", "packet_id": packet.packet_id,
                  "supplements_request_id": f"FAST-{module.module_id}-{round_number}-{index:02d}"}
        _freeze(self, relative, result)
        return result

    def _fast_normalize_claim(self, module: OutlineModule, round_number: int,
                              index: int, claim: str) -> NormalizedClaim:
        request_id = f"FAST-{module.module_id}-{round_number}-{index:02d}"
        relative = Path("audit_private/research/fast_stages") / f"{request_id}-normalized.json"
        path = self.repo.root / relative
        rollback = self._latest_fast_research_rollback(request_id)
        if rollback is not None:
            version, record = rollback
            override = relative.with_name(f"{request_id}-normalized-rollback-{version:04d}.json")
            if (self.repo.root / override).is_file():
                saved = self._read_json(override)
                if saved.get("claim") != claim:
                    raise ValueError(f"frozen manual normalization conflicts with question: {request_id}")
                return NormalizedClaim.model_validate(saved["normalized_claim"])
            if not path.is_file():
                normalized = self.research_desk.normalize_request(ResearchRequest(
                    requester_id="WRITER", stage=ResearchStage.LITERATURE_REPORT, claim=claim,
                ))
                _freeze(self, relative, {
                    "claim": claim, "normalized_claim": normalized.model_dump(mode="json"),
                })
            original = self._read_json(relative)
            if original.get("claim") != claim:
                raise ValueError(f"frozen normalization conflicts with question: {request_id}")
            prior = NormalizedClaim.model_validate(original["normalized_claim"])
            queries = _frozen_or_call(
                self, relative.with_name(f"{request_id}-query-plan-rollback-{version:04d}.json"),
                FastSearchQueries, "RESEARCH_DESK",
                f"fast_manual_search_replan_{request_id}_v{version}",
                "人类要求对未完成的检索回退排障。只重拟四类检索式；"
                "科学主张、核验问题、适用范围和证据标准保持原样。"
                "优先使用检索后端容易接受的简短主题词，不重复先前被拒绝的复杂语法。"
                "OpenAlex 普通 search 不接受 * 或 ? 通配符；默认改用明确的 OR 词形变体。"
                "只有确有必要且 OpenAlex 支持时才使用通配符；系统会将含通配符的检索式送入"
                "不作词干化的 search.exact。不得改变命题或合并四类证据方向。"
                "不得把人类排障指示当成待证实的科学结论。只返回 JSON。",
                {"original_claim": claim, "frozen_normalization": prior.model_dump(mode="json"),
                 "human_troubleshooting_direction": record["direction"]},
            )
            normalized = prior.model_copy(update=queries.model_dump(mode="python"))
            _freeze(self, override, {
                "claim": claim, "rollback_record_path": record["record_path"],
                "normalized_claim": normalized.model_dump(mode="json"),
            })
            return normalized
        if path.is_file():
            saved = self._read_json(relative)
            if saved.get("claim") != claim:
                raise ValueError(f"frozen normalization conflicts with question: {request_id}")
            return NormalizedClaim.model_validate(saved["normalized_claim"])
        normalized = self.research_desk.normalize_request(ResearchRequest(
            requester_id="WRITER", stage=ResearchStage.LITERATURE_REPORT, claim=claim,
        ))
        _freeze(self, relative, {
            "claim": claim, "normalized_claim": normalized.model_dump(mode="json"),
        })
        return normalized

    def _latest_fast_research_rollback(self, request_id: str) -> tuple[int, dict] | None:
        root = self.repo.root / "audit_private/research/fast_stages"
        files = sorted(root.glob(f"{request_id}-rollback-[0-9][0-9][0-9][0-9].json"))
        if not files:
            return None
        latest = files[-1]
        record = json.loads(latest.read_text(encoding="utf-8"))
        if record.get("request_id") != request_id:
            raise ValueError(f"manual rollback record conflicts with question: {request_id}")
        return int(record["version"]), {**record, "record_path": str(latest.relative_to(self.repo.root))}

    def _fast_technician_search_repair(
        self, request_id: str, claim: str, normalized: NormalizedClaim,
        suffix: str, failure: Exception | None,
    ) -> NormalizedClaim | None:
        """Repair search syntax once, without changing the frozen scientific question."""
        relative = (Path("audit_private/research/fast_stages") /
                    f"{request_id}-technician-query-repair{suffix}.json")
        path = self.repo.root / relative
        if path.is_file():
            saved = self._read_json(relative)
            if (saved.get("claim") != claim or
                    saved.get("original_normalized_claim") != normalized.model_dump(mode="json")):
                raise ValueError(f"frozen Technician query repair conflicts with question: {request_id}")
            return NormalizedClaim.model_validate(saved["repaired_normalized_claim"])

        manifest_path = self.repo.root / "identity_private/meeting_manifest.json"
        if not manifest_path.is_file() or not json.loads(
            manifest_path.read_text(encoding="utf-8")
        ).get("technician_model"):
            return None
        fields = ("supporting_query", "contradictory_query", "limitations_query", "alternatives_query")
        original_queries = {field: getattr(normalized, field) for field in fields}
        self.engine.progress.info(f"{request_id} · OpenAlex 检索持续失败；Technician 正在精简并修订检索式")
        try:
            guard = getattr(self.engine, "recoverable_call", None)
            with (guard() if callable(guard) else nullcontext()):
                proposal = _frozen_or_call(
                    self, relative.with_name(f"{request_id}-technician-query-plan{suffix}.json"),
                    FastSearchQueries, "TECHNICIAN", f"fast_technician_query_repair_{request_id}{suffix}",
                    "You are the meeting Technician. Repair only search expressions after a repeated "
                    "OpenAlex HTTP 500. This status may reflect a server fault rather than a bad query; "
                    "do not assert a cause. Preserve the scientific claim, verification question, scope, "
                    "four evidence directions, and evidence standard. Use short topical terms and explicit "
                    "variants; remove unnecessary modifiers and Boolean nesting. Do not use * or ? by "
                    "default; prefer explicit OR variants. Use wildcards only when necessary and known "
                    "to be supported by the backend. Do not merge evidence directions or invent facts. "
                    "Return exactly four revised queries as JSON.",
                    {"claim": claim, "verification_question": normalized.verification_question,
                     "original_queries": original_queries, "backend_failure": str(failure or "HTTP 500")[:300]},
                )
            proposed_queries = proposal.model_dump(mode="python")
            if (proposed_queries == original_queries or
                    any(len(value) > 400 or "\n" in value for value in proposed_queries.values())):
                raise ValueError("Technician did not provide distinct, bounded search expressions")
            repaired = normalized.model_copy(update=proposed_queries)
            _freeze(self, relative, {
                "claim": claim,
                "original_normalized_claim": normalized.model_dump(mode="json"),
                "repaired_normalized_claim": repaired.model_dump(mode="json"),
                "reason": "REPEATED_OPENALEX_HTTP_500; CAUSE_UNCONFIRMED",
                "query_plan_path": str(relative.with_name(
                    f"{request_id}-technician-query-plan{suffix}.json")),
            })
            self.repo.events.append("TECHNICIAN_SEARCH_REPAIR_RECORDED", {
                "meeting_id": self.repo.meeting_id, "request_id": request_id,
                "record_path": str(relative), "original_queries_preserved": True,
            }, actor="orchestrator")
            return repaired
        except Exception as exc:
            self.engine.progress.info(
                f"{request_id} · Technician 未能形成可用检索式（{type(exc).__name__}）；保留原查询和人工排障入口"
            )
            return None

    def _fast_retrieve_claim(self, module: OutlineModule, round_number: int,
                             index: int, claim: str,
                             normalized: NormalizedClaim, *,
                             allow_recheck: bool = False) -> ResearchRetrievalResult | None:
        if not normalized.is_researchable:
            return None
        request_id = f"FAST-{module.module_id}-{round_number}-{index:02d}"
        rollback = self._latest_fast_research_rollback(request_id)
        suffix = f"-rollback-{rollback[0]:04d}" if rollback else ""
        relative = Path("audit_private/research/fast_stages") / f"{request_id}-retrieval{suffix}.json"
        path = self.repo.root / relative
        if path.is_file():
            recovered_relative = relative.with_name(f"{request_id}-retrieval-recheck{suffix}.json")
            if (self.repo.root / recovered_relative).is_file():
                saved = self._read_json(recovered_relative)
            else:
                saved = self._read_json(relative)
                retriever = _policy_retriever(self.research_desk.retriever)
                needs_recheck = (
                    retriever is not None
                    and (retriever.quota_policy == "wait" or allow_recheck)
                    and not retriever.openalex_suspended()
                    and "openalex" in saved.get("failed_backend_ids", [])
                    and "openalex" not in saved.get("effective_backend_ids", [])
                    and any(item.get("backend_id") == "openalex"
                            and "429" in str(item.get("error_summary", ""))
                            for item in saved.get("query_trace", []))
                )
                if needs_recheck:
                    if (saved.get("claim") != claim
                            or saved.get("normalized_claim") != normalized.model_dump(mode="json")):
                        raise ValueError(f"frozen retrieval conflicts with question: {request_id}")
                    academic = coerce_retrieval_result(
                        retriever.openalex.retrieve(normalized),
                        default_backend_ids=("openalex",),
                    )
                    previous = ResearchRetrievalResult(
                        candidates=copy.deepcopy(saved["candidates"]),
                        query_trace=saved["query_trace"],
                        effective_backend_ids=tuple(saved["effective_backend_ids"]),
                        failed_backend_ids=(),
                    )
                    merged = PolicyResearchRetriever._merge(previous, academic)
                    saved = {
                        "claim": claim,
                        "normalized_claim": normalized.model_dump(mode="json"),
                        "original_retrieval_path": str(relative),
                        "candidates": merged.candidates,
                        "query_trace": merged.query_trace,
                        "effective_backend_ids": list(merged.effective_backend_ids),
                        "failed_backend_ids": list(merged.failed_backend_ids),
                    }
                    _freeze(self, recovered_relative, saved)
            if saved.get("claim") != claim or saved.get("normalized_claim") != normalized.model_dump(mode="json"):
                raise ValueError(f"frozen retrieval conflicts with question: {request_id}")
            return ResearchRetrievalResult(
                candidates=saved["candidates"], query_trace=saved["query_trace"],
                effective_backend_ids=tuple(saved["effective_backend_ids"]),
                failed_backend_ids=tuple(saved["failed_backend_ids"]),
            )
        def retrieve_with_retries(search_claim: NormalizedClaim):
            for attempt in range(self.research_desk.retrieval_max_retries + 1):
                try:
                    return self.research_desk.retriever.retrieve(search_claim)
                except OpenAlexDailyQuotaExhausted:
                    raise  # The scheduler parks this question without occupying a worker.
                except TransientProviderError as exc:
                    if exc.retry_after_seconds is not None:
                        raise  # A timed backoff belongs in the scheduler, not a worker slot.
                    if attempt >= self.research_desk.retrieval_max_retries:
                        raise
                    time.sleep(self.research_desk.retrieval_retry_base_delay_seconds * (2 ** attempt))

        repair_relative = (Path("audit_private/research/fast_stages") /
                           f"{request_id}-technician-query-repair{suffix}.json")
        repaired = (self._fast_technician_search_repair(request_id, claim, normalized, suffix, None)
                    if (self.repo.root / repair_relative).is_file() else None)
        try:
            raw = retrieve_with_retries(repaired or normalized)
        except TransientProviderError as exc:
            if repaired is not None or "OpenAlex retrieval failed: HTTP 500" not in str(exc):
                raise
            repaired = self._fast_technician_search_repair(request_id, claim, normalized, suffix, exc)
            if repaired is None:
                raise
            raw = retrieve_with_retries(repaired)
        retrieved = coerce_retrieval_result(
            raw, default_backend_ids=self.research_desk.retriever.backend_ids,
        )
        _freeze(self, relative, {
            "claim": claim, "normalized_claim": normalized.model_dump(mode="json"),
            "candidates": retrieved.candidates, "query_trace": retrieved.query_trace,
            "effective_backend_ids": list(retrieved.effective_backend_ids),
            "failed_backend_ids": list(retrieved.failed_backend_ids),
            "technician_search_repair_path": str(repair_relative) if repaired is not None else None,
        })
        return retrieved

    def _fast_read_sources(self, module: OutlineModule, round_number: int,
                           index: int, claim: str, normalized: NormalizedClaim,
                           retrieved: ResearchRetrievalResult) -> tuple[list[dict], list[dict]]:
        request_id = f"FAST-{module.module_id}-{round_number}-{index:02d}"
        stage_root = Path("audit_private/research/fast_stages")
        rollback = self._latest_fast_research_rollback(request_id)
        rollback_suffix = f"-rollback-{rollback[0]:04d}" if rollback else ""
        suffix = ("sources-recheck.json" if (self.repo.root / stage_root /
                  f"{request_id}-retrieval-recheck{rollback_suffix}.json").is_file() else "sources.json")
        relative = stage_root / f"{request_id}-{suffix.removesuffix('.json')}{rollback_suffix}.json"
        path = self.repo.root / relative
        if path.is_file():
            saved = self._read_json(relative)
            if (saved.get("claim") != claim
                    or saved.get("normalized_claim") != normalized.model_dump(mode="json")):
                raise ValueError(f"frozen source reading conflicts with question: {request_id}")
            return saved["candidates"], saved["source_reading"]
        self.research_desk._validate_adversarial_query_trace(retrieved.query_trace)
        candidates = self.research_desk._deduplicate_candidate_sources(retrieved.candidates)
        prepared = self.research_desk.source_reader.prepare(
            candidates, question=normalized.verification_question,
            request_key=request_id + rollback_suffix,
        )
        _freeze(self, relative, {
            "claim": claim, "normalized_claim": normalized.model_dump(mode="json"),
            "candidates": prepared[0], "source_reading": prepared[1],
        })
        return prepared

    def _completed_openalex_supplement(self, module_id: str, round_number: int,
                                       index: int) -> bool:
        supplement = self.repo.root / self._fast_supplement_path(
            module_id, round_number, index,
        )
        recheck = (self.repo.root / "audit_private/research/fast_stages" /
                   f"FAST-{module_id}-{round_number}-{index:02d}-retrieval-recheck.json")
        if supplement.is_file() and recheck.is_file():
            try:
                result = json.loads(supplement.read_text(encoding="utf-8"))
                retrieval = json.loads(recheck.read_text(encoding="utf-8"))
                if (result.get("status") == "PACKET"
                        and "openalex" in retrieval.get("effective_backend_ids", [])
                        and "openalex" not in retrieval.get("failed_backend_ids", [])):
                    return True
            except (OSError, ValueError):
                pass
        return False

    def _restored_openalex_warning(self, module_id: str, round_number: int,
                                   index: int) -> str | None:
        if self._completed_openalex_supplement(module_id, round_number, index):
            return None
        trace = (self.repo.root / "audit_private/research/traces" /
                 f"FAST-{module_id}-{round_number}-{index:02d}.json")
        if not trace.is_file():
            return None
        try:
            data = json.loads(trace.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        queries = data.get("search_queries") if isinstance(data, dict) else None
        return openalex_unavailable_reason(queries) if isinstance(queries, list) else None

    def _run_staged_research_jobs(self, jobs: list[tuple[OutlineModule, int, str]],
                                  round_number: int,
                                  outcomes: dict[str, list[dict]]) -> None:
        """Work-conserving normalize/retrieve/finish queues for independent claims."""
        maximum = max(1, min(int(self.research_max_concurrent_claim_groups or 1), len(jobs)))
        pool_capacity = min(32, max(1, len(jobs)))
        pending = deque()
        for module, index, claim in jobs:
            relative = self._fast_root() / module.module_id / f"research_round_{round_number}_{index:02d}.json"
            if (self.repo.root / relative).is_file():
                if self._needs_openalex_supplement(module.module_id, round_number, index):
                    pending.append((module, index, claim))
                else:
                    outcomes[module.module_id].append(self._read_json(relative))
                    supplement = self._fast_supplement_path(module.module_id, round_number, index)
                    if (self.repo.root / supplement).is_file():
                        outcomes[module.module_id].append(self._read_json(supplement))
            else:
                pending.append((module, index, claim))
        deferred: list[tuple[float, str, tuple[OutlineModule, int, str],
                             NormalizedClaim, ResearchRetrievalResult | None,
                             tuple[list[dict], list[dict]] | None]] = []
        rate_limit_retries: dict[str, int] = {}
        futures: dict = {}
        failures: list[tuple[OutlineModule, int, Exception]] = []
        queued_changes: list[dict] = []
        human_retry = False
        human_pause = False
        stop_queue = False
        quota_parked: list[tuple[str, tuple[OutlineModule, int, str],
                                 NormalizedClaim, ResearchRetrievalResult | None,
                                 tuple[list[dict], list[dict]] | None]] = []
        quota_choice = None
        quota_error: OpenAlexDailyQuotaExhausted | None = None
        quota_authorized = False
        interrupt_announced = threading.Event()
        retrieval_cancelled = threading.Event()
        policy_retriever = _policy_retriever(getattr(self.research_desk, "retriever", None))
        openalex_retriever = getattr(policy_retriever, "openalex", None)
        if callable(getattr(openalex_retriever, "set_cancellation_event", None)):
            openalex_retriever.set_cancellation_event(retrieval_cancelled)

        def announce_interrupt() -> None:
            retrieval_cancelled.set()
            if interrupt_announced.is_set():
                return
            interrupt_announced.set()
            self.engine.progress.info(
                "已收到 Ctrl+C：停止派发新任务；OpenAlex 在当前 HTTP 请求结束后不再发起下一条查询。"
                "仍在途的模型或网页请求会安全收尾，之后显示恢复命令。"
            )

        def task_id(job):
            module, index, _claim = job
            return f"fast-desk-{module.module_id}-{round_number}-{index}"

        def normalize(job):
            module, index, claim = job
            self.engine.progress.task_started(task_id(job), "等待模型名额 · 规范化问题")
            return self._fast_normalize_claim(module, round_number, index, claim)

        def retrieve(job, normalized):
            module, index, claim = job
            self.engine.progress.task_started(task_id(job), "检索后端查询中")
            completed = (self.repo.root / self._fast_root() / module.module_id /
                         f"research_round_{round_number}_{index:02d}.json").is_file()
            if completed:
                return self._fast_retrieve_claim(
                    module, round_number, index, claim, normalized,
                    allow_recheck=True,
                )
            return self._fast_retrieve_claim(module, round_number, index, claim, normalized)

        def read_sources(job, normalized, retrieval):
            module, index, claim = job
            self.engine.progress.task_started(task_id(job), "读取候选原文")
            return self._fast_read_sources(module, round_number, index, claim, normalized, retrieval)

        def finish(job, normalized, retrieval, prepared):
            module, index, claim = job
            self.engine.progress.task_started(task_id(job), "读取来源、综合证据与归档")
            completed_relative = (self._fast_root() / module.module_id /
                                  f"research_round_{round_number}_{index:02d}.json")
            retriever = _policy_retriever(getattr(self.research_desk, "retriever", None))
            if (retrieval is not None
                    and "openalex" in getattr(retrieval, "failed_backend_ids", ())
                    and retriever is not None
                    and retriever.quota_policy == "tavily"):
                try:
                    supplemented = self._fast_retrieve_claim(
                        module, round_number, index, claim, normalized,
                        allow_recheck=True,
                    )
                except (OpenAlexDailyQuotaExhausted, TransientProviderError):
                    # The Tavily originals remain usable. The frozen 429 trace
                    # records reduced coverage; never silently claim OA success.
                    supplemented = None
                if supplemented is not None and "openalex" in supplemented.effective_backend_ids:
                    retrieval = supplemented
                    prepared = self._fast_read_sources(
                        module, round_number, index, claim, normalized, retrieval,
                    )
                    self.engine.progress.task_backend_warning(task_id(job), "")
            for attempt in range(2):
                try:
                    if (self.repo.root / completed_relative).is_file():
                        return self._supplement_frozen_fast_claim(
                            module, round_number, index, claim,
                            normalized_claim=normalized, retrieval_result=retrieval,
                            prepared_sources=prepared,
                        )
                    return self._research_claim(
                        module, round_number, index, claim,
                        normalized_claim=normalized, retrieval_result=retrieval,
                        prepared_sources=prepared,
                    )
                except OpenAlexDailyQuotaExhausted:
                    raise
                except (RepresentativeUnavailableError, TransientProviderError):
                    if attempt:
                        raise
                    self.engine.progress.task_started(
                        task_id(job), "供应商瞬时故障；自动重新提交 1/1",
                    )
            raise AssertionError("unreachable")

        with self.engine.progress.defer_model_replacement():
            with (DrainingThreadPoolExecutor(max_workers=pool_capacity, on_interrupt=announce_interrupt) as model_pool,
                  DrainingThreadPoolExecutor(max_workers=pool_capacity, on_interrupt=announce_interrupt) as retrieval_pool,
                  DrainingThreadPoolExecutor(max_workers=pool_capacity, on_interrupt=announce_interrupt) as reading_pool,
                  DrainingThreadPoolExecutor(max_workers=pool_capacity, on_interrupt=announce_interrupt) as finish_pool):
                while pending or futures or deferred or quota_parked or quota_choice:
                    if self.engine.progress.force_control_pending():
                        raise ForcedModelReplacementRequested(
                            "Human force-stopped active model calls in a research batch"
                        )
                    # Ask on the main thread only after work already admitted
                    # to this batch has drained. A background input() thread
                    # cannot be interrupted safely by Ctrl+C.
                    if (quota_choice is not None and not futures
                            and not pending and not deferred):
                        try:
                            with self.engine.progress.interactive_menu():
                                use_backup = bool(quota_choice(quota_error))
                        except Exception:
                            use_backup = False
                        quota_choice = None
                        if use_backup:
                            quota_authorized = True
                            from project_ensemble.runtime.run_controls import record_run_control
                            record_run_control(
                                self.repo, kind="openalex_quota_policy", target=None,
                                value="tavily",
                                reason="Human authorized backup search after confirmed OpenAlex daily exhaustion",
                            )
                            refresh = getattr(self, "batch_retriever_refresh", None)
                            if callable(refresh):
                                refresh()
                            policy = _policy_retriever(getattr(self.research_desk, "retriever", None))
                            if policy is not None and quota_error is not None:
                                policy.suspend_openalex(quota_error.reset_seconds)
                            self.engine.progress.info(
                                "人类已授权 Tavily 接手；只重提尚未落盘的检索，已完成的非检索工作保留"
                            )
                            for stage, job, normalized, retrieved, prepared in quota_parked:
                                if len(futures) >= maximum:
                                    deferred.append((
                                        time.monotonic(), stage, job, normalized,
                                        retrieved, prepared,
                                    ))
                                    continue
                                pool = (retrieval_pool if stage == "retrieve" else
                                        reading_pool if stage == "read" else finish_pool)
                                call = (retrieve if stage == "retrieve" else
                                        read_sources if stage == "read" else finish)
                                args = ((job, normalized) if stage == "retrieve" else
                                        (job, normalized, retrieved) if stage == "read" else
                                        (job, normalized, retrieved, prepared))
                                futures[pool.submit(call, *args)] = (
                                    stage, job, normalized, retrieved, prepared,
                                )
                            quota_parked.clear()
                        else:
                            human_pause = True
                            self.engine.progress.info(
                                "未授权备用搜索；不再启动新的检索，正在完成无需新检索的在途任务"
                            )
                    control_callback = getattr(self, "batch_control_callback", None)
                    if (control_callback is not None
                            and self.engine.progress.control_request_pending()):
                        with self.engine.progress.interactive_menu():
                            changes = control_callback() or []
                        if any(item.get("kind") == "force_stop" for item in changes):
                            self.engine.progress.force_stop_active_calls()
                            raise ForcedModelReplacementRequested(
                                "Human force-stopped active model calls in a research batch"
                            )
                        for item in changes:
                            if item.get("kind") == "runtime_control":
                                from project_ensemble.runtime.run_controls import record_run_control
                                record_run_control(
                                    self.repo, kind=item["control_kind"],
                                    target=item["target"], value=item["value"],
                                    reason=item["reason"],
                                )
                                if item["control_kind"] == "research_parallelism":
                                    maximum = max(1, min(int(item["value"]), len(jobs)))
                                    self.research_max_concurrent_claim_groups = maximum
                                elif item["control_kind"] == "openalex_quota_policy":
                                    refresh = getattr(self, "batch_retriever_refresh", None)
                                    if callable(refresh):
                                        refresh()
                                elif item["control_kind"] == "model_concurrency":
                                    refresh = getattr(self, "batch_concurrency_refresh", None)
                                    if callable(refresh):
                                        refresh()
                            else:
                                service = ModelReplacementService(self.repo)
                                target = (item["provider_id"], item["model_id"])
                                if current_runtime_for(self.repo, item["participant_id"]) != target:
                                    service.replace(**item)
                                    refresh = getattr(self, "batch_replacement_applied", None)
                                    if callable(refresh):
                                        refresh()
                                    refresh = getattr(self, "batch_concurrency_refresh", None)
                                    if callable(refresh):
                                        refresh()
                        if changes:
                            self.engine.progress.info(
                                "新设置已应用于尚未启动的子任务；在途请求保持原调用，缩小的并行上限随槽位释放生效"
                            )
                    now = time.monotonic()
                    # The immediate Ctrl+R menu can change this input while
                    # the batch runs. Only admission of new work changes;
                    # in-flight questions retain their existing calls.
                    maximum = max(1, min(
                        int(self.research_max_concurrent_claim_groups or 1), len(jobs),
                    ))
                    ready = [item for item in deferred if item[0] <= now]
                    deferred = [item for item in deferred if item[0] > now]
                    for _due, stage, job, normalized, retrieved, prepared in ready:
                        if len(futures) >= maximum:
                            deferred.append((now, stage, job, normalized, retrieved, prepared))
                            continue
                        if stage == "retrieve":
                            if human_pause:
                                quota_parked.append((stage, job, normalized, None, None))
                            else:
                                futures[retrieval_pool.submit(retrieve, job, normalized)] = (
                                    stage, job, normalized, None, None,
                                )
                        elif stage == "read":
                            futures[reading_pool.submit(read_sources, job, normalized, retrieved)] = (
                                stage, job, normalized, retrieved, None,
                            )
                        else:
                            futures[finish_pool.submit(finish, job, normalized, retrieved, prepared)] = (
                                stage, job, normalized, retrieved, prepared,
                            )
                    while pending and len(futures) < maximum and not stop_queue:
                        job = pending.popleft()
                        futures[model_pool.submit(normalize, job)] = ("normalize", job, None, None, None)
                    if not futures:
                        if deferred:
                            time.sleep(min(2.0, max(0.0, min(item[0] for item in deferred) - now)))
                        elif quota_choice is not None:
                            time.sleep(0.2)
                        elif human_pause:
                            break
                        continue
                    finished, _ = wait(tuple(futures), timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in finished:
                        stage, job, normalized, retrieved, prepared = futures.pop(future)
                        module, index, _claim = job
                        try:
                            result = future.result()
                        except OpenAlexDailyQuotaExhausted as exc:
                            if stage == "normalize":
                                raise
                            if stop_queue:
                                continue
                            quota_parked.append((stage, job, normalized, retrieved, prepared))
                            self.engine.progress.task_waiting(
                                task_id(job), "OpenAlex 当日额度已核实不足；等待人类选择备用搜索",
                            )
                            self.engine.progress.task_backend_warning(task_id(job), str(exc))
                            if quota_choice is None and not human_pause and not quota_authorized:
                                quota_error = exc
                                callback = getattr(self, "batch_daily_quota_callback", None)
                                if callback is None:
                                    human_pause = True
                                else:
                                    quota_choice = callback
                            elif quota_authorized:
                                # An older in-flight call may still have used
                                # the pre-choice retriever. Retry it under the
                                # newly authorized backup route.
                                stage_, job_, normalized_, retrieved_, prepared_ = quota_parked.pop()
                                deferred.append((
                                    time.monotonic(), stage_, job_, normalized_,
                                    retrieved_, prepared_,
                                ))
                            continue
                        except Exception as exc:
                            if (isinstance(exc, TransientProviderError)
                                    and stage in {"retrieve", "read", "finish"} and not stop_queue
                                    and exc.retry_after_seconds is not None
                                    and rate_limit_retries.get(task_id(job), 0) < 5):
                                rate_limit_retries[task_id(job)] = rate_limit_retries.get(task_id(job), 0) + 1
                                delay = min(60.0, max(0.2, exc.retry_after_seconds)
                                            * (2 ** (rate_limit_retries[task_id(job)] - 1)))
                                deferred.append((
                                    time.monotonic() + delay, stage, job,
                                    normalized, retrieved, prepared,
                                ))
                                self.engine.progress.task_waiting(
                                    task_id(job), f"检索后端限速；约 {delay:.0f} 秒后重试"
                                    f"（{rate_limit_retries[task_id(job)]}/5）",
                                )
                                if "OpenAlex" in str(exc):
                                    self.engine.progress.task_backend_warning(task_id(job), str(exc))
                                continue
                            self.engine.progress.task_finished(
                                task_id(job), failed=True,
                                detail=f"{stage} 阶段失败（{type(exc).__name__}）；结果未落盘",
                            )
                            failures.append((module, index, exc))
                            failure_callback = getattr(self, "batch_failure_callback", None)
                            if failure_callback is not None and not human_pause:
                                issue = FastResearchDeskFailure(module.module_id, round_number, index, exc)
                                with self.engine.progress.interactive_menu():
                                    if failure_callback(issue):
                                        human_retry = True
                                    else:
                                        human_pause = True
                            continue
                        if stage == "normalize":
                            if stop_queue:
                                continue
                            if result.is_researchable:
                                if human_pause:
                                    quota_parked.append(("retrieve", job, result, None, None))
                                elif len(futures) >= maximum:
                                    deferred.append((
                                        time.monotonic(), "retrieve", job, result, None, None,
                                    ))
                                else:
                                    futures[retrieval_pool.submit(retrieve, job, result)] = (
                                        "retrieve", job, result, None, None,
                                    )
                            else:
                                if len(futures) >= maximum:
                                    deferred.append((
                                        time.monotonic(), "finish", job, result, None, None,
                                    ))
                                else:
                                    futures[finish_pool.submit(finish, job, result, None, None)] = (
                                        "finish", job, result, None, None,
                                    )
                        elif stage == "retrieve":
                            if stop_queue:
                                continue
                            if not getattr(result, "failed_backend_ids", ()):
                                self.engine.progress.task_backend_warning(task_id(job), "")
                            if len(futures) >= maximum:
                                deferred.append((
                                    time.monotonic(), "read", job, normalized, result, None,
                                ))
                            else:
                                futures[reading_pool.submit(read_sources, job, normalized, result)] = (
                                    "read", job, normalized, result, None,
                                )
                        elif stage == "read":
                            if stop_queue:
                                continue
                            if len(futures) >= maximum:
                                deferred.append((
                                    time.monotonic(), "finish", job, normalized, retrieved, result,
                                ))
                            else:
                                futures[finish_pool.submit(finish, job, normalized, retrieved, result)] = (
                                    "finish", job, normalized, retrieved, result,
                                )
                        else:
                            if "supplements_request_id" in result:
                                original = (self._fast_root() / module.module_id /
                                            f"research_round_{round_number}_{index:02d}.json")
                                outcomes[module.module_id].append(self._read_json(original))
                            outcomes[module.module_id].append(result)
                            self.engine.progress.task_finished(
                                task_id(job), detail=(
                                    "证据包已落盘" if result["status"] == "PACKET"
                                    else "未能核实；已记录未解决原因"
                                ),
                            )

        controls = [item for item in queued_changes if item.get("kind") == "runtime_control"]
        replacements = [item for item in queued_changes if item.get("kind") != "runtime_control"]
        if replacements:
            service = ModelReplacementService(self.repo)
            latest = {item["participant_id"]: item for item in replacements}
            for item in latest.values():
                target = (item["provider_id"], item["model_id"])
                if current_runtime_for(self.repo, item["participant_id"]) != target:
                    service.replace(**item)
            refresh = getattr(self, "batch_replacement_applied", None)
            if callable(refresh):
                refresh()
        if controls:
            from project_ensemble.runtime.run_controls import record_run_control
            for item in controls:
                record_run_control(
                    self.repo, kind=item["control_kind"], target=item["target"],
                    value=item["value"], reason=item["reason"],
                )
            if any(item["control_kind"] == "openalex_quota_policy" for item in controls):
                refresh = getattr(self, "batch_retriever_refresh", None)
                if callable(refresh):
                    refresh()
        if human_pause:
            raise FastResearchDeskPause(
                "OpenAlex daily budget exhausted; Human declined backup after non-search work finished"
            )
        if failures:
            if human_retry:
                raise FastResearchDeskRetry("Human will retry only missing research questions")
            module, index, cause = failures[0]
            raise FastResearchDeskFailure(module.module_id, round_number, index, cause) from cause
        if controls:
            raise FastResearchDeskRetry("Human changed future-call controls")

    def _research_all_modules(self, taskbook: FastTaskbook,
                              plans: dict[str, FastModulePlan]) -> dict[str, Path]:
        """Generate Writer questions serially, then fetch independent claims together."""
        modules = list(taskbook.outline.modules)
        outcomes: dict[str, list[dict]] = {module.module_id: [] for module in modules}
        stopped: set[str] = set()
        for round_number in range(1, 4):
            jobs: list[tuple[OutlineModule, int, str]] = []
            for module_index, module in enumerate(modules, 1):
                mid = module.module_id
                if mid in stopped or (self.repo.root / self._fast_root() / mid / "coverage.json").is_file():
                    continue
                position = f"模块 {module_index}/{len(modules)} · {mid} · {module.title}"
                self.engine.status.phase = MeetingPhase.LITERATURE_MODULE_RESEARCH
                self.engine.progress.fast_research_step(
                    section_id=mid, section_index=module_index,
                    section_total=len(modules), title=module.title,
                    stage="queries",
                    detail=f"步骤 3/5 · 第 {round_number}/3 轮 · 主笔提交待核查问题",
                )
                query_path = self.repo.root / self._fast_root() / mid / f"query_round_{round_number}.json"
                task_id = f"fast-query-{mid}-{round_number}"
                self.engine.progress.task_batch_started(
                    [TaskProgressItem(
                        task_id=task_id, participant_id="WRITER",
                        initial_state="completed" if query_path.is_file() else "pending",
                        initial_detail="已恢复冻结问题清单" if query_path.is_file() else None,
                        completion_requires_commit=True,
                    )],
                    title=f"{position} · 第 {round_number}/3 轮 · 提交待核查问题",
                )
                if not query_path.is_file():
                    self.engine.progress.task_started(task_id, "主笔正在确定本模块的新增问题")
                try:
                    batch = self._research_round(taskbook, module, plans[mid], round_number, outcomes[mid])
                except Exception:
                    self.engine.progress.task_finished(task_id, failed=True, detail="问题清单未通过校验或未落盘")
                    raise
                submitted_count = len(batch.claims) + len(batch.glossary_claims)
                self.engine.progress.task_finished(
                    task_id, detail=(
                        f"已提交最后一批 {submitted_count} 个问题，等待资料核查"
                        if batch.finished and submitted_count else
                        "已确认无需继续检索" if batch.finished else
                        f"已提交 {submitted_count} 个问题，等待资料核查"
                    ),
                )
                if batch.finished:
                    stopped.add(mid)
                jobs.extend((module, index, claim)
                            for index, claim in enumerate(
                                [*batch.claims, *batch.glossary_claims], 1))
            if jobs:
                self.engine.progress.fast_research_step(
                    section_id=f"ROUND-{round_number}",
                    section_index=round_number, section_total=3,
                    title=f"跨 {len({item[0].module_id for item in jobs})} 个模块 · {len(jobs)} 个独立问题",
                    stage="desk", detail="步骤 3/5 · Research Desk 并行核查证据",
                )
                task_items = []
                for module, index, _claim in jobs:
                    relative = self._fast_root() / module.module_id / f"research_round_{round_number}_{index:02d}.json"
                    original_frozen = (self.repo.root / relative).is_file()
                    restored = original_frozen and not self._needs_openalex_supplement(
                        module.module_id, round_number, index,
                    )
                    task_items.append(TaskProgressItem(
                        task_id=f"fast-desk-{module.module_id}-{round_number}-{index}",
                        participant_id="RESEARCH_DESK",
                        label=f"Research Desk · {module.module_id} · 问题 {index}",
                        initial_state="completed" if restored else "pending",
                        initial_detail=(("已恢复冻结核查结果；OpenAlex 补读已完成"
                                         if self._completed_openalex_supplement(
                                             module.module_id, round_number, index)
                                         else "已恢复冻结核查结果") if restored else
                                        "原证据包已冻结；待补检 OpenAlex" if original_frozen else None),
                        initial_backend_warning=(
                            self._restored_openalex_warning(module.module_id, round_number, index)
                            if restored else None
                        ),
                        completion_requires_commit=True,
                    ))
                self.engine.progress.task_batch_started(
                    task_items,
                    title=f"快速文献调研 · 第 {round_number}/3 轮 · Research Desk 证据核查",
                )
                if getattr(self, "research_desk", None) is not None:
                    self._run_staged_research_jobs(jobs, round_number, outcomes)
                    self.engine.progress.raise_if_control_requested()
                    continue
                maximum = max(1, min(int(self.research_max_concurrent_claim_groups or 1), len(jobs)))
                def run_research_job(module: OutlineModule, index: int, claim: str) -> dict:
                    task_id = f"fast-desk-{module.module_id}-{round_number}-{index}"
                    relative = self._fast_root() / module.module_id / f"research_round_{round_number}_{index:02d}.json"
                    if not (self.repo.root / relative).is_file():
                        self.engine.progress.task_started(task_id, "检索、筛选与证据综合")
                    try:
                        for attempt in range(2):
                            try:
                                result = self._research_claim(module, round_number, index, claim)
                                break
                            except (RepresentativeUnavailableError, TransientProviderError):
                                if attempt:
                                    raise
                                self.engine.progress.task_started(
                                    task_id, "供应商瞬时故障；自动重新提交 1/1",
                                )
                    except Exception as exc:
                        if "openalex" in str(exc).lower():
                            reason = str(exc).split("OpenAlex retrieval failed: ")[-1]
                            self.engine.progress.research_backend_unavailable(
                                "RESEARCH_DESK", "openalex", reason,
                            )
                        self.engine.progress.task_finished(
                            task_id, failed=True,
                            detail=(
                                "供应商拒绝此项内容；等待人类指定备用模型"
                                if isinstance(exc, ProviderContentRejectedError)
                                else f"核查失败（{type(exc).__name__}）；结果未落盘"
                            ),
                        )
                        raise
                    self.engine.progress.task_finished(
                        task_id, detail=("证据包已落盘" if result["status"] == "PACKET"
                                         else "未能核实；已记录未解决原因"),
                    )
                    return result
                failures: list[tuple[OutlineModule, int, Exception]] = []
                queued_replacements: list[dict] = []
                human_retry = False
                human_pause = False
                # Ctrl+R opens a menu immediately, but the selected runtime
                # takes effect at the batch boundary. A Human fallback choice
                # after one failure likewise leaves other jobs running.
                with self.engine.progress.defer_model_replacement():
                    with DrainingThreadPoolExecutor(
                        max_workers=maximum,
                        on_interrupt=lambda: self.engine.progress.info(
                            "已收到 Ctrl+C：取消排队问题，等待已发出的核查调用安全收尾；完成后显示恢复命令。"
                        ),
                    ) as pool:
                        future_to_job = {
                            pool.submit(run_research_job, module, index, claim): (module, index)
                            for module, index, claim in jobs
                        }
                        remaining = set(future_to_job)
                        while remaining:
                            if self.engine.progress.force_control_pending():
                                raise ForcedModelReplacementRequested(
                                    "Human force-stopped active Research Desk calls"
                                )
                            control_callback = getattr(self, "batch_control_callback", None)
                            if (control_callback is not None
                                    and self.engine.progress.control_request_pending()):
                                with self.engine.progress.interactive_menu():
                                    changes = control_callback() or []
                                    queued_replacements.extend(changes)
                                if any(item.get("kind") == "force_stop" for item in changes):
                                    self.engine.progress.force_stop_active_calls()
                                    raise ForcedModelReplacementRequested(
                                        "Human force-stopped active Research Desk calls"
                                    )
                                if any(item.get("kind") == "runtime_control" for item in changes):
                                    # Do not make the Human wait for every queued
                                    # claim. Preserve completed results, drain the
                                    # already-running calls, then rebuild the Desk
                                    # with the new limits and reasoning setting.
                                    for future in tuple(remaining):
                                        if future.cancel():
                                            remaining.remove(future)
                                    self.engine.progress.info(
                                        "运行参数将在当前在途请求结束后生效；未启动问题将重排，"
                                        "已落盘证据包保持不变"
                                    )
                            finished, remaining = wait(
                                remaining, timeout=0.2, return_when=FIRST_COMPLETED,
                            )
                            for future in finished:
                                module, index = future_to_job[future]
                                try:
                                    outcomes[module.module_id].append(future.result())
                                except Exception as exc:
                                    if isinstance(exc, ForcedModelReplacementRequested):
                                        raise
                                    failures.append((module, index, exc))
                                    failure_callback = getattr(self, "batch_failure_callback", None)
                                    if failure_callback is not None and not human_pause:
                                        failure = FastResearchDeskFailure(module.module_id, round_number, index, exc)
                                        with self.engine.progress.interactive_menu():
                                            if failure_callback(failure):
                                                human_retry = True
                                            else:
                                                human_pause = True
                queued_controls = [item for item in queued_replacements
                                   if item.get("kind") == "runtime_control"]
                queued_model_replacements = [item for item in queued_replacements
                                             if item.get("kind") != "runtime_control"]
                if queued_model_replacements:
                    service = ModelReplacementService(self.repo)
                    latest_by_participant = {
                        item["participant_id"]: item for item in queued_model_replacements
                    }
                    applied = 0
                    for item in latest_by_participant.values():
                        target = (item["provider_id"], item["model_id"])
                        if current_runtime_for(self.repo, item["participant_id"]) != target:
                            service.replace(**item)
                            applied += 1
                    refresh = getattr(self, "batch_replacement_applied", None)
                    if callable(refresh) and applied:
                        refresh()
                    self.engine.progress.info(
                        f"本批次已结束；{applied} 项模型替换现已对后续调用生效"
                    )
                if queued_controls:
                    from project_ensemble.runtime.run_controls import record_run_control
                    for item in queued_controls:
                        record_run_control(
                            self.repo, kind=item["control_kind"],
                            target=item["target"], value=item["value"], reason=item["reason"],
                        )
                    if any(item["control_kind"] == "openalex_quota_policy"
                           for item in queued_controls):
                        refresh = getattr(self, "batch_retriever_refresh", None)
                        if callable(refresh):
                            refresh()
                    self.engine.progress.info(
                        f"本批次已结束；{len(queued_controls)} 项运行参数已记录，"
                        "重建调度器后对未完成问题生效"
                    )
                if failures:
                    if human_pause:
                        raise FastResearchDeskPause("Human paused after independent research calls finished")
                    if human_retry:
                        raise FastResearchDeskRetry("Human will retry only missing research questions")
                    module, index, cause = failures[0]
                    raise FastResearchDeskFailure(module.module_id, round_number, index, cause) from cause
                if queued_controls:
                    raise FastResearchDeskRetry("Human changed future-call controls")
                self.engine.progress.raise_if_control_requested()
            if len(stopped) == len(modules):
                break
        dossiers = {}
        for module in modules:
            mid = module.module_id
            coverage = self._fast_root() / mid / "coverage.json"
            if not (self.repo.root / coverage).is_file():
                # Results are ordered by frozen round and query index, not by
                # thread-completion time, so resume is byte-identical.
                ordered = []
                for round_number in range(1, 4):
                    batch_path = self.repo.root / self._fast_root() / mid / f"query_round_{round_number}.json"
                    if not batch_path.is_file():
                        break
                    batch = FastQueryBatch.model_validate_json(batch_path.read_text(encoding="utf-8"))
                    for index, _claim in enumerate(
                        [*batch.claims, *batch.glossary_claims], 1
                    ):
                        item = self._read_json(self._fast_root() / mid
                                               / f"research_round_{round_number}_{index:02d}.json")
                        ordered.append({**item, "purpose": (
                            "GLOSSARY" if index > len(batch.claims) else "MODULE"
                        )})
                        supplement = self._fast_supplement_path(mid, round_number, index)
                        if (self.repo.root / supplement).is_file():
                            ordered.append({**self._read_json(supplement), "purpose": (
                                "GLOSSARY" if index > len(batch.claims) else "MODULE"
                            )})
                    if batch.finished:
                        break
                _freeze(self, coverage, {
                    "module_id": mid,
                    "assessments": [{"packet_ids": [item["packet_id"] for item in ordered
                                                     if item.get("packet_id")],
                                     "status": "FAST_RESEARCH_COMPLETE"}],
                    "outcomes": ordered, "max_query_rounds": 3,
                })
            dossiers[mid] = self._ensure_module_dossier(module, self.repo.root / coverage, [])
        return dossiers

    def _reviewers(self, *, stage: str | None = None) -> list[dict]:
        reviewers = [item for item in self.active
                     if item["runtime"]["persona"] == Persona.LIBRARIAN.value]
        models = {(item["runtime"]["provider_id"], item["runtime"]["model_id"])
                  for item in reviewers}
        if len(models) < 2:
            raise ValueError("fast scientific review requires two distinct active base models")
        return _science_librarians(self, stage=stage)

    def _review_evidence(self, module: OutlineModule, chapter: WriterChapter,
                         dossier: dict) -> dict:
        catalog_path = _effective_chapter_citation_catalog_path(self.repo.root, module.module_id)
        catalog = (json.loads(catalog_path.read_text(encoding="utf-8"))
                   if catalog_path.is_file() else {"sources": []})
        prose = chapter.draft.body_markdown + "\n" + chapter.draft.short_summary
        cited_ids = set(re.findall(r"C\d+-\d+", prose))
        source_ids = {str(item.get("source_id")) for item in catalog.get("sources", [])
                      if item.get("citation_id") in cited_ids and item.get("source_id")}
        return _science_review_evidence(dossier, chapter, cited_source_ids=source_ids)

    def _science_review(self, module: OutlineModule, version: int,
                        chapter: WriterChapter, dossier_path: Path) -> Path:
        relative = self._fast_root() / module.module_id / f"science_review_v{version}.json"
        if (self.repo.root / relative).is_file():
            return self.repo.root / relative
        dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
        evidence = self._review_evidence(module, chapter, dossier)
        reviews = []
        # No reviewer sees another review until every individual response is frozen.
        for record in self._reviewers(stage=f"fast_science_review_{module.module_id}_v{version}"):
            rid = record["representative_id"]
            item = _frozen_or_call(
                self, Path("governance_private/literature_report/fast") / module.module_id
                / f"science_v{version}_{rid}.json",
                ScienceChecklist, rid,
                f"fast_science_review_{module.module_id}_v{version}",
                "独立密封审阅科学事实、证据适用范围、公式与术语；只列实质问题。"
                "也要检查本章理解所必需的术语是否遗漏、解释是否可独立阅读、"
                "开头类比是否误导；把具体缺漏或错误列入 glossary_corrections。"
                "每条问题定位一处正文，说明为何影响结论并给出可核查证据。"
                "本轮实际 UTC 日期在输入中给出；不得用模型记忆中的系统日期否定它。"
                "有界证据若省略发现，不得把未展示误判为不存在；尤其核对正文已引用的来源。"
                "不评价文风、不扩展研究范围、不把内部编号写进读者正文。只返回 JSON。",
                {"module": module.model_dump(mode="json"),
                 "chapter": chapter.model_dump(mode="json"), "bounded_evidence": evidence,
                 "review_date_utc": datetime.now(timezone.utc).date().isoformat()},
            )
            reviews.append({"reviewer_id": rid, "checklist": item.model_dump(mode="json")})
        _freeze(self, relative, {"module_id": module.module_id,
                                 "version": version, "reviews": reviews})
        return self.repo.root / relative

    def _recheck(self, module: OutlineModule, chapter: WriterChapter,
                 review_path: Path, dossier_path: Path, revision: int) -> tuple[bool, Path]:
        relative = self._fast_root() / module.module_id / f"science_recheck_v{revision}.json"
        if (self.repo.root / relative).is_file():
            data = self._read_json(relative)
            return data["passed"], self.repo.root / relative
        review = json.loads(review_path.read_text(encoding="utf-8"))
        evidence = self._review_evidence(
            module, chapter, json.loads(dossier_path.read_text(encoding="utf-8")))
        votes = []
        for record in self._reviewers(stage=f"fast_science_recheck_{module.module_id}_v{revision}"):
            rid = record["representative_id"]
            vote = _frozen_or_call(
                self, Path("governance_private/literature_report/fast") / module.module_id
                / f"recheck_v{revision}_{rid}.json",
                FastResolutionVote, rid,
                f"fast_science_recheck_{module.module_id}_v{revision}",
                "只判断先前科学异议在当前修订稿中是否已得到实质回应。"
                "须先核对本轮引用来源及当前 UTC 日期；先前异议本身若因遗漏证据或日期错误"
                "而不成立，应判为已解决，不要求主笔删去有证据支持的正确表述。"
                "不得提出无关新要求；若仍有实质问题，逐项具体说明。只返回 JSON。",
                {"prior_sealed_review_group": review,
                 "revised_chapter": chapter.model_dump(mode="json"),
                 "bounded_evidence": evidence,
                 "review_date_utc": datetime.now(timezone.utc).date().isoformat()},
            )
            votes.append({"reviewer_id": rid, **vote.model_dump(mode="json")})
        yes = sum(item["resolved"] for item in votes)
        result = {"passed": yes > len(votes) / 2, "yes": yes,
                  "eligible": len(votes), "required_yes": len(votes) // 2 + 1,
                  "votes": votes}
        _freeze(self, relative, result)
        return result["passed"], self.repo.root / relative

    def _evidence_appeal(self, module: OutlineModule, chapter: WriterChapter,
                         review_path: Path, recheck_path: Path, dossier_path: Path,
                         revision: int) -> tuple[bool, Path]:
        """Add an immutable cited-evidence audit before pausing for an adverse vote."""
        relative = self._fast_root() / module.module_id / f"science_evidence_appeal_v{revision}.json"
        issue_id = f"HC-FAST-SCIENCE-{module.module_id}"
        consultation = HumanConsultationService(self.repo)
        if consultation.resolution(issue_id) is not None:
            return False, recheck_path  # A Human ruling is not silently displaced.
        if (self.repo.root / relative).is_file():
            result = self._read_json(relative)
        else:
            dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
            evidence = self._review_evidence(module, chapter, dossier)
            prior = json.loads(recheck_path.read_text(encoding="utf-8"))
            votes = []
            for record in self._reviewers(stage=f"fast_science_evidence_appeal_{module.module_id}_v{revision}"):
                rid = record["representative_id"]
                vote = _frozen_or_call(
                    self, Path("governance_private/literature_report/fast") / module.module_id
                    / f"evidence_appeal_v{revision}_{rid}.json",
                    FastEvidenceAppealVote, rid,
                    f"fast_science_evidence_appeal_{module.module_id}_v{revision}",
                    "这是针对先前未通过票的补充证据核验，不是新一轮写作。"
                    "逐条核对已引用来源的具体发现与本轮 UTC 日期。"
                    "旧异议若只是因为有界摘录省略了实际存在的发现，或使用了错误日期，"
                    "应说明所据来源并判已解决；不能把旧票当作事实。"
                    "若仍存在真实科学问题，指出当前稿的准确位置与来源边界。"
                    "resolved 为 true 时，resolved_objection_explanations 至少解释一项；只返回 JSON。",
                    {"initial_sealed_review": json.loads(review_path.read_text(encoding="utf-8")),
                     "prior_recheck": prior,
                     "current_chapter": chapter.model_dump(mode="json"),
                     "cited_evidence": evidence,
                     "review_date_utc": datetime.now(timezone.utc).date().isoformat()},
                )
                votes.append({"reviewer_id": rid, **vote.model_dump(mode="json")})
            yes = sum(item["resolved"] for item in votes)
            result = {"passed": yes > len(votes) / 2, "yes": yes,
                      "eligible": len(votes), "required_yes": len(votes) // 2 + 1,
                      "votes": votes, "source_review_path": str(review_path.relative_to(self.repo.root)),
                      "source_recheck_path": str(recheck_path.relative_to(self.repo.root)),
                      "policy": "SUPPLEMENTAL_CITED_EVIDENCE_AUDIT; PRIOR_BALLOTS_IMMUTABLE"}
            _freeze(self, relative, result)
        if result["passed"]:
            old_issue = self.repo.root / "human_private/consultations" / f"{issue_id}.issue.json"
            if old_issue.is_file() and consultation.resolution(issue_id) is None:
                consultation.withdraw(
                    issue_id=issue_id,
                    reason="Supplemental cited-evidence review found the frozen adverse vote unsupported; original vote preserved",
                )
        return result["passed"], self.repo.root / relative

    def _consult_local_science_repair(self, module: OutlineModule, revision: int,
                                      recheck_path: Path) -> HumanConsultationResolution:
        legacy_id = f"HC-FAST-SCIENCE-{module.module_id}-FINAL"
        service = HumanConsultationService(self.repo)
        # An older meeting may already be waiting at the two-option final
        # consultation. Its frozen question stays intact; a successor adds the
        # local-repair choice without rewriting institutional history.
        legacy_resolution = service.resolution(legacy_id)
        if legacy_resolution is not None:
            if legacy_resolution.decision == "RETRY_WRITER_REVISION":
                return legacy_resolution.model_copy(update={"decision": "REWRITE_WHOLE_MODULE"})
            return legacy_resolution
        issue_id = f"HC-FAST-SCIENCE-{module.module_id}-LOCAL-V{revision}"
        service.open_issue(HumanConsultationIssue(
            issue_id=issue_id, meeting_id=self.repo.meeting_id,
            reason_code="FAST_SCIENCE_REVIEW_HUMAN_REQUIRED",
            stage="FAST_SCIENCE_REVIEW",
            question=("当前稿仍有具体科学异议。默认只修复涉及的段落/术语并再次核验；"
                      "若异议涉及全章结构，可选择整章重写。也可知悉问题后附限制继续，或保持暂停。"),
            options=["RETRY_WRITER_LOCAL_REPAIR", "REWRITE_WHOLE_MODULE",
                     "ACCEPT_WITH_DISCLOSED_LIMITATION", "KEEP_PAUSED"],
            context={"module_id": module.module_id,
                     "recheck_path": str(recheck_path.relative_to(self.repo.root))},
        ))
        old_path = self.repo.root / "human_private/consultations" / f"{legacy_id}.issue.json"
        if old_path.is_file():
            service.supersede(
                issue_id=legacy_id, successor_issue_id=issue_id,
                reason="local scientific repair added; original issue preserved",
            )
        resolution = service.resolution(issue_id)
        if resolution is None:
            raise LiteratureWritingPaused("FAST_SCIENCE_REVIEW_HUMAN_REQUIRED")
        return resolution

    def _local_science_repair(self, module: OutlineModule, version: int,
                              chapter: WriterChapter, recheck_path: Path) -> tuple[Path, WriterChapter]:
        base = Path("public/literature_report/modules") / module.module_id
        validated_relative = base / "writing_v071" / f"writer_v{version}_validated.json"
        draft_relative = base / "drafts" / f"fast-v{version}.json"
        validated_path = self.repo.root / validated_relative
        if validated_path.is_file():
            restored = WriterChapter.model_validate(self._read_json(validated_relative))
            return self.repo.root / draft_relative, restored
        objections = list(dict.fromkeys(
            str(problem).strip() for problem in self._local_science_objections(recheck_path)
            if str(problem).strip()
        ))
        if not objections:
            raise ValueError("local science repair has no recorded objections")
        _literature_step(
            self, module, "draft",
            f"主笔局部修订第 {version} 版 · 只处理科学异议涉及的段落或术语",
        )
        self.engine.status.phase = MeetingPhase.LITERATURE_MODULE_DRAFTING
        self.engine.progress.status(
            MeetingPhase.LITERATURE_MODULE_DRAFTING,
            f"{module.module_id} · 主笔局部修订第 {version} 版；按审阅意见精确替换指定片段",
        )
        catalog_path = _effective_chapter_citation_catalog_path(self.repo.root, module.module_id)
        original = chapter.model_dump(mode="python")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        all_context_text = chapter.draft.body_markdown + "\n" + chapter.draft.short_summary + "\n" + "\n".join(objections)
        mentioned_packet_ids = set(re.findall(r"\bRP-[A-Z0-9]+\b", all_context_text))
        reader_facing_citation_catalog = [
            {
                "citation_id": source["citation_id"], "title": source.get("title", ""),
                "authors": source.get("authors", []),
                "publication_year": source.get("publication_year"),
                "doi": source.get("doi"),
            }
            for source in catalog.get("sources", [])
            if source.get("citation_id") and (
                not mentioned_packet_ids
                or mentioned_packet_ids.intersection(source.get("packet_ids", []))
            )
        ]
        patch_files = list((self.repo.root / base / "writing_v071").glob(
            f"writer_v{version}_local_patch_c*_a*.json"
        ))
        cycle_numbers = [
            int(match.group(1))
            for path in patch_files
            if (match := re.fullmatch(
                rf"writer_v{version}_local_patch_c(\d+)_a\d+\.json", path.name,
            ))
        ]
        first_cycle = max(cycle_numbers, default=0) + 1
        body_paragraphs = _markdown_paragraph_spans(chapter.draft.body_markdown)
        patch_context = chapter.model_dump(mode="json")
        patch_context["draft"]["body_markdown"] = (
            "正文按 editable_body_paragraphs 中的编号提供；请勿依赖原句全文搜索。"
        )
        patch_context["editable_body_paragraphs"] = [
            {"paragraph_number": index, "text": paragraph}
            for index, (_start, _end, paragraph) in enumerate(body_paragraphs, 1)
        ]
        patch_context["reader_facing_citation_catalog"] = reader_facing_citation_catalog
        # Old frozen chapters and reviewer notes can still contain internal
        # packet handles. Give the writer only reader-facing source IDs and
        # neutral placeholders; the immutable evidence links stay on our side.
        patch_context = _writer_visible_payload(patch_context, catalog)
        last_problem = ""
        for cycle in count(first_cycle):
            for attempt in range(1, 4):
                relative = (base / "writing_v071" /
                            f"writer_v{version}_local_patch_c{cycle:02d}_a{attempt:02d}.json")
                proposal = _frozen_or_call(
                    self, relative, FastLocalScienceRepair, "WRITER",
                    f"fast_local_science_repair_{module.module_id}_v{version}_c{cycle}_a{attempt}",
                    "只处理列出的科学异议，不重写整章。逐条提交精确的旧文本与替换文本；"
                    "正文按 editable_body_paragraphs 的 paragraph_number 定位；优先用该编号和 new_text 替换整段，"
                    "无需复制 old_text。若提供 old_text，它必须与编号对应的整段原文一致。"
                    "同一段落涉及多条异议时合并到同一修改并列出全部 objection_numbers；每段只提交一次替换。"
                    "如需改写完整短摘要，使用 target_field=short_summary、replace_entire_field=true 和 new_text；"
                    "不要复制旧摘要作为定位锚点。已有术语表用 glossary_edits 按 term 和 field（explanation 或 formula）定位，"
                    "可选 expected_text 用于确认当前内容；不得用整段全文搜索来定位重复术语。"
                    "所有正文和短摘要引用只能使用 reader_facing_citation_catalog 中的 C 文献编号。"
                    "历史引用占位符 [来源待核] 必须结合相邻论述及目录改成正确 C 编号；不得保留占位符、猜造编号或输出内部证据包编号。"
                    "若修订段落或完整摘要原先含有引用，替换后须保留相应 C 文献引文；系统会据这些引文重建证据来源列表。"
                    "如果异议要求补充当前不存在的术语表词条，可在 glossary_additions 中提交新词条及其 objection_numbers；"
                    "新词条不得与现有词条重复，定义只能依据当前模块已核查资料，并在有来源时填写本章目录中的 C 文献编号。"
                    "若证据不足以给出可靠定义，应通过局部正文修改收窄或解释用法，不得猜测。"
                    "可以收窄或撤回未获证实的断言，不得增加未核实事实、新来源或新的研究范围。"
                    "替换后的正文与释义面向研究读者，不得照搬任务指令中的内部流程用语；避免把‘核对、条目、登记、接口、交付、缺口’等词当作学术概念使用，"
                    "也避免生造复杂复合名词。"
                    "每条异议至少由一项局部文字替换或一项新增词条处理；单项处理可对应多条异议。"
                    "新增词条也须明确关联至少一条异议。保持其他段落、标题、引文及未涉及的术语表条目原样。"
                    "只返回 JSON。",
                    {"current_draft": patch_context,
                     "numbered_objections": _writer_visible_payload([
                         {"number": index, "objection": problem}
                         for index, problem in enumerate(objections, 1)
                     ], catalog),
                     "previous_attempt_problem": _writer_visible_payload(last_problem, catalog)},
                )
                try:
                    changed = dict(original)
                    covered: set[int] = set()
                    paragraph_replacements: list[tuple[int, int, str]] = []
                    used_paragraphs: set[int] = set()
                    replaced_fields: set[str] = set()
                    for edit in proposal.edits:
                        if any(number > len(objections) for number in edit.objection_numbers):
                            raise ValueError("edit references an objection outside the frozen list")
                        if edit.replace_entire_field:
                            assert edit.target_field == "short_summary"
                            if edit.target_field in replaced_fields:
                                raise ValueError("short_summary has multiple whole-field replacements")
                            changed["draft"][edit.target_field] = edit.new_text
                            replaced_fields.add(edit.target_field)
                            covered.update(edit.objection_numbers)
                            continue
                        if edit.paragraph_number is None:
                            continue
                        paragraph_number = edit.paragraph_number
                        if paragraph_number > len(body_paragraphs):
                            raise ValueError(
                                f"paragraph_number {paragraph_number} is outside the editable body"
                            )
                        if paragraph_number in used_paragraphs:
                            raise ValueError(
                                f"body paragraph {paragraph_number} has multiple replacements"
                            )
                        start, end, current_paragraph = body_paragraphs[paragraph_number - 1]
                        if edit.old_text is not None and edit.old_text.strip() != current_paragraph.strip():
                            raise ValueError(
                                f"old_text does not match editable body paragraph {paragraph_number}"
                            )
                        if re.search(r"\bRP-[A-Z0-9]+\b", current_paragraph) and not re.search(
                            r"\[C[0-9]+-[0-9]+\]", edit.new_text,
                        ):
                            raise ValueError(
                                f"replacement for cited paragraph {paragraph_number} must retain a C citation"
                            )
                        used_paragraphs.add(paragraph_number)
                        paragraph_replacements.append((start, end, edit.new_text))
                        covered.update(edit.objection_numbers)
                    for start, end, replacement in sorted(
                        paragraph_replacements, key=lambda item: item[0], reverse=True,
                    ):
                        body = changed["draft"]["body_markdown"]
                        changed["draft"]["body_markdown"] = body[:start] + replacement + body[end:]

                    for edit in proposal.edits:
                        if edit.paragraph_number is not None or edit.replace_entire_field:
                            continue
                        assert edit.old_text is not None
                        locations: list[tuple[str, int | None, str]] = []
                        fields = (edit.target_field,) if edit.target_field else (
                            "body_markdown", "short_summary",
                        )
                        for field in fields:
                            assert field is not None
                            if changed["draft"][field].count(edit.old_text) == 1:
                                locations.append((field, None, field))
                        for index, term in enumerate(changed.get("glossary_additions", [])):
                            for field in ("explanation", "formula"):
                                value = term.get(field)
                                if isinstance(value, str) and value.count(edit.old_text) == 1:
                                    locations.append((field, index, f"glossary_additions[{index}].{field}"))
                        if len(locations) != 1:
                            raise ValueError("old_text must occur exactly once in one editable field")
                        field, index, _label = locations[0]
                        target = changed["draft"] if index is None else changed["glossary_additions"][index]
                        target[field] = target[field].replace(edit.old_text, edit.new_text, 1)
                        covered.update(edit.objection_numbers)

                    if "short_summary" in replaced_fields:
                        if (re.search(r"\bRP-[A-Z0-9]+\b", chapter.draft.short_summary)
                                and not re.search(r"\[C[0-9]+-[0-9]+\]", changed["draft"]["short_summary"])):
                            raise ValueError("replacement for cited short_summary must retain a C citation")

                    for edit in proposal.glossary_edits:
                        if any(number > len(objections) for number in edit.objection_numbers):
                            raise ValueError(
                                "glossary edit references an objection outside the frozen list"
                            )
                        matches = [
                            term for term in changed.get("glossary_additions", [])
                            if str(term.get("term", "")).casefold() == edit.term.casefold()
                        ]
                        if len(matches) != 1:
                            raise ValueError(
                                f"glossary term must identify exactly one existing entry: {edit.term}"
                            )
                        current_text = matches[0].get(edit.field)
                        if not isinstance(current_text, str):
                            raise ValueError(
                                f"glossary field {edit.field} is not editable for {edit.term}"
                            )
                        if edit.expected_text is not None and current_text != edit.expected_text:
                            raise ValueError(
                                f"glossary field changed since the patch context: {edit.term}.{edit.field}"
                            )
                        matches[0][edit.field] = edit.new_text
                        covered.update(edit.objection_numbers)

                    existing_terms = {
                        str(term.get("term", "")).casefold()
                        for term in changed.get("glossary_additions", [])
                    }
                    glossary_root = self.repo.root / "public/literature_report/writing_v071"
                    prior_glossary_paths = sorted(
                        path for path in glossary_root.glob("glossary_after_RM-*.json")
                        if path.stem.removeprefix("glossary_after_") < module.module_id
                    )
                    if prior_glossary_paths:
                        prior_glossary = json.loads(
                            prior_glossary_paths[-1].read_text(encoding="utf-8")
                        )
                        existing_terms.update(
                            str(term.get("term", "")).casefold()
                            for term in prior_glossary
                        )
                    chapter_sources = {
                        source["citation_id"]: source
                        for source in catalog.get("sources", [])
                        if isinstance(source, dict) and source.get("citation_id")
                    }
                    for addition in proposal.glossary_additions:
                        if any(number > len(objections) for number in addition.objection_numbers):
                            raise ValueError(
                                "glossary addition references an objection outside the frozen list"
                            )
                        entry = addition.entry
                        normalized_term = entry.term.casefold()
                        if normalized_term in existing_terms:
                            raise ValueError(
                                "glossary term already exists; edit its current definition instead: "
                                f"{entry.term}"
                            )
                        existing_terms.add(normalized_term)
                        unknown_citations = sorted(set(entry.source_citation_ids) - chapter_sources.keys())
                        if unknown_citations:
                            raise ValueError(
                                "glossary addition cites sources outside the chapter catalog: "
                                f"{unknown_citations}"
                            )
                        missing_packets = sorted(
                            citation_id for citation_id in entry.source_citation_ids
                            if not chapter_sources[citation_id].get("packet_ids")
                        )
                        if missing_packets:
                            raise ValueError(
                                "glossary addition sources have no linked evidence packets: "
                                f"{missing_packets}"
                            )
                        linked_packet_ids = [
                            packet_id for citation_id in entry.source_citation_ids
                            for packet_id in chapter_sources[citation_id]["packet_ids"]
                        ]
                        self._validate_citations(linked_packet_ids)
                        changed.setdefault("glossary_additions", []).append(
                            entry.model_dump(mode="python")
                        )
                        covered.update(addition.objection_numbers)
                    if covered != set(range(1, len(objections) + 1)):
                        raise ValueError("every current objection needs a local edit")
                    repaired = WriterChapter.model_validate(changed)
                    draft = repaired.draft
                    legacy_citations_before = set(re.findall(
                        r"\bRP-[A-Z0-9]+\b",
                        chapter.draft.body_markdown + "\n" + chapter.draft.short_summary,
                    ))
                    legacy_citations_after = re.findall(
                        r"\bRP-[A-Z0-9]+\b", draft.body_markdown + "\n" + draft.short_summary,
                    )
                    if legacy_citations_before and legacy_citations_after:
                        raise ValueError(
                            "replace every legacy packet marker in the body and short summary with a C citation"
                        )
                    draft = self._validate_chapter_source_citations(
                        module, draft, catalog_path,
                        previous_draft=None if legacy_citations_before else chapter.draft,
                    )
                    self._validate_citations(draft.cited_packet_ids)
                except ValueError as exc:
                    last_problem = str(exc)
                    continue
                repaired = repaired.model_copy(update={"draft": draft})
                _freeze(self, validated_relative, repaired)
                _freeze(self, draft_relative, draft)
                _freeze(self, base / "writing_v071" / f"writer_v{version}_local_patch_applied.json", {
                    "source_draft_path": str(base / "writing_v071" /
                                             f"writer_v{version - 1}_validated.json"),
                    "source_sha256": hashlib.sha256(chapter.model_dump_json().encode()).hexdigest(),
                    "patch_path": str(relative),
                    "objections": objections,
                    "result_sha256": hashlib.sha256(repaired.model_dump_json().encode()).hexdigest(),
                    "policy": "TARGETED_TEXT_AND_GLOSSARY_PATCHES_ONLY",
                })
                return self.repo.root / draft_relative, repaired
            decision = self._consult(
                f"HC-FAST-SCIENCE-{module.module_id}-LOCAL-FORMAT-V{version}-C{cycle}",
                "FAST_SCIENCE_REVIEW",
                "局部修订连续三次未能精确定位或通过格式核验。可换模型重试局部修订、"
                "改为整章重写，或保持暂停。",
                ["RETRY_WRITER_LOCAL_REPAIR", "REWRITE_WHOLE_MODULE", "PAUSE_FOR_MANUAL_REVIEW"],
                {"module_id": module.module_id,
                 "recheck_path": str(recheck_path.relative_to(self.repo.root)),
                 "last_problem": last_problem},
            )
            if decision.decision == "REWRITE_WHOLE_MODULE":
                raise FastWholeModuleRewriteRequested
            if decision.decision != "RETRY_WRITER_LOCAL_REPAIR":
                raise LiteratureWritingPaused("FAST_SCIENCE_REVIEW_HUMAN_REQUIRED")

    def _local_science_objections(self, source_path: Path) -> list[str]:
        """Read either an initial sealed checklist or a later recheck docket."""
        record = self._read_json(source_path.relative_to(self.repo.root))
        if "votes" in record:
            return list(dict.fromkeys(
                str(problem).strip()
                for vote in record.get("votes", [])
                for problem in vote.get("remaining_material_problems", [])
                if str(problem).strip()
            ))
        objections: list[str] = []
        for item in record.get("reviews", []):
            checklist = item.get("checklist") or {}
            for issue in checklist.get("issues", []):
                objections.append(
                    "位置：" + str(issue.get("location_excerpt", "未定位"))
                    + "；受质疑表述：" + str(issue.get("questioned_claim", "未提供"))
                    + "；问题及影响：" + str(issue.get("why_it_matters", "未说明"))
                    + ("；建议处理：" + str(issue["suggested_response"])
                       if issue.get("suggested_response") else "")
                )
            objections.extend(
                "术语表修订意见：" + str(problem)
                for problem in checklist.get("glossary_corrections", [])
                if str(problem).strip()
            )
        return list(dict.fromkeys(objections))

    def _recheck_science_revision(self, module: OutlineModule, chapter: WriterChapter,
                                  review_path: Path, dossier_path: Path,
                                  revision: int) -> tuple[bool, Path]:
        passed, recheck_path = self._recheck(
            module, chapter, review_path, dossier_path, revision,
        )
        if not passed:
            passed, recheck_path = self._evidence_appeal(
                module, chapter, review_path, recheck_path, dossier_path, revision,
            )
        return passed, recheck_path

    def _write_module(self, outline: FrozenResearchOutline, module: OutlineModule,
                      index: int, dossier_path: Path) -> dict:
        base = Path("public/literature_report/modules") / module.module_id
        outcome = base / "module_outcome.json"
        if (self.repo.root / outcome).is_file():
            self.engine.progress.info(f"{module.module_id} · 已恢复冻结模块")
            return self._read_json(outcome)
        self._v071_progress = (index, len(outline.modules))
        approved = self._effective_fast_outline(module.module_id)
        draft_path, chapter = _writer_chapter(self, module, 1, dossier_path, approved)
        review_path = self._science_review(module, 1, chapter, dossier_path)
        review = json.loads(review_path.read_text(encoding="utf-8"))
        material = any(item["checklist"]["issues"] or item["checklist"]["glossary_corrections"]
                       for item in review["reviews"])
        if material:
            validated_v2 = self.repo.root / base / "writing_v071/writer_v2_validated.json"
            local_v2 = self.repo.root / base / "writing_v071/writer_v2_local_patch_applied.json"
            resuming_legacy_rewrite = validated_v2.is_file() and not local_v2.is_file()
            if resuming_legacy_rewrite:
                # Resume meetings that had already entered the older whole-chapter
                # revision path; do not reinterpret their frozen version numbers.
                draft_path, chapter = _writer_chapter(
                    self, module, 2, dossier_path, approved,
                    previous=self.repo.root / base / "writing_v071/writer_v1_validated.json",
                    review_path=review_path,
                )
            else:
                self.engine.progress.info(
                    f"{module.module_id} · 科学修订默认采用局部补丁；仅改异议涉及的段落或术语"
                )
                try:
                    draft_path, chapter = self._local_science_repair(
                        module, 2, chapter, review_path,
                    )
                except FastWholeModuleRewriteRequested:
                    draft_path, chapter = _writer_chapter(
                        self, module, 2, dossier_path, approved,
                        previous=self.repo.root / base / "writing_v071/writer_v1_validated.json",
                        review_path=review_path,
                    )
            passed, recheck_path = self._recheck_science_revision(
                module, chapter, review_path, dossier_path, 2,
            )

            revision = 2
            accepted_after_legacy_ruling = False
            if not passed and resuming_legacy_rewrite:
                # Honor any already-open Human decision from the former flow
                # before creating a successor consultation with new options.
                decision = self._consult(
                    f"HC-FAST-SCIENCE-{module.module_id}", "FAST_SCIENCE_REVIEW",
                    f"{module.module_id} 的整章返修仍有未解决异议。可沿旧流程再作一次整章返修，"
                    "也可在知悉问题后附限制继续。",
                    ["RETRY_WRITER_REVISION", "ACCEPT_WITH_DISCLOSED_LIMITATION"],
                    {"module_id": module.module_id,
                     "review_path": str(review_path.relative_to(self.repo.root)),
                     "recheck_path": str(recheck_path.relative_to(self.repo.root))},
                )
                if decision.decision == "RETRY_WRITER_REVISION":
                    revision = 3
                    draft_path, chapter = _writer_chapter(
                        self, module, revision, dossier_path, approved,
                        previous=self.repo.root / base / "writing_v071/writer_v2_validated.json",
                        review_path=recheck_path,
                    )
                    passed, recheck_path = self._recheck_science_revision(
                        module, chapter, review_path, dossier_path, revision,
                    )
                elif decision.decision == "ACCEPT_WITH_DISCLOSED_LIMITATION":
                    accepted_after_legacy_ruling = True
            while not passed and not accepted_after_legacy_ruling:
                decision = self._consult_local_science_repair(
                    module, revision + 1, recheck_path,
                )
                if decision.decision == "KEEP_PAUSED":
                    raise LiteratureWritingPaused("FAST_SCIENCE_REVIEW_HUMAN_REQUIRED")
                if decision.decision == "ACCEPT_WITH_DISCLOSED_LIMITATION":
                    break
                revision += 1
                if decision.decision == "RETRY_WRITER_LOCAL_REPAIR":
                    try:
                        draft_path, chapter = self._local_science_repair(
                            module, revision, chapter, recheck_path,
                        )
                    except FastWholeModuleRewriteRequested:
                        draft_path, chapter = _writer_chapter(
                            self, module, revision, dossier_path, approved,
                            previous=self.repo.root / base / "writing_v071" /
                            f"writer_v{revision - 1}_validated.json",
                            review_path=recheck_path,
                        )
                elif decision.decision == "REWRITE_WHOLE_MODULE":
                    draft_path, chapter = _writer_chapter(
                        self, module, revision, dossier_path, approved,
                        previous=self.repo.root / base / "writing_v071" / f"writer_v{revision - 1}_validated.json",
                        review_path=recheck_path,
                    )
                else:
                    raise ValueError("unknown fast-science repair decision")
                passed, recheck_path = self._recheck_science_revision(
                    module, chapter, review_path, dossier_path, revision,
                )
            if not passed:
                note_path = base / "fast_science_limitation.json"
                _freeze(self, note_path, {"checks": [
                    {"status": "MATERIAL_PROBLEM", "problem": problem}
                    for vote in self._read_json(recheck_path.relative_to(self.repo.root)).get("votes", [])
                    for problem in vote.get("remaining_material_problems", [])]})
            else:
                note_path = None
        else:
            note_path = None
        _freeze_glossary(self, module, chapter)
        payload = {"module_id": module.module_id, "title": module.title,
                   "status": "ADOPTED", "draft_path": str(draft_path.relative_to(self.repo.root)),
                   "short_summary": chapter.draft.short_summary,
                   "cited_packet_ids": chapter.draft.cited_packet_ids,
                   "unresolved_ids": chapter.draft.unresolved_ids,
                   "dissents_path": None, "writing_policy": "fast",
                   "local_science_check_status": "MATERIAL_PROBLEM" if note_path else "PASS",
                   "local_science_check_path": str(note_path) if note_path else None,
                   "glossary_path": str(Path("public/literature_report/writing_v071")
                                        / f"glossary_after_{module.module_id}.json")}
        _freeze(self, outcome, payload)
        return payload

    def _synthesis(self, taskbook: FastTaskbook, completed: list[dict]) -> Path:
        relative = self._fast_root() / "whole_report_synthesis.json"
        summaries = [{"module_id": item["module_id"], "title": item["title"],
                      "summary": item["short_summary"]} for item in completed]
        glossary_path = (self.repo.root / completed[-1]["glossary_path"]
                         if completed and completed[-1].get("glossary_path") else None)
        glossary_terms = ([item.get("term", "") for item in json.loads(
            glossary_path.read_text(encoding="utf-8"))]
            if glossary_path and glossary_path.is_file() else [])
        combined_catalog = {"sources": []}
        for item in completed:
            catalog_path = _effective_chapter_citation_catalog_path(
                self.repo.root, item["module_id"],
            )
            if catalog_path.is_file():
                combined_catalog["sources"].extend(
                    json.loads(catalog_path.read_text(encoding="utf-8"))["sources"]
                )
        bounded_evidence = []
        bounded_module_ids = []
        for item in completed:
            dossier_path = (self.repo.root / "public/literature_report/modules"
                            / item["module_id"] / "research/evidence_dossier.json")
            if dossier_path.is_file():
                bounded_module_ids.append(item["module_id"])
                bounded_evidence.append(_science_review_evidence(
                    json.loads(dossier_path.read_text(encoding="utf-8")),
                    packet_ids=set(item.get("cited_packet_ids", [])),
                    max_chars=14_000,
                ))
        writer_bounded_evidence = []
        for module_id, evidence in zip(bounded_module_ids, bounded_evidence):
            catalog_path = _effective_chapter_citation_catalog_path(self.repo.root, module_id)
            local_catalog = (json.loads(catalog_path.read_text(encoding="utf-8"))
                             if catalog_path.is_file() else {"sources": []})
            writer_bounded_evidence.append(_writer_visible_payload(evidence, local_catalog))
        synthesis = _frozen_or_call(
            self, relative, FastWholeSynthesis, "WRITER", "fast_whole_report_synthesis",
            "为已经完成科学复核的章节撰写全文标题、摘要、引言、方法说明与必要的跨模块综合。"
            "不重写已经审阅过的模块正文，不添加未经核查的科学事实。跨模块综合若没有证据支撑则留空。"
            "摘要与引言只概括已有模块；结论可留空。body_sections 必须为空数组，"
            "不要在跨模块综合中引入术语表未解释的关键专门方法名；确需使用时就地简短定义，"
            "说明它与已有章节的关系，不得以定义为名增加未经核查的新事实。"
            "模块插入顺序由程序决定。不得加入新的 MODEL_PRIOR 事实；"
            "model_prior_claims 必须为空数组。引用必须对应已核实来源。只返回 JSON。",
            _writer_visible_payload({"taskbook": taskbook.model_dump(mode="json"),
             "frozen_module_summaries": summaries,
             "frozen_glossary_terms": glossary_terms,
             "writing_preferences": self._writing_preferences()}, combined_catalog),
        )
        synthesis, effective_relative = self._normalize_synthesis_citation_metadata(
            synthesis, relative, combined_catalog,
        )
        if synthesis.cross_module_synthesis or synthesis.conclusion:
            # Cross-module conclusions are substantive and receive the same
            # sealed independent science check before assembly.
            review_relative = self._fast_root() / "synthesis_science_reviews.json"
            if not (self.repo.root / review_relative).is_file():
                reviews = []
                for record in self._reviewers(stage="fast_synthesis_science_review"):
                    rid = record["representative_id"]
                    value = _frozen_or_call(
                        self, Path("governance_private/literature_report/fast")
                        / f"synthesis_science_{rid}.json",
                        ScienceChecklist, rid, "fast_synthesis_science_review",
                        "仅审阅跨模块综合和结论是否超出已冻结模块支持；摘要与引言无需扩展审查。"
                        "只列实质事实问题，不要求补写新研究。只返回 JSON。",
                        {"cross_module_synthesis": synthesis.cross_module_synthesis,
                         "conclusion": synthesis.conclusion,
                         "frozen_module_summaries": summaries,
                         "bounded_module_evidence": bounded_evidence},
                    )
                    reviews.append({"reviewer_id": rid, "checklist": value.model_dump(mode="json")})
                _freeze(self, review_relative, {"reviews": reviews})
            reviews = self._read_json(review_relative)["reviews"]
            if any(item["checklist"]["issues"] for item in reviews):
                revised_relative = self._fast_root() / "whole_report_synthesis_revised.json"
                revised = _frozen_or_call(
                    self, revised_relative, FastWholeSynthesis, "WRITER",
                    "fast_whole_report_synthesis_repair",
                    "只修复智库长指出的跨模块科学问题；不能改动已审阅章节或增加无来源新结论。"
                    "只返回 JSON。",
                    _writer_visible_payload({"original": synthesis.model_dump(mode="json"), "reviews": reviews,
                     "frozen_module_summaries": summaries,
                     "bounded_module_evidence": writer_bounded_evidence}, combined_catalog),
                )
                synthesis = revised
                relative = revised_relative
                synthesis, effective_relative = self._normalize_synthesis_citation_metadata(
                    synthesis, relative, combined_catalog,
                )
                # An unresolved substantive dispute blocks publication.
                votes = []
                for record in self._reviewers(stage="fast_synthesis_science_recheck"):
                    rid = record["representative_id"]
                    value = _frozen_or_call(
                        self, Path("governance_private/literature_report/fast")
                        / f"synthesis_recheck_{rid}.json",
                        FastResolutionVote, rid, "fast_synthesis_science_recheck",
                        "判断先前跨模块事实异议是否在修订稿中得到实质解决。只返回 JSON。",
                        {"reviews": reviews, "revised": synthesis.model_dump(mode="json"),
                         "frozen_module_summaries": summaries,
                         "bounded_module_evidence": bounded_evidence},
                    )
                    votes.append(value)
                yes = sum(vote.resolved for vote in votes)
                _freeze(self, self._fast_root() / "synthesis_science_recheck.json",
                        {"yes": yes, "eligible": len(votes),
                         "required_yes": len(votes) // 2 + 1,
                         "passed": yes > len(votes) / 2,
                         "remaining_material_problems": [problem for vote in votes
                                                         for problem in vote.remaining_material_problems]})
                if yes <= len(votes) / 2:
                    decision = self._consult(
                        "HC-FAST-SYNTHESIS-SCIENCE", "FAST_SCIENCE_REVIEW",
                        "跨模块综合仍未获智库长过半认可；是否去除争议性综合与结论后出版？",
                        ["REMOVE_UNSUPPORTED_SYNTHESIS", "KEEP_PAUSED"],
                        {"review_path": str(review_relative)},
                    )
                    if decision.decision == "KEEP_PAUSED":
                        raise LiteratureWritingPaused("FAST_SCIENCE_REVIEW_HUMAN_REQUIRED")
                    retained = "\n".join((synthesis.abstract, synthesis.introduction,
                                          synthesis.methods))
                    retained_packets = list(dict.fromkeys(
                        packet_id for packet_id in synthesis.cited_packet_ids
                        if f"[{packet_id}]" in retained
                    ))
                    stripped = synthesis.model_copy(update={"cross_module_synthesis": "",
                                                         "conclusion": "",
                                                         "cited_packet_ids": retained_packets})
                    relative = self._fast_root() / "whole_report_synthesis_stripped.json"
                    _freeze(self, relative, stripped)
                    effective_relative = relative
        return self.repo.root / effective_relative

    def _normalize_synthesis_citation_metadata(
        self, synthesis: FastWholeSynthesis, source_relative: Path, catalog: dict,
        *, repair_attempt: int = 1,
    ) -> tuple[FastWholeSynthesis, Path]:
        """Keep frozen Writer text; C source IDs are not Research Desk packet IDs.

        The report assembler already resolves every inline C citation through
        the frozen per-chapter source catalog.  The legacy metadata field only
        carries *inline RP packet* IDs, so remove C IDs from that field in a
        separate immutable presentation input, never from the original draft.
        """
        by_source = {item["citation_id"]: item for item in catalog["sources"]}
        known_packets = {item["packet_id"] for item in self._compact_evidence_index()}
        source_ids = [identifier for identifier in synthesis.cited_packet_ids
                      if re.fullmatch(r"C[0-9]+-[0-9]+", identifier)]
        inline_source_ids = set(re.findall(
            r"C[0-9]+-[0-9]+", "\n".join((
                synthesis.abstract, synthesis.introduction, synthesis.methods,
                synthesis.cross_module_synthesis, synthesis.conclusion,
            )),
        ))
        used_source_ids = set(source_ids) | inline_source_ids
        unknown = sorted(
            (used_source_ids - by_source.keys())
            | {identifier for identifier in used_source_ids
               if identifier in by_source and not by_source[identifier].get("packet_ids")}
            | (set(synthesis.cited_packet_ids) - known_packets - set(source_ids))
        )
        if unknown:
            issue_id = f"HC-FAST-SYNTHESIS-CITATION-METADATA-C{repair_attempt:02d}"
            decision = self._consult(
                issue_id, "FAST_SYNTHESIS_CITATION_METADATA",
                "全篇导读使用了冻结文献目录之外的引文编号，无法机械确定来源。"
                "原稿和已完成模块保持冻结；请选择让主笔只修引文，或保持暂停。",
                ["RETRY_WRITER_CITATIONS", "KEEP_PAUSED"],
                {"unknown_ids": unknown, "source_path": str(source_relative)},
            )
            if decision.decision != "RETRY_WRITER_CITATIONS":
                raise LiteratureWritingPaused("FAST_SYNTHESIS_CITATION_METADATA_HUMAN_REQUIRED")
            repair_relative = self._fast_root() / (
                f"whole_report_synthesis_citation_repair_c{repair_attempt:02d}.json"
            )
            repaired = _frozen_or_call(
                self, repair_relative, FastWholeSynthesis, "WRITER",
                "fast_whole_report_synthesis_citation_repair",
                "只修正所列未知引文编号，不增删科学论断，不改章节结构。"
                "只能使用冻结文献目录中的 C 编号；不能确认来源的论断须限定或撤回。只返回 JSON。",
                _writer_visible_payload({
                    "unknown_ids": unknown,
                    "original": synthesis.model_dump(mode="json"),
                    "chapter_citation_catalog": catalog,
                }, catalog),
            )
            return self._normalize_synthesis_citation_metadata(
                repaired, repair_relative, catalog, repair_attempt=repair_attempt + 1,
            )
        if not source_ids:
            self._validate_citations(synthesis.cited_packet_ids)
            return synthesis, source_relative
        retained = [identifier for identifier in synthesis.cited_packet_ids
                    if identifier in known_packets]
        normalized = synthesis.model_copy(update={"cited_packet_ids": retained})
        effective_relative = source_relative.with_name(
            source_relative.stem + "_citation_normalized.json"
        )
        _freeze(self, effective_relative, normalized)
        _freeze(self, effective_relative.with_name(
            effective_relative.stem + "_trace.json"
        ), {
            "frozen_writer_response": str(source_relative),
            "source_sha256": hashlib.sha256((self.repo.root / source_relative).read_bytes()).hexdigest(),
            "removed_source_ids_from_legacy_packet_field": source_ids,
            "source_to_packet_ids": {
                identifier: by_source[identifier].get("packet_ids", []) for identifier in source_ids
            },
            "policy": "METADATA_ONLY; INLINE_CITATIONS_AND_FROZEN_WRITER_RESPONSE_UNCHANGED",
        })
        self._validate_citations(normalized.cited_packet_ids)
        return normalized, effective_relative

    def _citation_aliases(self, packet_ids: list[str]) -> dict[str, str]:
        # Only strong DOI/PMID/arXiv/URL identities are merged mechanically.
        # Fuzzy merging would require a Chair, which this mode does not have.
        return {}

    def _approved_glossary(self, module_drafts: list[tuple[dict, object]]) -> list:
        # The Writer's rolling terms were included in sealed science review.
        # Publishing them needs no new Chair-authored definition.
        return super()._approved_glossary(module_drafts)

    def _repair_reader_facing_leaks(self, markdown: str) -> str:
        """Permit only local presentational edits by the Writer, never a Chair."""
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        relative = self._fast_root() / f"reader_leak_repair_{digest}.json"
        if (self.repo.root / relative).is_file():
            return self._read_json(relative)["repaired_text"]
        lines = markdown.splitlines(keepends=True)
        findings = []
        for index, line in enumerate(lines):
            identifiers = leaked_internal_identifiers(line)
            if not identifiers:
                continue
            proposal = _frozen_or_call(
                self, self._fast_root() / f"reader_leak_line_{digest}_{index:04d}.json",
                ReaderFacingLineRepair, "WRITER", f"fast_reader_leak_repair_{digest[:12]}_{index:04d}",
                "只将这一行的内部会议编号或状态代码改写为普通读者可懂的表述。"
                "保留原有科学论断、数值、限制条件及引文；不能安全替换则原样返回。只返回 JSON。",
                {"line": line.rstrip("\n"), "leaked_identifiers": identifiers},
            )
            candidate = proposal.revised_text + ("\n" if line.endswith("\n") else "")
            applied = (re.findall(r"\[[0-9]+(?:,\s*[0-9]+)*\]", line)
                       == re.findall(r"\[[0-9]+(?:,\s*[0-9]+)*\]", candidate)
                       and is_identifier_only_rewrite(line.rstrip("\n"), proposal.revised_text)
                       and len(leaked_internal_identifiers(candidate)) < len(identifiers))
            if applied:
                lines[index] = candidate
            findings.append({"line": index + 1, "identifiers": identifiers,
                             "applied": applied})
        repaired = "".join(lines)
        _freeze(self, relative, {"source_sha256": digest,
                                 "repaired_text": repaired, "findings": findings,
                                 "remaining_identifiers": leaked_internal_identifiers(repaired)})
        return repaired

    def run(self) -> LiteratureReportExecutionResult:
        final_relative = Path("public/literature_report/execution_result.json")
        if (self.repo.root / final_relative).is_file():
            result = LiteratureReportExecutionResult.model_validate(self._read_json(final_relative))
            self._ensure_visible_links(result)
            return result
        self.engine.status.phase = MeetingPhase.RESEARCH_PLANNING
        self.engine.progress.fast_workflow_stage(1)
        self.engine.progress.status(MeetingPhase.RESEARCH_PLANNING,
                                    "快速文献调研 · 步骤 1/5：学术主笔与人类确认任务书")
        taskbook = self._taskbook()
        outline = FrozenResearchOutline.model_validate({
            **taskbook.outline.model_dump(mode="json"), "review_dispositions": [],
        })
        plans = {}
        self.engine.progress.fast_workflow_stage(2)
        # Do not introduce new planning material into a meeting whose module
        # plans were already frozen before this optional search was added.
        plans_already_started = any(
            (self.repo.root / self._fast_root() / module.module_id
             / "execution_plan.json").is_file()
            for module in outline.modules
        )
        if self.research_desk is not None and not plans_already_started:
            self.engine.progress.status(
                MeetingPhase.RESEARCH_PLANNING,
                "快速文献调研 · 步骤 2/5：主笔做一次跨模块广度检索（最多 8 条）",
            )
            self._breadth_search(2, {
                "approved_taskbook": taskbook.model_dump(mode="json"),
                "step_1_search": self._read_json(
                    self._fast_root() / "planning_search/step_1_breadth.json"
                ) if (self.repo.root / self._fast_root()
                      / "planning_search/step_1_breadth.json").is_file() else None,
            })
        for index, module in enumerate(outline.modules, 1):
            self.engine.progress.fast_planning_step(
                section_id=module.module_id,
                section_index=index,
                section_total=len(outline.modules),
                title=module.title,
                detail="步骤 2/5 · 主笔制定执行单；如有范围异议，待人类逐条确认",
            )
            plan_path = self.repo.root / self._fast_root() / module.module_id / "execution_plan.json"
            task_id = f"fast-plan-{module.module_id}"
            self.engine.progress.task_batch_started(
                [TaskProgressItem(
                    task_id=task_id, participant_id="WRITER",
                    initial_state="completed" if plan_path.is_file() else "pending",
                    initial_detail="已恢复落盘执行单；核对范围决定" if plan_path.is_file() else None,
                    completion_requires_commit=True,
                )],
                title=f"{module.module_id} · 主笔制定模块执行单",
            )
            if not plan_path.is_file():
                self.engine.progress.task_started(task_id, "主笔正在制定执行单")
            try:
                plans[module.module_id] = self._module_plan(taskbook, module)
            except LiteratureWritingPaused:
                self.engine.progress.task_finished(
                    task_id, detail="执行单已落盘；等待人类确认范围",
                )
                raise
            except Exception:
                self.engine.progress.task_finished(
                    task_id, failed=True, detail="执行单未能完成或通过校验",
                )
                raise
            self.engine.progress.task_finished(task_id, detail="执行单与范围决定已冻结")
        self.engine.progress.fast_workflow_stage(3)
        dossiers = self._research_all_modules(taskbook, plans)
        completed = []
        self.engine.progress.fast_workflow_stage(4)
        for index, module in enumerate(outline.modules, 1):
            self.engine.status.phase = MeetingPhase.LITERATURE_MODULE_DRAFTING
            completed.append(self._write_module(outline, module, index, dossiers[module.module_id]))
        self.engine.status.phase = MeetingPhase.LITERATURE_REPORT_SYNTHESIS
        self.engine.progress.fast_workflow_stage(5)
        self.engine.progress.status(MeetingPhase.LITERATURE_REPORT_SYNTHESIS,
                                    "快速文献调研 · 步骤 5/5：学术主笔组装全篇，智库长核查跨模块结论")
        synthesis = self._synthesis(taskbook, completed)
        markdown = self._repair_reader_facing_leaks(
            self._assemble_report_markdown(outline, completed, synthesis, footnotes=[]))
        self.research_desk.literature_bundle.rebuild_download_bundle()
        result = self._publish(markdown, completed, 0)
        _freeze(self, final_relative, result)
        self.engine.status.phase = MeetingPhase.HANDOFF_READY
        self.engine.status.paused_reason = None
        self._ensure_visible_links(result)
        return result
