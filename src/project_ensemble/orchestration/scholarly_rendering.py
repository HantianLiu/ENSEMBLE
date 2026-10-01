from __future__ import annotations

"""Resumable scholarly redrawing of a frozen literature-review report.

The source report is never edited.  Chair is both lead renderer and final
editor; science and citation reviewers only inspect the delta.  Every model
exchange and disposition remains auditable while the reader-facing report is
kept free of internal ENSEMBLE vocabulary.
"""

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.domain import DeliverableType, MeetingPhase, RenderingRole
from project_ensemble.errors import (
    RepresentativeUnavailableError,
    ResearchQualityControlError,
    ResearchRequestRejectedError,
)
from project_ensemble.orchestration.consultations import (
    HumanConsultationIssue,
    HumanConsultationService,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.academic_html import render_academic_review_html
from project_ensemble.orchestration.readability_policy import (
    reader_facing_prose_contract, reader_style_policy,
)
from project_ensemble.orchestration.academic_pdf import render_academic_review_pdf
from project_ensemble.orchestration.final_publication import validate_pdf
from project_ensemble.orchestration.publication_layout import (
    convert_intro_headings,
    heading_numbering_issues,
    markdown_to_latex,
    normalize_publication_headings,
    replace_internal_references,
    sort_numeric_citations,
    split_inline_enumerations,
)
from project_ensemble.research.desk import ResearchDesk
from project_ensemble.research.models import EvidencePacket, ResearchRequest, ResearchStage
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.runtime.structured_output import parse_json_object
from project_ensemble.runtime.model_replacements import (
    current_runtime_for,
    replacement_model_pairs,
)
from project_ensemble.storage.events import HashChainEventLog
from project_ensemble.storage.human_outputs import ensure_visible_link
from project_ensemble.storage.meeting import MeetingRepository


_READER_FACING_IDENTIFIER_RULE = (
    "读者可见的正文、章节标题和表格不得保留或重新引入仅供 ENSEMBLE 内部追踪的代号、"
    "字段名和文件指针，例如 RM-xx、SR-xx、RD-xx、INF-xx、record_id、evidence packet ID。"
    "用自然语言的研究主题、章节名称和规范的文献引文表达其实际含义；"
    "内部代号与原始材料的对应关系留在审计记录或附录，不写进正文。"
    "不得因此删去科学内容、适用范围或真实的证据引文；DOI、PMID、正式数据集编号等"
    "读者确有必要识别的外部标识不属于应删除的内部代号。"
)

_REVIEWER_IDENTIFIER_RULE = (
    "原文中的 RM-xx、SR-xx、RD-xx、record_id 等内部索引未进入读者正文，不构成科学内容遗漏。"
    "不得以保持完整性为理由要求恢复这些代号；只核对其承载的实质主张、限制与文献映射是否保留。"
)

_SCHOLARLY_PRESENTATION_RULE = (
    "章节与小节标题只写语义名称，不手写中文序数、阿拉伯数字或括号编号；"
    "统一编号及目录由出版组装器生成。"
    "body_markdown 只写本块正文，不自建全文章标题或目录；实质分节使用无编号 Markdown 标题。"
    "章节开头若需导读，用 > **章节导读** 引用块；中间块不要重复导读，"
    "不要用独立的 ## 摘要、## 导言或 ## 概述标题。"
    "同段含（1）（2）等并列观点时，改用 Markdown 有序列表的 1.、2. 分条呈现；"
    "仅在确实并列时分条，不把连续论证机械拆碎。"
    "同一方括号中的数字文献引文按编号升序写出；只有连续编号中的每一篇都支持该论断时"
    "才使用 [A-B]，不得因编号相邻虚构支持或遗漏来源。"
    "表格尽量不超过四列；超过四列时保持每个字段及其单位，供出版组装器转换为字段卡。"
)

_SCHOLARLY_QUANTITATIVE_PRESERVATION_RULE = (
    "冻结原文中支撑结论的物理量或定量指标定义属于实质内容：保留公式、符号含义、"
    "单位或约化单位、控制量与观测量的区别、测量及平均口径和适用条件；"
    "不得为了行文流畅把数学定义改成笼统形容词，也不得混同不同研究的同名量。"
    "若原文缺少必要定义，只能如实标明原文未交代，不能凭模型记忆补写新公式或数值。"
)

_SCHOLARLY_QUANTITATIVE_REVIEW_RULE = (
    "若重绘遗漏或混淆原文中关键物理量的公式、符号、单位、测量/平均口径，"
    "使结论无法解释或研究间失去可比性，应视为科学内容偏差而非纯风格问题；"
    "异议须指出原文与当前稿的具体位置，不得要求凭空补造原文没有的公式。"
)


class SourceBlock(BaseModel):
    block_id: str = Field(pattern=r"^SB-[0-9]{3}$")
    heading: str = Field(min_length=1, max_length=300)
    body_markdown: str = Field(min_length=1)


class RenderingAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    section_id: str = Field(pattern=r"^SR-[0-9]{3}$")
    title: str = Field(min_length=1, max_length=300)
    source_block_ids: list[str] = Field(min_length=1, max_length=12)


class RenderingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    assignments: list[RenderingAssignment] = Field(min_length=1, max_length=999)
    rationale: str = Field(min_length=1, max_length=2000)


class RenderingScopeProposal(BaseModel):
    """Chair's proposed block boundary; Human approval is required to freeze it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    selected_block_ids: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=3000)


class RenderingLengthPriority(BaseModel):
    section_id: str = Field(pattern=r"^SR-[0-9]{3}$")
    priority: int = Field(ge=1, le=5)


class RenderingLengthPriorities(BaseModel):
    priorities: list[RenderingLengthPriority] = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=2000)


class RenderedSection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=300)
    body_markdown: str = Field(min_length=1)
    justification: str = Field(min_length=1, max_length=2500)
    removed_irrelevant_material: list[str] = Field(default_factory=list, max_length=20)
    conclusion_changes: list[str] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def conclusions_are_frozen(self) -> "RenderedSection":
        if self.conclusion_changes:
            raise ValueError("scholarly rendering cannot alter the source report's conclusions")
        return self


class ScienceObjection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issue: str = Field(min_length=1, max_length=1200)
    proposed_wording: str | None = Field(default=None, max_length=4000)
    requires_external_verification: bool = False
    verification_claim: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def claim_matches_flag(self) -> "ScienceObjection":
        if self.requires_external_verification != (self.verification_claim is not None):
            raise ValueError("external verification requires exactly one concrete claim")
        return self


class ScienceReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    vote: Literal["YES", "NO"]
    objections: list[ScienceObjection] = Field(default_factory=list, max_length=8)
    style_note: str | None = Field(default=None, max_length=700)

    @model_validator(mode="after")
    def opinion_is_the_vote(self) -> "ScienceReview":
        if self.vote == "YES" and self.objections:
            raise ValueError("YES carries no substantive objection")
        if self.vote == "NO" and not self.objections:
            raise ValueError("NO requires at least one concrete objection")
        return self


class ChairScienceRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    body_markdown: str = Field(min_length=1)
    dispositions: list[str] = Field(default_factory=list, max_length=40)


class ChairScienceIssueAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # These are archival text fields, not terminal previews. Long but otherwise
    # well-formed advice must not stop an already-frozen Human consultation.
    source_excerpt: str = Field(min_length=1)
    redraw_excerpt: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)


def _science_cycle_suffix(cycle: int) -> str:
    if cycle < 1:
        raise ValueError("science review cycle must be positive")
    return "" if cycle == 1 else f"_c{cycle:03d}"


class BinaryVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    vote: Literal["YES", "NO"]


class FinalScienceObjection(ScienceObjection):
    current_excerpt: str = Field(min_length=1)


class FinalScienceResearchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    finished: bool
    claim: str | None = Field(default=None, min_length=1, max_length=1200)

    @model_validator(mode="after")
    def claim_matches_continuation(self) -> "FinalScienceResearchDecision":
        if self.finished == (self.claim is not None):
            raise ValueError("finished requires no claim; continued research requires one claim")
        return self


class FinalScienceBallot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    vote: Literal["YES", "NO"]
    objections: list[FinalScienceObjection] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def no_vote_has_current_objections(self) -> "FinalScienceBallot":
        if self.vote == "YES" and self.objections:
            raise ValueError("YES carries no objection")
        if self.vote == "NO" and not self.objections:
            raise ValueError("NO requires at least one objection to the current draft")
        return self


class CitationIssue(BaseModel):
    # Exact whitespace is part of a mechanically applicable INSERT/REMOVE unit.
    model_config = ConfigDict(extra="forbid")
    operation: Literal["INSERT", "REMOVE"]
    target_text: str = Field(min_length=1, max_length=3000)
    citation_text: str | None = None
    source_packet_id: str | None = Field(default=None, pattern=r"^RP-[A-Z0-9]+$")
    rationale: str = Field(min_length=1, max_length=900)

    @model_validator(mode="after")
    def insertion_has_content(self) -> "CitationIssue":
        if not self.target_text.strip() or not self.rationale.strip():
            raise ValueError("citation target and rationale cannot be whitespace-only")
        if self.operation == "INSERT" and not self.citation_text:
            raise ValueError("INSERT requires exact citation text for mechanical application")
        if self.operation == "INSERT" and not self.citation_text.strip():
            raise ValueError("INSERT citation text cannot be whitespace-only")
        if self.operation == "REMOVE" and self.citation_text is not None:
            raise ValueError("REMOVE cannot carry insertion text")
        return self


class CitationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issues: list[CitationIssue] = Field(default_factory=list, max_length=20)
    # Numbering/heading findings are editorial QC, never citation amendments.
    structure_issues: list[str] = Field(default_factory=list, max_length=12)


class CitationAmendment(CitationIssue):
    amendment_id: str = Field(pattern=r"^CA-[0-9]{3}$")


class CitationDocket(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    amendments: list[CitationAmendment] = Field(default_factory=list, max_length=100)


class CitationVoteItem(BaseModel):
    amendment_id: str
    vote: Literal["YES", "NO"]


class CitationVoteSet(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    votes: list[CitationVoteItem] = Field(default_factory=list, max_length=100)


class CitationDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    amendment_id: str = Field(pattern=r"^CA-[0-9]{3}$")
    apply: bool
    rationale: str = Field(min_length=1, max_length=500)


class ChairCitationApplication(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decisions: list[CitationDisposition] = Field(min_length=1, max_length=100)


class CitationAnchorRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amendments: list[CitationAmendment] = Field(min_length=1, max_length=100)


class CitationTargetConflict(ValueError):
    def __init__(self, conflicts: list[dict]):
        super().__init__("one or more citation amendment targets are not unique")
        self.conflicts = conflicts


class ScholarlyRenderingPaused(Exception):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class DisqualifiedScienceBallot(Exception):
    """A final voter insisted on an externally unsupported correction."""


class ScholarlyRenderingResult(BaseModel):
    meeting_id: str
    status: Literal["PAUSED", "HANDOFF_READY"]
    section_count: int
    completed_section_count: int
    source_markdown_path: str
    final_markdown_path: str | None = None
    final_html_path: str | None = None
    final_latex_path: str | None = None
    final_pdf_path: str | None = None
    appendix_path: str | None = None
    next_phase: MeetingPhase
    paused_reason: str | None = None


class ScholarlyRenderingRunner:
    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        research_desk: ResearchDesk,
        max_output_tokens: int | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.research_desk = research_desk
        self.max_output_tokens = max_output_tokens
        self.manifest = json.loads(
            repo.docs.read_text("identity_private/meeting_manifest.json")
        )
        self.registry = json.loads(
            repo.docs.read_text("identity_private/representative_registry.json")
        )
        self.science = [
            record
            for record in self.registry
            if record["runtime"].get("rendering_role") == RenderingRole.SCIENCE_BOOKKEEPER.value
        ]
        self.citations = [
            record
            for record in self.registry
            if record["runtime"].get("rendering_role") == RenderingRole.CITATION_BOOKKEEPER.value
        ]
        self.access_log = HashChainEventLog(
            repo.root / "governance_private/representative_file_access.jsonl"
        )

    def run(self) -> ScholarlyRenderingResult:
        if self.manifest.get("deliverable_type") != DeliverableType.SCHOLARLY_RENDERING.value:
            raise ValueError("scholarly renderer requires a scholarly_rendering meeting")
        frozen_result = self.repo.root / "public/scholarly_rendering/execution_result.json"
        if frozen_result.exists():
            return ScholarlyRenderingResult.model_validate_json(
                frozen_result.read_text(encoding="utf-8")
            )
        source_path = self._source_path()
        source_text = source_path.read_text(encoding="utf-8")
        blocks = self._source_blocks(source_text)
        self._show_workload(f"待重绘原文块共 {len(blocks)} 块；正在规划重绘章节")
        try:
            plan = self._plan(blocks)
        except ScholarlyRenderingPaused as exc:
            return self._paused_result(
                plan=None, completed=[], source_path=source_path, reason_code=exc.reason_code,
            )
        length_budget = self._ensure_length_budget(plan, blocks)
        if length_budget is not None:
            self.engine.progress.info(
                "重绘建议正文长度 "
                f"{length_budget['target_characters']:,} 字符；"
                "这是软性参考，不设硬性长度门槛，也不保证最终达到；"
                "参考文献和独立附录不计"
            )
        completed: list[tuple[RenderingAssignment, str]] = []
        completed_block_count = 0
        restored_sections = 0
        for index, assignment in enumerate(plan.assignments, start=1):
            current_block_count = len(assignment.source_block_ids)
            final_path = (
                self.repo.root / "public/scholarly_rendering/sections"
                / assignment.section_id / "final.md"
            )
            if final_path.is_file():
                if (self.manifest.get("rendering_scope_description")
                        and not self._scope_selection_intersects(assignment.source_block_ids)):
                    expected_original = self._assignment_text(assignment, blocks)
                    if final_path.read_text(encoding="utf-8") != expected_original:
                        raise ValueError(
                            f"preserved source was changed after Human scope approval: {assignment.section_id}"
                        )
                completed.append((assignment, final_path.read_text(encoding="utf-8")))
                completed_block_count += current_block_count
                restored_sections += 1
                continue
            if restored_sections:
                self.engine.progress.info(
                    f"已恢复 {restored_sections}/{len(plan.assignments)} 个完成的重绘章节；"
                    f"从 {assignment.section_id} 继续"
                )
                restored_sections = 0
            self._show_workload(
                f"原文块 {completed_block_count + 1}–"
                f"{completed_block_count + current_block_count}/{len(blocks)}；"
                f"重绘章节 {index}/{len(plan.assignments)}"
            )
            self._rendering_scope = (assignment, index, len(plan.assignments))
            original = self._assignment_text(assignment, blocks)
            if self.manifest.get("rendering_scope_description") and not self._scope_selection_intersects(
                assignment.source_block_ids
            ):
                relative = Path("public/scholarly_rendering/sections") / assignment.section_id
                provenance = json.dumps({
                    "source_block_ids": assignment.source_block_ids,
                    "source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                    "scope_approval_path": "public/scholarly_rendering/approved_scope.json",
                    "action": "PRESERVE_SOURCE",
                }, indent=2, ensure_ascii=False)
                for artifact, content in (
                    (relative / "preserved_source.json", provenance),
                    (relative / "final.md", original),
                ):
                    path = self.repo.root / artifact
                    if path.is_file():
                        if path.read_text(encoding="utf-8") != content:
                            raise ValueError(f"preserved source artifact conflicts with frozen text: {artifact}")
                    else:
                        self.repo.docs.write_once(artifact, content)
                completed.append((assignment, original))
                completed_block_count += current_block_count
                self.engine.progress.info(
                    f"{assignment.section_id} · 不在已批准重绘范围内；保留冻结原文"
                )
                continue
            try:
                outcome = self._run_section(assignment, original, source_path)
            except ScholarlyRenderingPaused as exc:
                return self._paused_result(
                    plan=plan,
                    completed=completed,
                    source_path=source_path,
                    reason_code=exc.reason_code,
                )
            if outcome is None:
                return self._paused_result(
                    plan=plan,
                    completed=completed,
                    source_path=source_path,
                    reason_code="SCHOLARLY_RENDERING_HUMAN_RESOLUTION_REQUIRED",
                )
            completed.append((assignment, outcome))
            completed_block_count += current_block_count
            self._show_workload(
                f"已完成原文块 {completed_block_count}/{len(blocks)}；"
                f"已完成重绘章节 {index}/{len(plan.assignments)}"
            )
        if restored_sections:
            self.engine.progress.info(
                f"已恢复 {restored_sections}/{len(plan.assignments)} 个完成的重绘章节"
            )
        try:
            return self._publish(source_path, plan, completed)
        except ScholarlyRenderingPaused as exc:
            return self._paused_result(
                plan=plan,
                completed=completed,
                source_path=source_path,
                reason_code=exc.reason_code,
            )

    def _show_workload(self, message: str) -> None:
        setter = getattr(self.engine.progress, "set_workload", None)
        if callable(setter):
            setter(message)

    def _show_rendering_step(self, stage: str, detail: str) -> None:
        scope = getattr(self, "_rendering_scope", None)
        if scope is None:
            return
        setter = getattr(self.engine.progress, "rendering_step", None)
        if callable(setter):
            assignment, index, total = scope
            setter(
                section_id=assignment.section_id,
                section_index=index,
                section_total=total,
                title=assignment.title,
                stage=stage,
                detail=detail,
            )

    def _paused_result(
        self,
        *,
        plan: RenderingPlan | None,
        completed: list[tuple[RenderingAssignment, str]],
        source_path: Path,
        reason_code: str,
    ) -> ScholarlyRenderingResult:
        self.engine.status.phase = MeetingPhase.PAUSED
        self.engine.status.paused_reason = reason_code
        self.engine.progress.paused(reason_code, "HUMAN")
        return ScholarlyRenderingResult(
            meeting_id=self.repo.meeting_id,
            status="PAUSED",
            section_count=len(plan.assignments) if plan is not None else 0,
            completed_section_count=len(completed),
            source_markdown_path=str(source_path.relative_to(self.repo.root)),
            next_phase=MeetingPhase.PAUSED,
            paused_reason=reason_code,
        )

    def _source_path(self) -> Path:
        candidates = (
            "public/continuation/source_artifacts/final/scholarly_rendering/scholarly_review.md",
            "public/continuation/source_artifacts/final/literature_review_report.md",
            "public/continuation/source_artifacts/final/final_report_v2.md",
            "public/continuation/source_artifacts/final/final_report.md",
        )
        for relative in candidates:
            path = self.repo.root / relative
            if path.is_file():
                return path
        raise ValueError("the rendering meeting has no inherited Markdown report")

    @staticmethod
    def _source_blocks(text: str, *, max_chars: int = 16_000) -> list[SourceBlock]:
        lines = text.splitlines()
        raw: list[tuple[str, list[str]]] = []
        heading = "Document opening"
        body: list[str] = []
        in_fence = False
        for line in lines:
            if re.match(r"^\s*(```|~~~)", line):
                in_fence = not in_fence
                body.append(line)
                continue
            heading_match = None if in_fence else re.match(r"^#{1,6}\s+(.+)$", line)
            if heading_match and body:
                raw.append((heading, body))
                heading, body = heading_match.group(1).strip(), [line]
            else:
                if heading_match:
                    heading = heading_match.group(1).strip()
                body.append(line)
        if body:
            raw.append((heading, body))
        chunks: list[tuple[str, str]] = []
        for title, section_lines in raw:
            section = "\n".join(section_lines).strip()
            if len(section) <= max_chars:
                chunks.append((title, section))
                continue
            paragraphs = re.split(r"\n\s*\n", section)
            current = ""
            part = 1
            for paragraph in paragraphs:
                candidate = paragraph if not current else current + "\n\n" + paragraph
                if current and len(candidate) > max_chars:
                    chunks.append((f"{title} (part {part})", current))
                    current, part = paragraph, part + 1
                else:
                    current = candidate
            if current:
                chunks.append((f"{title} (part {part})", current))
        return [
            SourceBlock(block_id=f"SB-{index:03d}", heading=title, body_markdown=body)
            for index, (title, body) in enumerate(chunks, start=1)
        ]

    def _plan(self, blocks: list[SourceBlock]) -> RenderingPlan:
        relative = Path("public/scholarly_rendering/rendering_plan.json")
        path = self.repo.root / relative
        if path.exists():
            plan = RenderingPlan.model_validate_json(path.read_text(encoding="utf-8"))
            self._validate_plan(plan, blocks)
            return plan
        if self.manifest.get("rendering_scope_description"):
            proposal = self._approved_scope(blocks)
            selected = set(proposal.selected_block_ids)
            assignments: list[RenderingAssignment] = []
            # Preserve exact source order.  Split at each action boundary; a
            # section can never mix approved redraw text with pass-through text.
            for block in blocks:
                action = block.block_id in selected
                if (assignments and len(assignments[-1].source_block_ids) < 12
                        and (assignments[-1].source_block_ids[0] in selected) == action):
                    assignments[-1].source_block_ids.append(block.block_id)
                else:
                    assignments.append(RenderingAssignment(
                        section_id=f"SR-{len(assignments)+1:03d}",
                        title=block.heading[:300], source_block_ids=[block.block_id],
                    ))
            plan = RenderingPlan(
                assignments=assignments,
                rationale="人类批准局部范围后按原文顺序机械分块；保留块不进入重绘与核校。",
            )
            self._validate_plan(plan, blocks)
            self.repo.docs.write_once(relative, plan.model_dump_json(indent=2))
            return plan
        self.engine.status.phase = MeetingPhase.SCHOLARLY_RENDERING_PLAN
        self.engine.progress.status(
            MeetingPhase.SCHOLARLY_RENDERING_PLAN,
            f"Chair 正在规划 {len(blocks)} 个有界原文块的学术化重绘；不改动冻结原稿",
        )
        inventory = [
            {"block_id": block.block_id, "heading": block.heading, "characters": len(block.body_markdown)}
            for block in blocks
        ]
        plan = self._invoke(
            "CHAIR",
            stage="scholarly_rendering_plan",
            schema=RenderingPlan,
            system=(
                "在主席权限内规划学术化重绘。将有序原文块组合成范围有限的学术章节；"
                "每块恰好覆盖一次，保持原顺序。本阶段不推断或改写实质内容。"
                "章节标题使用读者能理解的研究主题，不要把 RM-xx、SR-xx 或 record_id 当作标题。"
            ),
            user={"source_inventory": inventory, "rendering_configuration": self._public_config()},
        )
        self._validate_plan(plan, blocks)
        self.repo.docs.write_once(relative, plan.model_dump_json(indent=2))
        return plan

    def _scope_selection_intersects(self, block_ids: list[str]) -> bool:
        path = self.repo.root / "public/scholarly_rendering/approved_scope.json"
        if not path.is_file():
            raise ValueError("partial rendering has no approved Human scope")
        selected = set(json.loads(path.read_text(encoding="utf-8"))["proposal"]["selected_block_ids"])
        actions = {block_id in selected for block_id in block_ids}
        if len(actions) != 1:
            raise ValueError("rendering assignment crosses an approved scope boundary")
        return True in actions

    def _approved_scope(self, blocks: list[SourceBlock]) -> RenderingScopeProposal:
        approved_relative = Path("public/scholarly_rendering/approved_scope.json")
        approved_path = self.repo.root / approved_relative
        expected = {block.block_id for block in blocks}
        if approved_path.is_file():
            data = json.loads(approved_path.read_text(encoding="utf-8"))
            proposal = RenderingScopeProposal.model_validate(data["proposal"])
            if data["source_sha256"] != hashlib.sha256(
                "\n\n".join(block.body_markdown for block in blocks).encode("utf-8")
            ).hexdigest() or not set(proposal.selected_block_ids) <= expected:
                raise ValueError("approved rendering scope no longer matches inherited source")
            return proposal
        service = HumanConsultationService(self.repo)
        inventory = [{
            "block_id": item.block_id, "heading": item.heading,
            "characters": len(item.body_markdown),
            "opening": item.body_markdown[:500],
        } for item in blocks]
        for cycle in range(1, 101):
            proposal_relative = Path(
                f"public/scholarly_rendering/scope_candidates/candidate_{cycle:03d}.json"
            )
            proposal_path = self.repo.root / proposal_relative
            if proposal_path.is_file():
                proposal = RenderingScopeProposal.model_validate_json(
                    proposal_path.read_text(encoding="utf-8")
                )
            else:
                prior_issue = f"HC-SR-SCOPE-{cycle-1:03d}" if cycle > 1 else None
                prior_resolution = service.resolution(prior_issue) if prior_issue else None
                proposal = self._invoke(
                    "CHAIR", stage=f"scholarly_rendering_scope_{cycle:03d}",
                    schema=RenderingScopeProposal,
                    system=(
                        "根据 Human 自然语言要求，划定需要重绘的原文块。只返回确实需要重绘的 block_id，"
                        "其余块保持原文且不进入审阅。不要扩大 Human 指定范围。"
                        "如果边界有歧义，在 rationale 中明确说明，交 Human 裁定。"
                    ),
                    user={
                        "human_scope_description": self.manifest["rendering_scope_description"],
                        "source_inventory": inventory,
                        "previous_human_feedback": prior_resolution.rationale if prior_resolution else None,
                    },
                )
                if (len(proposal.selected_block_ids) != len(set(proposal.selected_block_ids))
                        or not set(proposal.selected_block_ids) <= expected):
                    raise ValueError("Chair scope proposal has duplicate or unknown source blocks")
                self.repo.docs.write_once(proposal_relative, proposal.model_dump_json(indent=2))
            if (len(proposal.selected_block_ids) != len(set(proposal.selected_block_ids))
                    or not set(proposal.selected_block_ids) <= expected):
                raise ValueError("frozen Chair scope proposal has duplicate or unknown source blocks")
            issue_id = f"HC-SR-SCOPE-{cycle:03d}"
            selected = set(proposal.selected_block_ids)
            issue = HumanConsultationIssue(
                issue_id=issue_id, meeting_id=self.repo.meeting_id,
                reason_code="SCHOLARLY_RENDERING_SCOPE_APPROVAL_REQUIRED",
                stage="SCHOLARLY_RENDERING_SCOPE",
                question=("请确认主席据自然语言要求划定的局部重绘范围。"
                          "批准后仅选中块进入原有科学和引文核校；其余块原文保留。"
                          "如边界不准确，请退回并说明要增删的范围。"),
                options=["APPROVE_RENDERING_SCOPE", "REVISE_RENDERING_SCOPE"],
                affected_items=proposal.selected_block_ids,
                context={
                    "cycle": cycle, "proposal_path": str(proposal_relative),
                    "human_description": self.manifest["rendering_scope_description"],
                    "proposal_rationale": proposal.rationale,
                    "selected_blocks": [
                        {"id": item.block_id, "heading": item.heading}
                        for item in blocks if item.block_id in selected
                    ],
                    "preserved_block_count": len(blocks) - len(selected),
                    "total_block_count": len(blocks),
                },
            )
            service.open_issue(issue)
            resolution = service.resolution(issue_id)
            if resolution is None:
                raise ScholarlyRenderingPaused("SCHOLARLY_RENDERING_SCOPE_APPROVAL_REQUIRED")
            if resolution.decision == "APPROVE_RENDERING_SCOPE":
                self.repo.docs.write_once(approved_relative, json.dumps({
                    "proposal": proposal.model_dump(mode="json"),
                    "human_issue_id": issue_id,
                    "source_sha256": hashlib.sha256(
                        "\n\n".join(block.body_markdown for block in blocks).encode("utf-8")
                    ).hexdigest(),
                }, indent=2, ensure_ascii=False))
                return proposal
        raise ValueError("too many rejected rendering scope proposals; Human intervention required")

    @staticmethod
    def _validate_plan(plan: RenderingPlan, blocks: list[SourceBlock]) -> None:
        flattened = [item for assignment in plan.assignments for item in assignment.source_block_ids]
        expected = [block.block_id for block in blocks]
        if flattened != expected:
            raise ValueError("Chair rendering plan must cover every source block exactly once in source order")

    @staticmethod
    def _count_body_characters(markdown: str) -> int:
        """Stable length metric: Unicode characters other than whitespace."""

        return len(re.sub(r"\s+", "", markdown))

    @staticmethod
    def _is_supplementary_assignment(assignment: RenderingAssignment) -> bool:
        return bool(re.search(
            r"参考文献|文献追溯|附录|bibliography|references|appendix",
            assignment.title, re.I,
        ))

    def _ensure_length_budget(
        self, plan: RenderingPlan, blocks: list[SourceBlock]
    ) -> dict | None:
        target = self.manifest.get("rendering_target_body_characters")
        if target is None:  # Meetings created before this policy retain their frozen plan.
            return None
        relative = Path("public/scholarly_rendering/body_length_budget.json")
        path = self.repo.root / relative
        if path.exists():
            frozen = json.loads(path.read_text(encoding="utf-8"))
            if (frozen.get("target_characters") != target or
                    [item["section_id"] for item in frozen.get("sections", [])] !=
                    [item.section_id for item in plan.assignments]):
                raise ValueError("frozen scholarly rendering length budget conflicts with the plan")
            return frozen
        by_id = {block.block_id: block for block in blocks}
        priority_relative = Path("public/scholarly_rendering/body_length_priorities.json")
        priority_path = self.repo.root / priority_relative
        if priority_path.exists():
            priorities = RenderingLengthPriorities.model_validate_json(
                priority_path.read_text(encoding="utf-8")
            )
        else:
            priorities = self._invoke(
                "CHAIR",
                stage="scholarly_rendering_length_priorities",
                schema=RenderingLengthPriorities,
                system=(
                    "仅为已冻结的重绘计划分配正文内容保留优先级，不改动拆块、顺序或科学结论。"
                    "逐项返回 1–5 的 priority：1 表示可大幅合并重复说明，"
                    "5 表示信息密度高、需要充分篇幅。参考文献与独立附录不占正文目标。"
                    "这只是长度指导，不得删去证据条件、不确定性、必要引文或结论。"
                ),
                user={
                    "body_target_characters": target,
                    "policy": "ADVISORY_ONLY; NO_HARD_LIMIT; FINAL_LENGTH_NOT_GUARANTEED",
                    "sections": [
                        {
                            "section_id": item.section_id,
                            "title": item.title,
                            "source_characters": self._count_body_characters(
                                "\n".join(by_id[block_id].body_markdown for block_id in item.source_block_ids)
                            ),
                            "counts_toward_body_target": not self._is_supplementary_assignment(item),
                        }
                        for item in plan.assignments
                    ],
                },
            )
            if [item.section_id for item in priorities.priorities] != [
                item.section_id for item in plan.assignments
            ]:
                raise ValueError("Chair length priorities must cover the frozen plan in order")
            self.repo.docs.write_once(priority_relative, priorities.model_dump_json(indent=2))
        priority_by_id = {item.section_id: item.priority for item in priorities.priorities}
        if list(priority_by_id) != [item.section_id for item in plan.assignments]:
            raise ValueError("frozen Chair length priorities conflict with the rendering plan")
        weighted: list[tuple[int, float]] = []
        source_counts: list[int] = []
        for index, item in enumerate(plan.assignments):
            count = self._count_body_characters(
                "\n".join(by_id[block_id].body_markdown for block_id in item.source_block_ids)
            )
            source_counts.append(count)
            if not self._is_supplementary_assignment(item):
                weighted.append((index, max(count, 1) * priority_by_id[item.section_id]))
        total_weight = sum(weight for _, weight in weighted)
        allocations = [0] * len(plan.assignments)
        if weighted:
            # Section allocations are hints only. If the requested target is
            # smaller than the number of sections, prefer one character per
            # section as a bookkeeping floor rather than rejecting the run.
            allocation_total = max(target, len(weighted))
            fractions: list[tuple[float, int]] = []
            distributable = allocation_total - len(weighted)
            for index, weight in weighted:
                raw = distributable * weight / total_weight
                allocations[index] = 1 + math.floor(raw)
                fractions.append((raw - math.floor(raw), index))
            remaining = allocation_total - sum(allocations)
            for _, index in sorted(fractions, key=lambda pair: (-pair[0], pair[1]))[:remaining]:
                allocations[index] += 1
        budget = {
            "target_characters": target,
            "policy": "ADVISORY_ONLY; NO_HARD_LIMIT; FINAL_LENGTH_NOT_GUARANTEED",
            "measurement": "Unicode non-whitespace characters in approved section Markdown; standalone reference/appendix sections excluded",
            "sections": [
                {
                    "section_id": item.section_id,
                    "title": item.title,
                    "source_characters": source_counts[index],
                    "length_priority": priority_by_id[item.section_id],
                    "target_characters": allocations[index],
                    "counts_toward_body_target": not self._is_supplementary_assignment(item),
                }
                for index, item in enumerate(plan.assignments)
            ],
        }
        self.repo.docs.write_once(relative, json.dumps(budget, indent=2, ensure_ascii=False))
        return budget

    def _run_section(
        self, assignment: RenderingAssignment, original: str, source_path: Path
    ) -> str | None:
        root = Path("public/scholarly_rendering/sections") / assignment.section_id
        final_path = self.repo.root / root / "final.md"
        if final_path.is_file():
            return final_path.read_text(encoding="utf-8")
        draft_path = self.repo.root / root / "chair_redraw.json"
        self._show_rendering_step("draft", "主席重绘正文")
        self.engine.status.phase = MeetingPhase.SCHOLARLY_RENDERING_DRAFT
        self.engine.progress.status(
            MeetingPhase.SCHOLARLY_RENDERING_DRAFT,
            f"{assignment.section_id} · Chair 重绘正文并说明删改范围",
        )
        if draft_path.exists():
            rendered = RenderedSection.model_validate_json(draft_path.read_text(encoding="utf-8"))
        else:
            rendered = self._invoke(
                "CHAIR",
                stage=f"scholarly_redraw_{assignment.section_id}",
                schema=RenderedSection,
                system=self._chair_render_system(),
                user={
                    "assignment": assignment.model_dump(mode="json"),
                    "rendering_configuration": self._public_config(),
                    "writing_context": self._rendering_writing_context(
                        assignment, source_path, original
                    ),
                    "source_markdown": original,
                },
            )
            self.repo.docs.write_once(root / "chair_redraw.json", rendered.model_dump_json(indent=2))
        current = rendered.body_markdown
        eligible = {record["representative_id"] for record in self.science}
        cycle = 1
        while True:
            all_reviews: list[dict] = []
            supported_all: list[dict] = []
            for round_number in (1, 2):
                reviews = self._science_round(
                    assignment, round_number, original, current, source_path,
                    cycle=cycle,
                )
                all_reviews.extend(reviews)
                supported, disqualified = self._verify_science_corrections(
                    assignment, round_number, reviews, cycle=cycle,
                )
                supported_all.extend(supported)
                eligible -= disqualified
                objections = [
                    item for review in reviews for item in review["review"]["objections"]
                ]
                if objections:
                    current = self._chair_science_revision(
                        assignment, round_number, original, current, reviews,
                        supported, cycle=cycle,
                    )
            if self.science and not eligible:
                eligible = self._recover_empty_science_panel(assignment, cycle=cycle)
            ballots = self._science_ballot(
                assignment, original, current, eligible, source_path, cycle=cycle,
                prior_supported=supported_all,
            )
            supported_all.extend(self._final_ballot_verified_corrections(assignment, ballots))
            yes = sum(item["vote"] == "YES" for item in ballots)
            if yes >= math.floor(len(ballots) / 2) + 1:
                break
            revised = self._human_science_resolution(
                assignment, original, current, all_reviews, ballots,
                cycle=cycle,
                verified_corrections=supported_all,
            )
            if revised is None:
                return None
            current = revised
            if cycle == 1 and HumanConsultationService(self.repo).resolution(
                f"HC-SR-{assignment.section_id}"
            ) is not None:
                # A pre-upgrade Human ruling was a final disposition, not a
                # request to start another science-review cycle.
                break
            cycle += 1
        for round_number in (1, 2):
            current = self._citation_round(
                assignment, round_number, original, current, source_path
            )
        final_relative = root / "final.md"
        final_path = self.repo.root / final_relative
        if not final_path.exists():
            self.repo.docs.write_once(final_relative, current.strip() + "\n")
        return final_path.read_text(encoding="utf-8")

    def _recover_empty_science_panel(
        self, assignment: RenderingAssignment, *, cycle: int
    ) -> set[str]:
        """Ask Human before overriding an all-excluded panel; retain frozen checks."""

        issue = HumanConsultationIssue(
            issue_id=f"HC-SR-{assignment.section_id}-EMPTY-SCIENCE-C{cycle:03d}",
            meeting_id=self.repo.meeting_id,
            reason_code="SCHOLARLY_SCIENCE_BALLOT_NO_ELIGIBLE_VOTERS",
            stage="SCHOLARLY_SCIENCE_REVIEW",
            question=(
                "本节科学核校员均因 Research Desk 未支持其附带的外部核验请求而失去最终表决资格。"
                "请核对已冻结的审阅意见和主席修订稿：若意见实质上只要求恢复冻结原文、"
                "且未获支持的新科学结论并未写入当前稿，可授权全员仅就当前稿的原文忠实性重新表决；"
                "不得把未获支持的外部结论带入最终票。原始核验及失格记录永久保留。"
                "否则保持暂停，由人类另行处理。"
                f"\n核验记录：governance_private/scholarly_rendering/research_verifications/"
                f"{assignment.section_id}/round_2{_science_cycle_suffix(cycle)}.json"
                f"\n主席修订：public/scholarly_rendering/sections/"
                f"{assignment.section_id}/science_revision_2{_science_cycle_suffix(cycle)}.json"
            ),
            options=["RESTORE_SOURCE_FIDELITY_BALLOT", "KEEP_PAUSED"],
            affected_items=[assignment.section_id],
        )
        resolution = self._open_integrity_consultation(issue)
        if resolution.decision != "RESTORE_SOURCE_FIDELITY_BALLOT":
            raise ScholarlyRenderingPaused("SCHOLARLY_SCIENCE_BALLOT_NO_ELIGIBLE_VOTERS")
        return {record["representative_id"] for record in self.science}

    def _science_round(
        self,
        assignment: RenderingAssignment,
        round_number: int,
        original: str,
        current: str,
        source_path: Path,
        *,
        cycle: int = 1,
    ) -> list[dict]:
        self._show_rendering_step(
            "science", f"第 {cycle} 次修订 · 第 {round_number}/2 轮审阅"
        )
        self.engine.status.phase = MeetingPhase.SCHOLARLY_SCIENCE_REVIEW
        self.engine.progress.status(
            MeetingPhase.SCHOLARLY_SCIENCE_REVIEW,
            f"{assignment.section_id} · 第 {cycle} 次修订后科学核校第 {round_number}/2 轮；按 Human 冻结顺序审阅",
        )
        ordered = self._ordered_science()
        prior: list[dict] = []
        private_root = (
            Path("governance_private/scholarly_rendering/science_reviews")
            / assignment.section_id
            / f"round_{round_number}{_science_cycle_suffix(cycle)}"
        )
        for record in ordered:
            rid = record["representative_id"]
            path = self.repo.root / private_root / f"{rid}.json"
            if path.exists():
                # Validate the frozen vote without serializing it back through
                # today's schema. Newly added optional fields would otherwise
                # appear as null and change the verification input digest.
                review_payload = json.loads(path.read_text(encoding="utf-8"))
                ScienceReview.model_validate(review_payload)
            else:
                self._record_read(rid, source_path, f"science_review_round_{round_number}{_science_cycle_suffix(cycle)}")
                review = self._invoke(
                    rid,
                    stage=f"scholarly_science_review_{assignment.section_id}_r{round_number}{_science_cycle_suffix(cycle)}",
                    schema=ScienceReview,
                    system=(
                        "审查科学事实，不担任正文作者。比较冻结原文、重绘稿及此前审阅者的完整意见。"
                        "只有科学主张、适用范围、不确定性及结论均得到保留时才投 YES；投 NO 须给出有限范围的异议。"
                        "不得仅因风格或版式变化提出反对；但结构或措辞变化若改变科学含义、论证关系、"
                        "适用范围或证据对应，应指出当前稿中的具体位置和实质影响，可以投 NO。"
                        "若建议超出原文的科学纠正，标记为需要 Research Desk 核验。"
                        + (
                            "若仅要求删除重绘稿新增的断言或恢复冻结原文，不属于新增科学纠正；"
                            "即使附带『若坚持保留该断言则应查文献』的条件性提醒，也不要标记需要外部核验。"
                            if getattr(self, "manifest", {}).get("rendering_target_body_characters") is not None else ""
                        )
                        + "纯风格意见可写入可选 style_note；YES 票也可附此字段。"
                        "style_note 不影响票数，不构成科学异议，也不要求下轮写作者采纳或解释。"
                        + _SCHOLARLY_QUANTITATIVE_REVIEW_RULE
                        + _REVIEWER_IDENTIFIER_RULE
                    ),
                    user={
                        "section": assignment.model_dump(mode="json"),
                        "source_markdown": original,
                        "current_redraw": current,
                        "prior_reviewers": [
                            {
                                **{k: v for k, v in item.items() if k not in {"reviewer_id", "review"}},
                                "review": {k: v for k, v in item["review"].items() if k != "style_note"},
                            }
                            for item in prior
                        ],
                    },
                )
                self.repo.docs.write_once(private_root / f"{rid}.json", review.model_dump_json(indent=2))
                review_payload = review.model_dump(mode="json")
            public_review = {
                "review_number": len(prior) + 1,
                "review": review_payload,
            }
            prior.append({"reviewer_id": rid, **public_review})
        public_relative = (
            Path("public/scholarly_rendering/sections")
            / assignment.section_id
            / f"science_round_{round_number}{_science_cycle_suffix(cycle)}.json"
        )
        if not (self.repo.root / public_relative).exists():
            self.repo.docs.write_once(
                public_relative,
                json.dumps(
                    {"reviews": [{k: v for k, v in item.items() if k != "reviewer_id"} for item in prior]},
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        return prior

    def _verify_science_corrections(
        self, assignment: RenderingAssignment, round_number: int, reviews: list[dict],
        *, cycle: int = 1,
    ) -> tuple[list[dict], set[str]]:
        self._show_rendering_step(
            "science", f"第 {cycle} 次修订 · 第 {round_number}/2 轮证据核验"
        )
        relative = (
            Path("governance_private/scholarly_rendering/research_verifications")
            / assignment.section_id
            / f"round_{round_number}{_science_cycle_suffix(cycle)}.json"
        )
        path = self.repo.root / relative
        input_sha256 = hashlib.sha256(
            json.dumps(reviews, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        request_suffix = ""
        if path.exists():
            frozen = json.loads(path.read_text(encoding="utf-8"))
            if frozen.get("input_sha256") != input_sha256:
                issue_id = f"HC-SR-{assignment.section_id}-VERIFY-R{round_number}{_science_cycle_suffix(cycle).upper()}"
                resolution = self._open_integrity_consultation(
                    HumanConsultationIssue(
                        issue_id=issue_id,
                        meeting_id=self.repo.meeting_id,
                        reason_code="SCHOLARLY_RENDERING_INTEGRITY_CONFLICT",
                        stage="SCHOLARLY_SCIENCE_REVIEW",
                        question=(
                            "恢复时发现当前审阅内容与已经冻结的科学核验输入不一致。"
                            "请选择沿用冻结结果，或对当前审阅内容重新核验并另存为不可变补充记录。\n\n"
                            f"冻结输入 SHA-256: {frozen.get('input_sha256')}\n"
                            f"当前输入 SHA-256: {input_sha256}"
                        ),
                        options=[
                            "USE_FROZEN_VERIFICATION",
                            "USE_CURRENT_REVIEWS_AND_REVERIFY",
                        ],
                        affected_items=[assignment.section_id],
                    )
                )
                if resolution.decision == "USE_FROZEN_VERIFICATION":
                    return list(frozen["supported"]), set(
                        frozen["disqualified_reviewer_ids"]
                    )
                relative = relative.with_name(
                    f"round_{round_number}{_science_cycle_suffix(cycle)}.reverified_{input_sha256[:12]}.json"
                )
                path = self.repo.root / relative
                request_suffix = f"-{input_sha256[:12]}"
                if path.exists():
                    reverified = json.loads(path.read_text(encoding="utf-8"))
                    if reverified.get("input_sha256") != input_sha256:
                        raise ValueError("reverified science record has an invalid input digest")
                    return list(reverified["supported"]), set(
                        reverified["disqualified_reviewer_ids"]
                    )
            else:
                return list(frozen["supported"]), set(frozen["disqualified_reviewer_ids"])

        supported: list[dict] = []
        disqualified: set[str] = set()
        checks: list[dict] = []
        for review in reviews:
            rid = review["reviewer_id"]
            for index, objection in enumerate(review["review"]["objections"], start=1):
                claim = objection.get("verification_claim")
                if not claim:
                    continue
                request_id = (
                    f"{assignment.section_id}-R{round_number}{_science_cycle_suffix(cycle).upper()}-{rid}-{index:02d}"
                    f"{request_suffix}"
                )
                try:
                    packet = self._research_or_restore_science_correction(
                        request_id=request_id,
                        requester_id=rid,
                        claim=claim,
                    )
                except (
                    ResearchQualityControlError,
                    ResearchRequestRejectedError,
                    RepresentativeUnavailableError,
                ) as exc:
                    disqualified.add(rid)
                    checks.append(
                        {
                            "request_id": request_id,
                            "reviewer_id": rid,
                            "claim": claim,
                            "status": "UNSUPPORTED_CORRECTION",
                            "reason": type(exc).__name__,
                            "summary": str(exc)[:1000],
                        }
                    )
                    continue
                if packet.supporting_evidence:
                    outcome = {
                        "reviewer_id": rid,
                        "objection": objection,
                        "packet_id": packet.packet_id,
                        "knowledge_status": packet.knowledge_status.value,
                    }
                    supported.append(outcome)
                    checks.append(
                        {
                            "request_id": request_id,
                            "reviewer_id": rid,
                            "claim": claim,
                            "status": "SUPPORTED_AT_LEAST_IN_PART",
                            "packet_id": packet.packet_id,
                        }
                    )
                else:
                    disqualified.add(rid)
                    checks.append(
                        {
                            "request_id": request_id,
                            "reviewer_id": rid,
                            "claim": claim,
                            "status": "UNSUPPORTED_CORRECTION",
                            "packet_id": packet.packet_id,
                            "reason": "NO_SUPPORTING_EVIDENCE",
                        }
                    )
        self.repo.docs.write_once(
            relative,
            json.dumps(
                {
                    "section_id": assignment.section_id,
                    "round_number": round_number,
                    "input_sha256": input_sha256,
                    "supported": supported,
                    "disqualified_reviewer_ids": sorted(disqualified),
                    "checks": checks,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        return supported, disqualified

    def _research_or_restore_science_correction(
        self, *, request_id: str, requester_id: str, claim: str
    ) -> EvidencePacket:
        for compartment, filename in (
            ("requests", f"{request_id}.json"),
            ("traces", f"{request_id}.json"),
        ):
            path = self.repo.root / "audit_private/research" / compartment / filename
            if not path.exists():
                continue
            saved = json.loads(path.read_text(encoding="utf-8"))
            request = saved.get("request") or {}
            if request.get("requester_id") != requester_id or request.get("claim") != claim:
                raise ValueError(
                    f"persisted Research Desk outcome conflicts with science correction: {request_id}"
                )
            if saved.get("status") == "REJECTED_NOT_CLAIM_SCOPED":
                normalized = saved.get("normalized_claim") or {}
                raise ResearchRequestRejectedError(
                    str(normalized.get("rejection_reason") or "request is not claim-scoped")
                )
            packet_relative = saved.get("packet_path")
            if packet_relative:
                packet_path = self.repo.root / packet_relative
                if not packet_path.exists():
                    raise ValueError(
                        f"persisted Research Desk packet is missing for {request_id}: {packet_relative}"
                    )
                return EvidencePacket.model_validate_json(
                    packet_path.read_text(encoding="utf-8")
                )
        return self.research_desk.research(
            ResearchRequest(
                requester_id=requester_id,
                stage=ResearchStage.SCHOLARLY_RENDERING,
                claim=claim,
            ),
            request_id=request_id,
            defer_bundle_rebuild=True,
        )

    def _final_science_research(
        self, assignment: RenderingAssignment, rid: str, original: str,
        current: str, *, cycle: int,
    ) -> list[dict]:
        """Let one final voter inspect up to four new claims, with durable steps.

        The counter is section-wide across review cycles. Exact repeat requests
        reuse the earlier answer and do not spend another Research Desk query.
        Older meetings have no frozen limit and retain their original flow.
        """

        limit = self.manifest.get("rendering_final_science_query_limit") or 0
        if not limit:
            return []
        root = (
            Path("governance_private/scholarly_rendering/final_science_research")
            / assignment.section_id / rid
        )
        history: list[dict] = []
        for previous_cycle in range(1, cycle):
            previous = self.repo.root / root / f"cycle_{previous_cycle:03d}"
            for path in sorted(previous.glob("step_*.result.json")):
                history.append(json.loads(path.read_text(encoding="utf-8")))
        current_root = root / f"cycle_{cycle:03d}"
        executed = sum(item.get("query_consumed", False) for item in history)
        max_steps = max(1, limit * 3)
        for step in range(1, max_steps + 1):
            if executed >= limit:
                break
            decision_relative = current_root / f"step_{step:02d}.decision.json"
            decision_path = self.repo.root / decision_relative
            if decision_path.exists():
                decision = FinalScienceResearchDecision.model_validate_json(
                    decision_path.read_text(encoding="utf-8")
                )
            else:
                decision = self._invoke(
                    rid,
                    stage=f"scholarly_final_science_research_{assignment.section_id}_c{cycle:03d}_s{step:02d}",
                    schema=FinalScienceResearchDecision,
                    system=(
                        "这是最终科学表决前的可选增量查证，不是表决。仅就可能影响冻结原文与当前重绘稿"
                        "科学一致性的可外部核验命题向 Research Desk 提问；已有公开证据足以回答时直接结束，"
                        "不要重复同一问题或为了耗尽额度而检索。每次只提一个命题，等证据返回再决定下一步。"
                        "Research Desk 结果仅提供证据，不能代替你的判断。"
                    ),
                    user={
                        "source_markdown": original,
                        "current_redraw": current,
                        "previous_results": history[-limit:],
                        "remaining_queries": limit - executed,
                    },
                )
                self.repo.docs.write_once(decision_relative, decision.model_dump_json(indent=2))
            if decision.finished:
                break
            claim = decision.claim or ""
            result_relative = current_root / f"step_{step:02d}.result.json"
            result_path = self.repo.root / result_relative
            if result_path.exists():
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if result.get("claim") != claim:
                    raise ValueError("frozen final science research result conflicts with its request")
            else:
                fingerprint = " ".join(claim.casefold().split())
                prior = next(
                    (item for item in history if " ".join(item.get("claim", "").casefold().split()) == fingerprint),
                    None,
                )
                if prior is not None:
                    result = {**prior, "claim": claim, "query_consumed": False, "reused": True}
                else:
                    request_id = f"{assignment.section_id}-FINAL-C{cycle:03d}-{rid}-S{step:02d}"
                    try:
                        packet = self._research_or_restore_science_correction(
                            request_id=request_id, requester_id=rid, claim=claim,
                        )
                    except ResearchRequestRejectedError as exc:
                        result = {"claim": claim, "status": "REJECTED_NOT_CLAIM_SCOPED",
                                  "query_consumed": False, "reason": str(exc)[:600]}
                    except (ResearchQualityControlError, RepresentativeUnavailableError) as exc:
                        result = {"claim": claim, "status": "UNAVAILABLE_OR_QC_FAILED",
                                  "query_consumed": True, "reason": str(exc)[:600]}
                    else:
                        result = {
                            "claim": claim, "status": "SUPPORTED_AT_LEAST_IN_PART"
                            if packet.supporting_evidence else "NO_SUPPORTING_EVIDENCE",
                            "query_consumed": True, "packet_id": packet.packet_id,
                            "knowledge_status": packet.knowledge_status.value,
                            "evidence_summary": {
                                "normalized_claim": packet.normalized_claim,
                                "supporting": [item.model_dump(mode="json") for item in packet.supporting_evidence[:3]],
                                "contradictory": [item.model_dump(mode="json") for item in packet.contradictory_evidence[:3]],
                                "limitations": [item.model_dump(mode="json") for item in packet.scope_limitations[:3]],
                            },
                        }
                self.repo.docs.write_once(result_relative, json.dumps(result, indent=2, ensure_ascii=False))
            history.append(result)
            executed += bool(result.get("query_consumed"))
        return [
            item for item in history
            if item.get("status") == "SUPPORTED_AT_LEAST_IN_PART"
        ]

    def _chair_science_revision(
        self,
        assignment: RenderingAssignment,
        round_number: int,
        original: str,
        current: str,
        reviews: list[dict],
        supported: list[dict],
        *, cycle: int = 1,
    ) -> str:
        self._show_rendering_step(
            "science", f"第 {cycle} 次修订 · 第 {round_number}/2 轮主席整合"
        )
        relative = (
            Path("public/scholarly_rendering/sections")
            / assignment.section_id
            / f"science_revision_{round_number}{_science_cycle_suffix(cycle)}.json"
        )
        path = self.repo.root / relative
        if path.exists():
            return ChairScienceRevision.model_validate_json(path.read_text(encoding="utf-8")).body_markdown
        revised = self._invoke(
            "CHAIR",
            stage=f"chair_science_revision_{assignment.section_id}_r{round_number}{_science_cycle_suffix(cycle)}",
            schema=ChairScienceRevision,
            system=(
                "在主席权限内只修改学术重绘稿，处理本轮审阅者异议。优先纠正重绘相对于冻结原文的偏差，"
                "默认保留原文的科学结论、适用范围与不确定性，不得仅为可读性改变结论。"
                "只有审阅者明确提出、列入 verified_corrections，且 Research Desk 证据至少部分支持的"
                "科学纠错，才允许在证据支持的范围内修改原文结论；部分支持不得写成确定性结论。"
                "在 dispositions 中逐项说明实际修改与对应异议；若改变原文结论，列明证据包及保留的"
                "不确定性。保留引文。"
                + _SCHOLARLY_QUANTITATIVE_PRESERVATION_RULE
                + _READER_FACING_IDENTIFIER_RULE
            ),
            user={
                "source_markdown": original,
                "current_redraw": current,
                "reviews": [{k: v for k, v in item.items() if k != "reviewer_id"} for item in reviews],
                "verified_corrections": supported,
            },
        )
        self.repo.docs.write_once(relative, revised.model_dump_json(indent=2))
        return revised.body_markdown

    def _science_ballot(
        self,
        assignment: RenderingAssignment,
        original: str,
        current: str,
        eligible: set[str],
        source_path: Path,
        *, cycle: int = 1, prior_supported: list[dict] | None = None,
    ) -> list[dict]:
        self._show_rendering_step("science", f"第 {cycle} 次修订 · 最终科学表决")
        records = [record for record in self.science if record["representative_id"] in eligible]
        private_root = (
            Path("governance_private/scholarly_rendering/science_ballots")
            / assignment.section_id
            / (f"cycle_{cycle:03d}" if cycle > 1 else "")
        )
        completed: dict[str, dict] = {}
        for record in records:
            rid = record["representative_id"]
            path = self.repo.root / private_root / f"{rid}.json"
            if path.exists():
                completed[rid] = json.loads(path.read_text(encoding="utf-8"))
        missing = [record for record in records if record["representative_id"] not in completed]

        def worker(record: dict) -> tuple[str, FinalScienceBallot | dict]:
            rid = record["representative_id"]
            self._record_read(rid, source_path, f"science_final_ballot_c{cycle:03d}")
            fresh_supported = self._final_science_research(
                assignment, rid, original, current, cycle=cycle,
            )
            try:
                vote = self._invoke_current_science_ballot(
                    rid,
                    stage=f"scholarly_science_ballot_{assignment.section_id}{_science_cycle_suffix(cycle)}",
                    original=original,
                    current=current,
                    legacy=False,
                    verified_corrections=(
                        [*(prior_supported or []), *fresh_supported]
                        if self.manifest.get("rendering_final_science_query_limit") else []
                    ),
                )
            except DisqualifiedScienceBallot as exc:
                return rid, {"vote": "DISQUALIFIED", "reason": str(exc)}
            return rid, vote

        def persist(result: tuple[str, FinalScienceBallot | dict]) -> None:
            rid, vote = result
            payload = {"reviewer_id": rid, **(
                vote.model_dump(mode="json") if isinstance(vote, FinalScienceBallot) else vote
            )}
            self.repo.docs.write_once(private_root / f"{rid}.json", json.dumps(payload, indent=2))
            completed[rid] = payload

        run_bounded_representative_lanes(
            missing,
            worker,
            self.engine.model_concurrency_limit,
            on_result=persist,
            progress=self.engine.progress,
            batch_title=f"{assignment.section_id} · 第 {cycle} 次修订后科学事实表决",
            progress_records=records,
            completed_participant_ids=completed,
            runtime_key=self._resolved_runtime_key,
        )
        for rid, payload in completed.items():
            if payload.get("vote") == "DISQUALIFIED":
                eligible.discard(rid)
        ballots = [
            completed[record["representative_id"]] for record in records
            if completed[record["representative_id"]].get("vote") in {"YES", "NO"}
        ]
        if not ballots:
            self.engine.progress.paused(
                "SCHOLARLY_SCIENCE_BALLOT_NO_ELIGIBLE_VOTERS",
                f"HUMAN · {assignment.section_id} 的科学核校员均失去本轮表决资格",
            )
            raise ScholarlyRenderingPaused("SCHOLARLY_SCIENCE_BALLOT_NO_ELIGIBLE_VOTERS")
        service = HumanConsultationService(self.repo)
        pending_legacy_issues = self._pending_legacy_science_issues(assignment, cycle)
        if sum(ballot["vote"] == "YES" for ballot in ballots) >= math.floor(len(ballots) / 2) + 1:
            for issue_id in pending_legacy_issues:
                service.withdraw(
                    issue_id=issue_id,
                    reason="冻结的最终科学表决已获过半；此前未裁决的咨询不再适用",
                )
            return ballots
        # A recorded Human ruling is authoritative. An unanswered question is
        # not: an old-format NO must first be clarified against the current text.
        has_human_ruling = self._science_consultation_has_resolution(assignment, cycle)
        clarified_legacy_no = False
        for index, ballot in enumerate(ballots):
            if ballot["vote"] != "NO" or "objections" in ballot or has_human_ruling:
                continue
            clarified_legacy_no = True
            rid = ballot["reviewer_id"]
            clarification_relative = (
                Path("governance_private/scholarly_rendering/science_ballot_clarifications")
                / assignment.section_id / f"cycle_{cycle:03d}" / f"{rid}.json"
            )
            clarification_path = self.repo.root / clarification_relative
            original_sha256 = hashlib.sha256(
                json.dumps(ballot, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            current_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
            if clarification_path.exists():
                saved = json.loads(clarification_path.read_text(encoding="utf-8"))
                if (
                    saved.get("original_ballot_sha256") != original_sha256
                    or saved.get("current_redraw_sha256") != current_sha256
                ):
                    raise ValueError("legacy science ballot clarification conflicts with the frozen ballot or current draft")
                corrected = FinalScienceBallot.model_validate(saved["clarified_ballot"])
            else:
                self._record_read(rid, source_path, f"science_ballot_clarification_c{cycle:03d}")
                corrected = self._invoke_current_science_ballot(
                    rid,
                    stage=f"scholarly_science_ballot_clarification_{assignment.section_id}_c{cycle:03d}",
                    original=original,
                    current=current,
                    legacy=True,
                )
                self.repo.docs.write_once(
                    clarification_relative,
                    json.dumps({
                        "original_ballot_sha256": original_sha256,
                        "current_redraw_sha256": current_sha256,
                        "clarified_ballot": corrected.model_dump(mode="json"),
                    }, indent=2, ensure_ascii=False),
                )
            self._validate_final_science_ballot(corrected, current)
            ballots[index] = {
                "reviewer_id": rid,
                **corrected.model_dump(mode="json"),
                "legacy_ballot_clarification_path": str(clarification_relative),
            }
        for ballot in ballots:
            if ballot["vote"] == "NO" and "objections" in ballot:
                self._validate_final_science_ballot(
                    FinalScienceBallot.model_validate({
                        "vote": ballot["vote"], "objections": ballot["objections"]
                    }), current
                )
        if clarified_legacy_no:
            for issue_id in pending_legacy_issues:
                service.withdraw(
                    issue_id=issue_id,
                    reason="旧格式最终反对票已针对当前稿重新确认；历史审阅异议不再构成人工咨询依据",
                )
        return ballots

    def _final_ballot_verified_corrections(
        self, assignment: RenderingAssignment, ballots: list[dict],
    ) -> list[dict]:
        """Forward only evidence-backed corrections actually raised on final NO votes."""

        root = (
            self.repo.root / "governance_private/scholarly_rendering/final_science_research"
            / assignment.section_id
        )
        supported_by_reviewer: dict[str, dict[str, dict]] = {}
        for ballot in ballots:
            rid = ballot.get("reviewer_id")
            if not rid:
                continue
            matches: dict[str, dict] = {}
            for path in sorted((root / rid).glob("cycle_*/step_*.result.json")):
                result = json.loads(path.read_text(encoding="utf-8"))
                if result.get("status") == "SUPPORTED_AT_LEAST_IN_PART":
                    matches[" ".join(result["claim"].casefold().split())] = result
            supported_by_reviewer[rid] = matches
        accepted: list[dict] = []
        for ballot in ballots:
            if ballot["vote"] != "NO":
                continue
            rid = ballot.get("reviewer_id")
            if not rid:
                continue
            for objection in ballot.get("objections", []):
                if not objection.get("requires_external_verification"):
                    continue
                claim = objection.get("verification_claim") or ""
                match = supported_by_reviewer.get(rid, {}).get(" ".join(claim.casefold().split()))
                if match is not None:
                    accepted.append({
                        "reviewer_id": rid, "objection": objection,
                        "packet_id": match["packet_id"],
                        "knowledge_status": match.get("knowledge_status"),
                    })
        return accepted

    def _pending_legacy_science_issues(
        self, assignment: RenderingAssignment, cycle: int
    ) -> list[str]:
        root = self.repo.root / "human_private/consultations"
        prefix = f"HC-SR-{assignment.section_id}"
        paths = list(root.glob(f"{prefix}-C{cycle:03d}-O[0-9][0-9][0-9].issue.json"))
        if cycle == 1:
            paths.append(root / f"{prefix}.issue.json")
        service = HumanConsultationService(self.repo)
        return [
            issue_id for path in sorted(paths) if path.exists()
            for issue_id in [path.name.removesuffix(".issue.json")]
            if service.resolution(issue_id) is None
            and not (root / f"{issue_id}.superseded.json").exists()
            and not (root / f"{issue_id}.withdrawn.json").exists()
        ]

    def _science_consultation_has_resolution(
        self, assignment: RenderingAssignment, cycle: int
    ) -> bool:
        root = self.repo.root / "human_private/consultations"
        prefix = f"HC-SR-{assignment.section_id}"
        paths = list(root.glob(f"{prefix}-C{cycle:03d}-O*.resolution.json"))
        if cycle == 1:
            paths.append(root / f"{prefix}.resolution.json")
        return any(path.exists() for path in paths)

    def _invoke_current_science_ballot(
        self, rid: str, *, stage: str, original: str, current: str, legacy: bool,
        verified_corrections: list[dict] | None = None,
    ) -> FinalScienceBallot:
        query_window_enabled = bool(getattr(self, "manifest", {}).get("rendering_final_science_query_limit"))
        instruction = (
            "这是对旧格式最终票的一次补充确认，原票仍永久保留。"
            "如果先前反对理由已在修稿中消失，可以改投 YES。"
            if legacy else "这是最终科学完整性表决。"
        ) + (
            "只比较冻结原文与当前重绘稿，投 YES 或 NO，不执行修改。"
            "NO 票必须列出当前稿仍然存在的具体科学异议，逐字摘录异议直接针对的当前稿片段。"
            "若异议是原文信息被遗漏，摘录当前稿中最接近遗漏位置的现存片段，说明缺失及其实质影响；不得虚构引文。"
            + (
                "可以依据本节已经核验、至少部分获支持的命题提出新的科学纠正；"
                "不得把未核验或无支持证据的纠正写进最终票。"
                if query_window_enabled else
                "不得在最终票中提出未经核验的新外部科学纠正。"
            )
            + "已经解决的历史异议不得重提；YES 票不附意见。"
            + _SCHOLARLY_QUANTITATIVE_REVIEW_RULE
            + _REVIEWER_IDENTIFIER_RULE
        )
        last_excerpt = None
        last_unverified = None
        verified_claims = {
            " ".join(str(item.get("claim") or item.get("objection", {}).get("verification_claim") or "").casefold().split())
            for item in (verified_corrections or [])
        }
        for attempt in range(3):
            ballot = self._invoke(
                rid,
                stage=stage if attempt == 0 else f"{stage}_excerpt_repair_{attempt}",
                schema=FinalScienceBallot,
                system=instruction,
                user={
                    "source_markdown": original,
                    "current_redraw": current,
                    **({
                        "previous_excerpt_not_found": last_excerpt,
                        "repair_instruction": "上一答复所引句子不在当前稿中。请重新核对当前稿，纠正票型或给出真实存在的逐字片段。",
                    } if last_excerpt is not None else {}),
                    **({
                        "unsupported_final_correction": last_unverified,
                        "correction_instruction": "这项新纠正没有已核验且至少部分支持的证据；撤回它，或仅保留已核验的具体异议。",
                    } if last_unverified is not None else {}),
                    "verified_corrections": verified_corrections or [],
                },
            )
            if not query_window_enabled and any(
                item.requires_external_verification for item in ballot.objections
            ):
                raise ValueError("final NO ballot cannot introduce an unverified external correction")
            last_excerpt = next(
                (item.current_excerpt for item in ballot.objections
                 if item.current_excerpt not in current),
                None,
            )
            last_unverified = next(
                (item.verification_claim for item in ballot.objections
                 if item.requires_external_verification and
                 " ".join((item.verification_claim or "").casefold().split()) not in verified_claims),
                None,
            )
            if last_excerpt is None and last_unverified is None:
                return ballot
        if last_unverified is not None:
            raise DisqualifiedScienceBallot(
                "final NO ballot retained an unverified or unsupported scientific correction"
            )
        raise ValueError("final science ballot cited text absent from current redraw after 3 attempts")

    @staticmethod
    def _validate_final_science_ballot(ballot: FinalScienceBallot, current: str) -> None:
        for objection in ballot.objections:
            if objection.current_excerpt not in current:
                raise ValueError(
                    "final science objection excerpt is absent from the current redraw"
                )

    def _human_science_resolution(
        self,
        assignment: RenderingAssignment,
        original: str,
        current: str,
        reviews: list[dict],
        ballots: list[dict],
        *,
        cycle: int,
        verified_corrections: list[dict],
    ) -> str | None:
        self._show_rendering_step("science", f"第 {cycle} 次修订 · 逐项处理异议")
        service = HumanConsultationService(self.repo)
        legacy_id = f"HC-SR-{assignment.section_id}"
        legacy_resolution = service.resolution(legacy_id) if cycle == 1 else None
        if legacy_resolution is not None:
            # An already-recorded Human ruling remains authoritative.  Only open
            # legacy consultations are migrated into the new iterative workflow.
            if legacy_resolution.decision == "USE_SOURCE_TEXT":
                return original
            if legacy_resolution.decision == "USE_CURRENT_REDRAW":
                return current
            if legacy_resolution.human_wording is None:
                raise ValueError("Human wording resolution is missing its final text")
            return legacy_resolution.human_wording

        objections: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        section_root = Path("public/scholarly_rendering/sections") / assignment.section_id
        docket_relative = (
            Path("governance_private/scholarly_rendering/science_human_dockets")
            / assignment.section_id / f"cycle_{cycle:03d}.json"
        )
        docket_path = self.repo.root / docket_relative
        ballot_sha256 = hashlib.sha256(
            json.dumps(ballots, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        final_objections = [
            objection for ballot in ballots if ballot["vote"] == "NO"
            for objection in ballot.get("objections", [])
        ]
        if docket_path.exists():
            frozen_docket = json.loads(docket_path.read_text(encoding="utf-8"))
            if (frozen_docket.get("section_id") != assignment.section_id
                    or frozen_docket.get("cycle") != cycle
                    or (frozen_docket.get("ballot_sha256") is not None
                        and frozen_docket["ballot_sha256"] != ballot_sha256)):
                self.engine.progress.paused(
                    "SCHOLARLY_RENDERING_CONSULTATION_INPUT_CONFLICT",
                    f"HUMAN · {assignment.section_id} 的冻结咨询清单与当前表决不一致；未覆盖原记录",
                )
                raise ScholarlyRenderingPaused("SCHOLARLY_RENDERING_CONSULTATION_INPUT_CONFLICT")
            final_objections = frozen_docket["objections"]
        elif not final_objections and self._science_consultation_has_resolution(assignment, cycle):
            # Old-format final ballots carried no objections. A partially
            # answered consultation must replay the entire frozen docket, not
            # merely the objections whose answers happen to be on disk. The
            # first context records the original docket size. In the legacy
            # path that docket was assembled from the completed reviews.
            resolution_root = self.repo.root / "human_private/consultations"
            existing_section_root = self.repo.root / section_root
            first_context_path = existing_section_root / f"science_human_context_c{cycle:03d}_o001.json"
            if first_context_path.exists():
                first_context = json.loads(first_context_path.read_text(encoding="utf-8"))
                frozen_count = first_context.get("objection_count")
                if isinstance(frozen_count, int) and frozen_count > 1:
                    candidates = [
                        objection
                        for review in reviews
                        for objection in review["review"]["objections"]
                    ]
                    deduplicated = list(dict.fromkeys(
                        (item["issue"], item.get("proposed_wording") or "", item.get("verification_claim") or "")
                        for item in candidates
                    ))
                    if len(deduplicated) != frozen_count or not candidates or candidates[0] != first_context["objection"]:
                        raise ValueError("frozen science consultation docket conflicts with restored reviews")
                    final_objections.extend(candidates)
            for path in sorted(resolution_root.glob(f"{legacy_id}-C{cycle:03d}-O*.resolution.json")):
                issue_id = path.name.removesuffix(".resolution.json")
                match = re.fullmatch(
                    rf"{re.escape(legacy_id)}-C{cycle:03d}-O(\d{{3}})(-CLARIFIED)?",
                    issue_id,
                )
                if match is None:
                    continue
                suffix = "_clarified" if match.group(2) else ""
                context_path = existing_section_root / (
                    f"science_human_context_c{cycle:03d}_o{match.group(1)}{suffix}.json"
                )
                if not context_path.exists():
                    raise ValueError(f"resolved science consultation has no frozen context: {issue_id}")
                if not final_objections:
                    final_objections.append(json.loads(context_path.read_text(encoding="utf-8"))["objection"])
        for objection in final_objections:
            key = (
                objection["issue"],
                objection.get("proposed_wording") or "",
                objection.get("verification_claim") or "",
            )
            if key not in seen:
                seen.add(key)
                objections.append(objection)
        if not objections:
            raise ValueError("failed science ballot has no current-draft objection; clarify old-format NO votes")

        # Freeze the *whole* consultation docket before the first question is
        # opened. A partial set of answered questions is never an adequate
        # source from which to reconstruct the original number or order.
        if not docket_path.exists():
            existing_section_root = self.repo.root / section_root
            for index, objection in enumerate(objections, start=1):
                context_path = existing_section_root / f"science_human_context_c{cycle:03d}_o{index:03d}.json"
                if not context_path.exists():
                    continue
                frozen_context = json.loads(context_path.read_text(encoding="utf-8"))
                if (frozen_context.get("objection") != objection
                        or frozen_context.get("objection_count") != len(objections)):
                    raise ValueError("frozen science consultation context conflicts with current docket")
            self.repo.docs.write_once(
                docket_relative,
                json.dumps({
                    "section_id": assignment.section_id,
                    "cycle": cycle,
                    "source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                    "current_redraw_sha256": hashlib.sha256(current.encode("utf-8")).hexdigest(),
                    "ballot_sha256": ballot_sha256,
                    "objections": objections,
                }, indent=2, ensure_ascii=False),
            )
        else:
            if (frozen_docket.get("source_sha256") != hashlib.sha256(original.encode("utf-8")).hexdigest()
                    or frozen_docket.get("current_redraw_sha256") != hashlib.sha256(current.encode("utf-8")).hexdigest()):
                self.engine.progress.paused(
                    "SCHOLARLY_RENDERING_CONSULTATION_INPUT_CONFLICT",
                    f"HUMAN · {assignment.section_id} 的冻结咨询清单与当前原文或重绘稿不一致；未覆盖原记录",
                )
                raise ScholarlyRenderingPaused("SCHOLARLY_RENDERING_CONSULTATION_INPUT_CONFLICT")

        legacy_path = self.repo.root / "human_private/consultations" / f"{legacy_id}.issue.json"
        first_issue_id = f"{legacy_id}-C{cycle:03d}-O001"
        if (cycle == 1 and legacy_path.exists()
                and service.resolution(legacy_id) is None
                and not (self.repo.root / "human_private/consultations" / f"{legacy_id}.withdrawn.json").exists()):
            service.supersede(
                issue_id=legacy_id,
                successor_issue_id=first_issue_id,
                reason="改为逐条异议咨询、主席修稿与重新审核；不要求人类撰写整章终稿",
            )

        rulings: list[dict] = []
        unresolved_count = 0
        clarified = (self.repo.root / "human_private/consultations"
                     / f"{first_issue_id}.withdrawn.json").exists()
        for index, objection in enumerate(objections, start=1):
            suffix = "_clarified" if clarified else ""
            issue_id = f"{legacy_id}-C{cycle:03d}-O{index:03d}" + ("-CLARIFIED" if clarified else "")
            advice_relative = section_root / f"science_human_advice_c{cycle:03d}_o{index:03d}{suffix}.json"
            advice_path = self.repo.root / advice_relative
            if advice_path.exists():
                advice_payload = json.loads(advice_path.read_text(encoding="utf-8"))
                advice = ChairScienceIssueAdvice.model_validate(advice_payload)
            else:
                advice_stage = f"chair_science_human_advice_{assignment.section_id}_c{cycle:03d}_o{index:03d}"
                advice_system = (
                    "仅为人类逐条解释科学事实异议。找出与本条异议相关的原文、当前稿逐字片段；"
                    "如无法定位，使用‘未定位’。提出一个可操作的修订建议并说明它如何保持原意、限制与不确定性。"
                    "不要代人类决定是否采纳，不要改稿，不要隐藏反对意见，也不要新增未经核验的事实。"
                )
                advice_user = {
                    "source_markdown": original,
                    "current_redraw": current,
                    "objection": objection,
                    "verified_corrections": verified_corrections,
                }
                advice = self._restore_pre_limit_chair_advice(
                    stage=advice_stage, system=advice_system, user=advice_user,
                ) or self._invoke(
                    "CHAIR", stage=advice_stage, schema=ChairScienceIssueAdvice,
                    system=advice_system, user=advice_user,
                )
                self.repo.docs.write_once(advice_relative, advice.model_dump_json(indent=2))
                advice_payload = advice.model_dump(mode="json")
            source_excerpt = (
                advice.source_excerpt if advice.source_excerpt in original else "未定位到逐字片段，请参阅完整原文"
            )
            redraw_excerpt = objection.get("current_excerpt") or (
                advice.redraw_excerpt if advice.redraw_excerpt in current else "未定位到逐字片段，请参阅当前重绘稿"
            )
            context_relative = section_root / f"science_human_context_c{cycle:03d}_o{index:03d}{suffix}.json"
            context_payload = {
                "section_id": assignment.section_id,
                "cycle": cycle,
                "objection_number": index,
                "objection_count": len(objections),
                "source_markdown": original,
                "current_redraw": current,
                "objection": objection,
                "chair_advice": advice_payload,
                "yes_votes": sum(item["vote"] == "YES" for item in ballots),
                "eligible_votes": len(ballots),
            }
            context_path = self.repo.root / context_relative
            if context_path.exists():
                if json.loads(context_path.read_text(encoding="utf-8")) != context_payload:
                    raise ValueError(
                        f"frozen science consultation context conflicts with current ballot: {context_relative}"
                    )
            else:
                self.repo.docs.write_once(
                    context_relative,
                    json.dumps(context_payload, indent=2, ensure_ascii=False),
                )
            issue = HumanConsultationIssue(
                issue_id=issue_id,
                meeting_id=self.repo.meeting_id,
                reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
                stage="SCHOLARLY_SCIENCE_REVIEW",
                question=(
                    f"第 {cycle} 个科学审核周期的最终表决未获过半；现在处理第 {index}/{len(objections)} 条异议。\n"
                    f"反对意见：{objection['issue']}\n"
                    f"原文相关片段：{source_excerpt}\n当前稿相关片段：{redraw_excerpt}\n"
                    f"主席建议：{advice.suggestion}\n"
                    f"完整对照资料：{context_relative}。可逐条向主席追问，无须撰写 Markdown 终稿。"
                ),
                options=[
                    "ACCEPT_SCIENCE_OBJECTION",
                    "REJECT_SCIENCE_OBJECTION",
                    "DIRECT_CHAIR_SCIENCE_REVISION",
                ],
                affected_items=[assignment.section_id],
            )
            issue_path = self.repo.root / "human_private/consultations" / f"{issue_id}.issue.json"
            if issue_path.exists():
                frozen_issue = HumanConsultationIssue.model_validate_json(
                    issue_path.read_text(encoding="utf-8")
                )
                if frozen_issue.model_dump(mode="json", exclude={"question"}) != issue.model_dump(
                    mode="json", exclude={"question"}
                ):
                    raise ValueError(f"frozen science consultation has changed procedural terms: {issue_id}")
                # The Human may already be reading the recorded wording. A new
                # presentation template must not change an immutable issue on resume.
                issue = frozen_issue
            else:
                service.open_issue(issue)
            resolution = service.resolution(issue_id)
            if resolution is None:
                unresolved_count += 1
                continue
            rulings.append({
                "objection_number": index,
                "objection": objection,
                "decision": resolution.decision,
                "human_direction": resolution.rationale,
                **({"decision_authority": resolution.authority}
                   if resolution.authority == "DELEGATED_CHAIR" else {}),
                "chair_suggestion": advice.suggestion,
            })

        if unresolved_count:
            self.engine.status.phase = MeetingPhase.PAUSED
            self.engine.status.paused_reason = "SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED"
            self.engine.progress.paused(
                "SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
                f"HUMAN · 本轮尚有 {unresolved_count}/{len(objections)} 条异议待处理；全部处理后才修稿",
            )
            return None

        revision_relative = section_root / f"science_human_revision_c{cycle:03d}.json"
        revision_path = self.repo.root / revision_relative
        if revision_path.exists():
            revised = ChairScienceRevision.model_validate_json(
                revision_path.read_text(encoding="utf-8")
            )
        else:
            delegated_ruling = any(item.get("decision_authority") == "DELEGATED_CHAIR" for item in rulings)
            revised = self._invoke(
                "CHAIR",
                stage=f"chair_science_human_revision_{assignment.section_id}_c{cycle:03d}",
                schema=ChairScienceRevision,
                system=(
                    (
                        "依据本轮已记录的逐条科学异议裁决，继续修改当前重绘稿，而非重写原始报告。"
                        "逐条尊重 decision_authority：HUMAN 是人类裁决，DELEGATED_CHAIR 是人类授权的主席代裁，"
                        "不得把后者表述为人类亲自决定。裁决理由是修订指示，不是要求逐字作为全文发表。"
                        if delegated_ruling else
                        "依据人类对每条科学异议的决定，继续修改当前重绘稿，而非重写原始报告。"
                        "采纳或拒绝意见时遵守人类具体方向；人类给出的文字是修订指示，不是要求逐字作为全文发表。"
                    )
                    + "默认保持冻结原文的结论、适用范围、不确定性及引文；仅对审阅中已明确提出、"
                    "列入 verified_corrections 且经 Research Desk 证据至少部分支持的科学纠错，"
                    "可在证据支持范围内修改原结论，不得夸大为确定性结论。"
                    + ("在 dispositions 中逐项记录裁决与实际改动；若改变原文结论，列明证据包及"
                     if delegated_ruling else
                     "在 dispositions 中逐项记录人类决定与实际改动；若改变原文结论，列明证据包及")
                    + "剩余不确定性。"
                    "修稿后必须重新接受科学审核与表决。"
                    + _SCHOLARLY_QUANTITATIVE_PRESERVATION_RULE
                    + _READER_FACING_IDENTIFIER_RULE
                ),
                user={
                    "source_markdown": original,
                    "current_redraw": current,
                    **({
                        "consultation_rulings": [
                            {**{key: value for key, value in item.items() if key != "human_direction"},
                             "decision_rationale": item["human_direction"],
                             "decision_authority": item.get("decision_authority", "HUMAN")}
                            for item in rulings
                        ]
                    } if delegated_ruling else {"human_rulings": rulings}),
                    "verified_corrections": verified_corrections,
                    "prior_ballots": ballots,
                },
            )
            self.repo.docs.write_once(revision_relative, revised.model_dump_json(indent=2))
        return revised.body_markdown

    def _open_integrity_consultation(self, issue: HumanConsultationIssue):
        service = HumanConsultationService(self.repo)
        service.open_issue(issue)
        resolution = service.resolution(issue.issue_id)
        if resolution is None:
            raise ScholarlyRenderingPaused(issue.reason_code)
        return resolution

    def _citation_round(
        self,
        assignment: RenderingAssignment,
        round_number: int,
        original: str,
        current: str,
        source_path: Path,
    ) -> str:
        self._show_rendering_step("citation", f"第 {round_number}/2 轮审阅")
        self.engine.status.phase = MeetingPhase.SCHOLARLY_CITATION_REVIEW
        self.engine.progress.status(
            MeetingPhase.SCHOLARLY_CITATION_REVIEW,
            f"{assignment.section_id} · 引文核校第 {round_number}/2 轮；意见并行、修正案离散表决",
        )
        private_root = (
            Path("governance_private/scholarly_rendering/citation_reviews")
            / assignment.section_id
            / f"round_{round_number}"
        )
        completed: dict[str, dict] = {}
        for record in self.citations:
            rid = record["representative_id"]
            path = self.repo.root / private_root / f"{rid}.json"
            if path.exists():
                completed[rid] = json.loads(path.read_text(encoding="utf-8"))
        missing = [record for record in self.citations if record["representative_id"] not in completed]

        def worker(record: dict) -> tuple[str, dict]:
            rid = record["representative_id"]
            self._record_read(rid, source_path, f"citation_review_round_{round_number}")
            review = self._invoke(
                rid,
                stage=f"scholarly_citation_review_{assignment.section_id}_r{round_number}",
                schema=CitationReview,
                system=(
                    "审查引文账目：比较原文与重绘稿，包括科学审阅引入的引文变化。每项具体问题只能写成 INSERT 或 REMOVE；"
                    "迁移须拆为一次 REMOVE 和一次 INSERT。不得否决整章或改变科学结论。"
                    "非同行评审来源应在参考文献条目末尾醒目标明。问题列表为空表示未发现离散的引文缺陷。"
                    "不得把内部模块号、record_id 或证据包编号当作学术引文插回正文；"
                    "只要求保留可核对的真实文献来源及其正确位置。"
                    "同时检查行文结构中的标题层级与章节编号纪律：章号由最终组装器统一生成，"
                    "本块不应手写章号、混用中文与阿拉伯数字编号、重复全文标题或跳跃标题层级。"
                    "把这类问题逐条写入 structure_issues，引用当前稿中的具体标题；"
                    "不要将其伪装成引文 INSERT/REMOVE，也不得借格式意见改变科学内容。"
                ),
                user={
                    "section": assignment.model_dump(mode="json"),
                    "source_markdown": original,
                    "current_redraw": current,
                    "evidence_index": self._evidence_index(
                        rid, f"citation_review_round_{round_number}"
                    ),
                },
            )
            return rid, review.model_dump(mode="json")

        def persist(result: tuple[str, dict]) -> None:
            rid, review = result
            payload = {"reviewer_id": rid, "review": review}
            self.repo.docs.write_once(
                private_root / f"{rid}.json", json.dumps(payload, indent=2, ensure_ascii=False)
            )
            completed[rid] = payload

        run_bounded_representative_lanes(
            missing,
            worker,
            self.engine.model_concurrency_limit,
            on_result=persist,
            progress=self.engine.progress,
            batch_title=f"{assignment.section_id} · 引文核校第 {round_number}/2 轮",
            progress_records=self.citations,
            completed_participant_ids=completed,
            runtime_key=self._resolved_runtime_key,
        )
        reviews = [completed[record["representative_id"]] for record in self.citations]
        structure_relative = (
            Path("public/scholarly_rendering/sections")
            / assignment.section_id / f"heading_review_{round_number}.json"
        )
        if not (self.repo.root / structure_relative).exists():
            self.repo.docs.write_once(
                structure_relative,
                json.dumps({
                    "section_id": assignment.section_id,
                    "round_number": round_number,
                    "findings": [
                        note
                        for record in reviews
                        for note in record["review"].get("structure_issues", [])
                    ],
                    "disposition": "Publication assembler normalizes numbering; final heading audit checks the result.",
                }, indent=2, ensure_ascii=False),
            )
        self._show_rendering_step("citation", f"第 {round_number}/2 轮 · 主席整理引文意见")
        docket_relative = (
            Path("public/scholarly_rendering/sections")
            / assignment.section_id
            / f"citation_docket_{round_number}.json"
        )
        docket_path = self.repo.root / docket_relative
        if docket_path.exists():
            docket = CitationDocket.model_validate_json(docket_path.read_text(encoding="utf-8"))
            docket = self._normalize_docket_ids(docket)
        else:
            docket = self._invoke(
                "CHAIR",
                stage=f"chair_citation_docket_{assignment.section_id}_r{round_number}",
                schema=CitationDocket,
                system=(
                    "在主席权限内将引文意见去重、拆分为原子化的 INSERT 或 REMOVE 修正案。"
                    "不得生成 MOVE；按数字顺序从 CA-001 编号。不得进行实质性科学修改。"
                    "不得以完整性为由新增 RM-xx、record_id 等内部追踪代号；"
                    "引文修正应指向真实文献及当前稿中读者可见的自然语言表述。"
                    "structure_issues 是标题层级与编号核查记录，不得转换成引文 INSERT/REMOVE；"
                    "最终编号由出版组装器统一生成并执行独立结构审计。"
                ),
                user={"reviews": [item["review"] for item in reviews], "current_redraw": current},
            )
            docket = self._normalize_docket_ids(docket)
            self.repo.docs.write_once(docket_relative, docket.model_dump_json(indent=2))
        if not docket.amendments:
            return current
        self._show_rendering_step("citation", f"第 {round_number}/2 轮 · 引文修正案表决")
        tallies = self._citation_votes(assignment, round_number, docket, current)
        self._show_rendering_step("citation", f"第 {round_number}/2 轮 · 主席应用引文修正")
        application_relative = (
            Path("public/scholarly_rendering/sections")
            / assignment.section_id
            / f"citation_application_{round_number}.json"
        )
        application_path = self.repo.root / application_relative
        if application_path.exists():
            application = ChairCitationApplication.model_validate_json(
                application_path.read_text(encoding="utf-8")
            )
        else:
            application = self._invoke(
                "CHAIR",
                stage=f"chair_citation_application_{assignment.section_id}_r{round_number}",
                schema=ChairCitationApplication,
                system=(
                    "在主席权限内逐项处理引文修正案。多数票结果仅供参考，但必须逐项考虑。"
                    "按议程顺序对每条修正案恰好返回一项应用或拒绝决定；不得返回或改写论文正文。"
                ),
                user={
                    "docket": docket.model_dump(mode="json"),
                    "tallies": tallies,
                },
            )
            normalized = self._normalize_citation_application(application, docket)
            if normalized is None:
                application = self._invoke(
                    "CHAIR",
                    stage=(
                        f"chair_citation_application_{assignment.section_id}_r{round_number}"
                        "_contract_repair"
                    ),
                    schema=ChairCitationApplication,
                    system=(
                        "只修复决定集合的格式：依给定顺序对每个预期修正案编号恰好返回一项应用或拒绝决定。"
                        "不得改写修正案或论文正文。"
                    ),
                    user={
                        "expected_amendment_ids": [
                            item.amendment_id for item in docket.amendments
                        ],
                        "invalid_application": application.model_dump(mode="json"),
                    },
                )
                normalized = self._normalize_citation_application(application, docket)
            if normalized is not None:
                application = normalized
                self.repo.docs.write_once(
                    application_relative, application.model_dump_json(indent=2)
                )
        normalized = self._normalize_citation_application(application, docket)
        if normalized is None:
            issue = HumanConsultationIssue(
                issue_id=(
                    f"HC-SR-{assignment.section_id}-APPLICATION-R{round_number}"
                ),
                meeting_id=self.repo.meeting_id,
                reason_code="SCHOLARLY_CITATION_APPLICATION_CONTRACT_INVALID",
                stage="SCHOLARLY_CITATION_REVIEW",
                question=(
                    "Chair 在一次契约修复后仍未逐项处置全部引文修正案。"
                    "请选择保留本轮修改前文本，或由 Human 提供最终章节表述。"
                ),
                options=["KEEP_PRE_CITATION_TEXT", "USE_HUMAN_WORDING"],
                affected_items=[item.amendment_id for item in docket.amendments],
            )
            resolution = self._open_integrity_consultation(issue)
            if resolution.decision == "KEEP_PRE_CITATION_TEXT":
                return current
            if resolution.human_wording is None:
                raise ValueError("Human citation application resolution is missing wording")
            return resolution.human_wording
        application = normalized
        return self._apply_citation_with_repair(
            assignment,
            round_number,
            current,
            docket,
            application,
        )

    @staticmethod
    def _normalize_docket_ids(docket: CitationDocket) -> CitationDocket:
        return CitationDocket(
            amendments=[
                amendment.model_copy(update={"amendment_id": f"CA-{index:03d}"})
                for index, amendment in enumerate(docket.amendments, start=1)
            ]
        )

    @staticmethod
    def _normalize_citation_application(
        application: ChairCitationApplication, docket: CitationDocket
    ) -> ChairCitationApplication | None:
        by_id: dict[str, CitationDisposition] = {}
        for decision in application.decisions:
            if decision.amendment_id in by_id:
                return None
            by_id[decision.amendment_id] = decision
        expected = [item.amendment_id for item in docket.amendments]
        if set(by_id) != set(expected):
            return None
        return ChairCitationApplication(decisions=[by_id[item] for item in expected])

    def _apply_citation_with_repair(
        self,
        assignment: RenderingAssignment,
        round_number: int,
        current: str,
        docket: CitationDocket,
        application: ChairCitationApplication,
    ) -> str:
        try:
            return self._apply_citation_decisions(current, docket, application)
        except CitationTargetConflict as first_conflict:
            repair_relative = (
                Path("public/scholarly_rendering/sections")
                / assignment.section_id
                / f"citation_anchor_repair_{round_number}.json"
            )
            repair_path = self.repo.root / repair_relative
            if repair_path.exists():
                repaired = CitationAnchorRepair.model_validate_json(
                    repair_path.read_text(encoding="utf-8")
                )
                self._validate_anchor_repair(docket, repaired)
            else:
                repaired = self._invoke(
                    "CHAIR",
                    stage=(
                        f"chair_citation_anchor_repair_{assignment.section_id}_r{round_number}"
                    ),
                    schema=CitationAnchorRepair,
                    system=(
                        "只修复引文 target_text 锚点缺失或歧义。按原顺序返回全部修正案，"
                        "逐字保留 amendment_id、operation、citation_text、source_packet_id 和 rationale；"
                        "只改 target_text，使每个已应用目标在 current_redraw 中恰好对应一处子串。"
                    ),
                    user={
                        "current_redraw": current,
                        "docket": docket.model_dump(mode="json"),
                        "target_conflicts": first_conflict.conflicts,
                    },
                )
                try:
                    self._validate_anchor_repair(docket, repaired)
                except ValueError as exc:
                    self.repo.events.append(
                        "SCHOLARLY_CITATION_ANCHOR_REPAIR_REJECTED",
                        {
                            "meeting_id": self.repo.meeting_id,
                            "section_id": assignment.section_id,
                            "round_number": round_number,
                            "reason": str(exc),
                        },
                        actor="orchestrator",
                    )
                    repaired = CitationAnchorRepair(amendments=docket.amendments)
                else:
                    self.repo.docs.write_once(
                        repair_relative, repaired.model_dump_json(indent=2)
                    )
            repaired_docket = CitationDocket(amendments=repaired.amendments)
            try:
                return self._apply_citation_decisions(
                    current, repaired_docket, application
                )
            except CitationTargetConflict as final_conflict:
                issue_id = f"HC-SR-{assignment.section_id}-CITATION-R{round_number}"
                service = HumanConsultationService(self.repo)
                resolution = service.resolution(issue_id)
                if resolution is not None:
                    # A Human decision already recorded under the old procedure
                    # is authoritative and must not be reinterpreted on resume.
                    if resolution.decision == "KEEP_PRE_CITATION_TEXT":
                        return current
                    if resolution.decision == "USE_HUMAN_WORDING":
                        if resolution.human_wording is None:
                            raise ValueError("Human citation resolution is missing wording")
                        return resolution.human_wording
                    if resolution.decision != "SKIP_AMBIGUOUS_CITATION_AMENDMENTS":
                        raise ValueError("unknown frozen Human citation resolution")
                skipped: set[str] = set()
                invalid: list[dict] = []
                conflicts = final_conflict.conflicts
                while True:
                    newly_invalid = [
                        conflict for conflict in conflicts
                        if conflict["amendment_id"] not in skipped
                    ]
                    if not newly_invalid:
                        raise ValueError("citation invalidation made no progress")
                    invalid.extend(newly_invalid)
                    skipped.update(item["amendment_id"] for item in newly_invalid)
                    try:
                        result = self._apply_citation_decisions(
                            current, repaired_docket, application, skip_ids=skipped,
                        )
                        break
                    except CitationTargetConflict as more_conflicts:
                        conflicts = more_conflicts.conflicts
                invalid_relative = (
                    Path("public/scholarly_rendering/sections")
                    / assignment.section_id / f"citation_invalid_amendments_{round_number}.json"
                )
                invalid_record = {
                    "section_id": assignment.section_id,
                    "round_number": round_number,
                    "reason_code": "CITATION_ANCHOR_NOT_UNIQUE_AFTER_REPAIR",
                    "input_sha256": hashlib.sha256(json.dumps({
                        "current_redraw": current,
                        "repaired_docket": repaired_docket.model_dump(mode="json"),
                        "application": application.model_dump(mode="json"),
                    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
                    "invalid_amendments": invalid,
                }
                invalid_path = self.repo.root / invalid_relative
                if invalid_path.exists():
                    if json.loads(invalid_path.read_text(encoding="utf-8")) != invalid_record:
                        raise ValueError("frozen citation invalidation conflicts with current inputs")
                else:
                    self.repo.docs.write_once(
                        invalid_relative,
                        json.dumps(invalid_record, indent=2, ensure_ascii=False),
                    )
                issue_path = self.repo.root / "human_private/consultations" / f"{issue_id}.issue.json"
                superseded_path = issue_path.with_name(f"{issue_id}.superseded.json")
                if issue_path.exists() and resolution is None and not superseded_path.exists():
                    service.withdraw(
                        issue_id=issue_id,
                        reason="一次锚点修复后仍无法唯一定位的修正案按新规则逐项作无效处理",
                    )
                return result

    @staticmethod
    def _validate_anchor_repair(
        original: CitationDocket, repaired: CitationAnchorRepair
    ) -> None:
        if len(original.amendments) != len(repaired.amendments):
            raise ValueError("citation anchor repair must preserve every amendment")
        for before, after in zip(original.amendments, repaired.amendments, strict=True):
            before_payload = before.model_dump(mode="json", exclude={"target_text"})
            after_payload = after.model_dump(mode="json", exclude={"target_text"})
            if before_payload != after_payload:
                raise ValueError("citation anchor repair may change only target_text")

    @staticmethod
    def _apply_citation_decisions(
        current: str,
        docket: CitationDocket,
        application: ChairCitationApplication,
        *,
        skip_ids: set[str] | None = None,
    ) -> str:
        expected = [item.amendment_id for item in docket.amendments]
        received = [item.amendment_id for item in application.decisions]
        if received != expected:
            raise ValueError(
                "Chair citation decisions must cover every amendment exactly once in docket order"
            )
        text = current
        skipped = skip_ids or set()
        by_id = {item.amendment_id: item for item in docket.amendments}
        conflicts: list[dict] = []
        for decision in application.decisions:
            if not decision.apply or decision.amendment_id in skipped:
                continue
            amendment = by_id[decision.amendment_id]
            occurrences = text.count(amendment.target_text)
            if occurrences != 1:
                conflicts.append(
                    {
                        "amendment_id": amendment.amendment_id,
                        "operation": amendment.operation,
                        "target_text": amendment.target_text,
                        "occurrences": occurrences,
                        "nearby_contexts": ScholarlyRenderingRunner._target_contexts(
                            text, amendment.target_text
                        ),
                    }
                )
                continue
            if amendment.operation == "REMOVE":
                text = text.replace(amendment.target_text, "", 1)
            else:
                text = text.replace(
                    amendment.target_text,
                    amendment.target_text + str(amendment.citation_text),
                    1,
                )
        if conflicts:
            raise CitationTargetConflict(conflicts)
        return text

    @staticmethod
    def _target_contexts(text: str, target: str, *, radius: int = 120) -> list[str]:
        if not target:
            return []
        contexts: list[str] = []
        start = 0
        while len(contexts) < 5:
            index = text.find(target, start)
            if index < 0:
                break
            contexts.append(
                text[max(0, index - radius) : min(len(text), index + len(target) + radius)]
            )
            start = index + max(1, len(target))
        return contexts

    def _citation_votes(
        self,
        assignment: RenderingAssignment,
        round_number: int,
        docket: CitationDocket,
        current: str,
    ) -> list[dict]:
        private_root = (
            Path("governance_private/scholarly_rendering/citation_votes")
            / assignment.section_id
            / f"round_{round_number}"
        )
        completed: dict[str, dict] = {}
        for record in self.citations:
            rid = record["representative_id"]
            path = self.repo.root / private_root / f"{rid}.json"
            if path.exists():
                completed[rid] = json.loads(path.read_text(encoding="utf-8"))
        missing = [record for record in self.citations if record["representative_id"] not in completed]

        def worker(record: dict) -> tuple[str, list[dict]]:
            rid = record["representative_id"]
            votes = self._citation_vote_with_contract_repair(
                rid=rid,
                assignment=assignment,
                round_number=round_number,
                docket=docket,
                current=current,
            )
            return rid, votes

        def persist(result: tuple[str, list[dict]]) -> None:
            rid, votes = result
            payload = {"reviewer_id": rid, "votes": votes}
            self.repo.docs.write_once(private_root / f"{rid}.json", json.dumps(payload, indent=2))
            completed[rid] = payload

        run_bounded_representative_lanes(
            missing,
            worker,
            self.engine.model_concurrency_limit,
            on_result=persist,
            progress=self.engine.progress,
            batch_title=f"{assignment.section_id} · 引文修正案并行表决",
            progress_records=self.citations,
            completed_participant_ids=completed,
            runtime_key=self._resolved_runtime_key,
        )
        threshold = math.floor(len(self.citations) / 2) + 1
        return [
            {
                "amendment_id": amendment.amendment_id,
                "yes": sum(
                    vote["vote"] == "YES"
                    for payload in completed.values()
                    for vote in payload["votes"]
                    if vote["amendment_id"] == amendment.amendment_id
                ),
                "required_yes": threshold,
                "majority_adopted": sum(
                    vote["vote"] == "YES"
                    for payload in completed.values()
                    for vote in payload["votes"]
                    if vote["amendment_id"] == amendment.amendment_id
                ) >= threshold,
            }
            for amendment in docket.amendments
        ]

    def _citation_vote_with_contract_repair(
        self,
        *,
        rid: str,
        assignment: RenderingAssignment,
        round_number: int,
        docket: CitationDocket,
        current: str,
    ) -> list[dict]:
        expected = [item.amendment_id for item in docket.amendments]
        base_stage = f"scholarly_citation_vote_{assignment.section_id}_r{round_number}"
        system = (
            "独立对每条引文修正案投 YES 或 NO，按议程顺序每项返回一票。"
            "不得提出新文本或对整章作总体判定。"
        )
        initial = self._invoke(
            rid,
            stage=base_stage,
            schema=CitationVoteSet,
            system=system,
            user={"current_redraw": current, "docket": docket.model_dump(mode="json")},
        )
        normalized = self._normalize_vote_set(initial, expected)
        if normalized is not None:
            return normalized
        repaired = self._invoke(
            rid,
            stage=f"{base_stage}_contract_repair",
            schema=CitationVoteSet,
            system=(
                "只修复票集格式：按给定顺序对每个预期修正案编号恰好返回一张 YES/NO 票。"
                "不得增加解释；除删除重复票所必需之外，不得修改已有票。"
            ),
            user={
                "expected_amendment_ids": expected,
                "invalid_vote_set": initial.model_dump(mode="json"),
            },
        )
        normalized = self._normalize_vote_set(repaired, expected)
        if normalized is not None:
            return normalized
        issue = HumanConsultationIssue(
            issue_id=f"HC-SR-{assignment.section_id}-VOTE-R{round_number}-{rid}",
            meeting_id=self.repo.meeting_id,
            reason_code="SCHOLARLY_CITATION_VOTE_CONTRACT_INVALID",
            stage="SCHOLARLY_CITATION_REVIEW",
            question=(
                f"核校员 {rid} 在原始提交和一次契约修复后仍未覆盖全部引文修正案。"
                "请选择在排障或替换模型后再请求一次，或把本轮票记为缺失票继续。\n\n"
                f"应有项目: {expected}\n"
                f"实际项目: {[item.amendment_id for item in repaired.votes]}"
            ),
            options=[
                "RETRY_INVALID_CITATION_VOTE",
                "EXCLUDE_INVALID_CITATION_VOTE",
            ],
            affected_items=expected,
        )
        resolution = self._open_integrity_consultation(issue)
        if resolution.decision == "EXCLUDE_INVALID_CITATION_VOTE":
            return [
                {"amendment_id": amendment_id, "vote": "MISSING"}
                for amendment_id in expected
            ]
        retry = self._invoke(
            rid,
            stage=f"{base_stage}_human_authorized_retry",
            schema=CitationVoteSet,
            system=system,
            user={"current_redraw": current, "docket": docket.model_dump(mode="json")},
        )
        normalized = self._normalize_vote_set(retry, expected)
        if normalized is not None:
            return normalized
        self.repo.events.append(
            "SCHOLARLY_CITATION_VOTE_RETRY_RECORDED_MISSING",
            {
                "meeting_id": self.repo.meeting_id,
                "section_id": assignment.section_id,
                "round_number": round_number,
                "representative_id": rid,
                "expected_amendment_ids": expected,
            },
            actor="orchestrator",
        )
        return [
            {"amendment_id": amendment_id, "vote": "MISSING"}
            for amendment_id in expected
        ]

    @staticmethod
    def _normalize_vote_set(
        vote_set: CitationVoteSet, expected: list[str]
    ) -> list[dict] | None:
        by_id: dict[str, CitationVoteItem] = {}
        for vote in vote_set.votes:
            if vote.amendment_id in by_id:
                return None
            by_id[vote.amendment_id] = vote
        if set(by_id) != set(expected):
            return None
        return [by_id[item].model_dump(mode="json") for item in expected]

    def _publish(
        self,
        source_path: Path,
        plan: RenderingPlan,
        completed: list[tuple[RenderingAssignment, str]],
    ) -> ScholarlyRenderingResult:
        self.engine.status.phase = MeetingPhase.SCHOLARLY_RENDERING_PUBLICATION
        self.engine.progress.status(
            MeetingPhase.SCHOLARLY_RENDERING_PUBLICATION,
            "Chair 重绘正文已通过科学事实与引文核校；正在生成所选出版格式",
        )
        self._check_final_length_budget(completed)
        body = self._assemble_publication_body(
            self.repo, self.manifest, source_path, completed
        )
        body = self._prepare_publication_presentation(body)
        body = self._resolve_publication_citation_validation(body)
        numbering_issues = heading_numbering_issues(body)
        if numbering_issues:
            raise ValueError(
                "reader-facing chapter numbering is inconsistent after assembly: "
                + "; ".join(numbering_issues[:8])
            )
        final_root = Path("public/final/scholarly_rendering")
        numbering_audit = final_root / "heading_numbering_audit.json"
        body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        heading_review_paths = sorted(
            (self.repo.root / "public/scholarly_rendering/sections").glob(
                "SR-*/heading_review_*.json"
            )
        )
        reviewer_finding_count = sum(
            len(json.loads(path.read_text(encoding="utf-8")).get("findings", []))
            for path in heading_review_paths
        )
        if (self.repo.root / numbering_audit).exists():
            frozen_audit = json.loads((self.repo.root / numbering_audit).read_text(encoding="utf-8"))
            if frozen_audit.get("assembled_markdown_sha256") != body_digest:
                raise ValueError("frozen heading-numbering audit conflicts with current assembled Markdown")
        else:
            self.repo.docs.write_once(numbering_audit, json.dumps({
                "status": "PASS",
                "assembled_markdown_sha256": body_digest,
                "chapter_and_subsection_numbering_issues": [],
                "citation_group_heading_review_round_count": len(heading_review_paths),
                "citation_group_heading_finding_count": reviewer_finding_count,
            }, indent=2, ensure_ascii=False))
        formats = set(self.manifest["rendering_output_formats"])
        md_path: str | None = None
        html_path: str | None = None
        latex_path: str | None = None
        pdf_path: str | None = None
        if "md" in formats or "pdf" in formats or "html" in formats:
            relative = final_root / "scholarly_review.md"
            if not (self.repo.root / relative).exists():
                self.repo.docs.write_once(relative, body)
            md_path = str(relative)
        if "html" in formats:
            relative = final_root / "scholarly_review.html"
            if not (self.repo.root / relative).exists():
                self.repo.docs.write_once(
                    relative,
                    render_academic_review_html(body, meeting_id=self.repo.meeting_id),
                )
            html_path = str(relative)
        if "latex" in formats:
            relative = final_root / "scholarly_review.tex"
            if not (self.repo.root / relative).exists():
                self.repo.docs.write_once(relative, self._latex(body))
            latex_path = str(relative)
        if "pdf" in formats:
            relative = final_root / "scholarly_review.pdf"
            if not (self.repo.root / relative).exists():
                pdf, font_path = render_academic_review_pdf(body, meeting_id=self.repo.meeting_id)
                validate_pdf(pdf)
                self.repo.docs.write_once(relative, pdf)
                font = Path(font_path)
                self.repo.docs.write_once(
                    final_root / "scholarly_review.pdf.provenance.json",
                    json.dumps(
                        {
                            "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                            "font_filename": font.name,
                            "font_sha256": hashlib.sha256(font.read_bytes()).hexdigest(),
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                )
            pdf_path = str(relative)
        appendix_relative = final_root / "rendering_appendix.md"
        if not (self.repo.root / appendix_relative).exists():
            self.repo.docs.write_once(
                appendix_relative,
                self._appendix(source_path, plan, completed),
            )
        result = ScholarlyRenderingResult(
            meeting_id=self.repo.meeting_id,
            status="HANDOFF_READY",
            section_count=len(plan.assignments),
            completed_section_count=len(completed),
            source_markdown_path=str(source_path.relative_to(self.repo.root)),
            final_markdown_path=md_path,
            final_html_path=html_path,
            final_latex_path=latex_path,
            final_pdf_path=pdf_path,
            appendix_path=str(appendix_relative),
            next_phase=MeetingPhase.HANDOFF_READY,
        )
        if md_path:
            ensure_visible_link(self.repo.root, link_name="SCHOLARLY_REVIEW.md", target_relative=md_path)
            ensure_visible_link(self.repo.root, link_name="FINAL_REPORT.md", target_relative=md_path, replace_symlink=True)
        if html_path:
            ensure_visible_link(self.repo.root, link_name="SCHOLARLY_REVIEW.html", target_relative=html_path)
            ensure_visible_link(self.repo.root, link_name="FINAL_REPORT.html", target_relative=html_path, replace_symlink=True)
        if latex_path:
            ensure_visible_link(self.repo.root, link_name="SCHOLARLY_REVIEW.tex", target_relative=latex_path)
            ensure_visible_link(self.repo.root, link_name="FINAL_REPORT.tex", target_relative=latex_path, replace_symlink=True)
        if pdf_path:
            ensure_visible_link(self.repo.root, link_name="SCHOLARLY_REVIEW.pdf", target_relative=pdf_path)
            ensure_visible_link(self.repo.root, link_name="FINAL_REPORT.pdf", target_relative=pdf_path, replace_symlink=True)
        lineage_path = self.repo.root / "public/continuation/lineage.json"
        provisional_source = (
            lineage_path.is_file()
            and json.loads(lineage_path.read_text(encoding="utf-8")).get("source_status")
            == "PROVISIONAL_UNCERTIFIED"
        )
        ensure_visible_link(
            self.repo.root,
            link_name="SOURCE_DRAFT.md" if provisional_source else "ORIGINAL_REPORT.md",
            target_relative=str(source_path.relative_to(self.repo.root)),
        )
        ensure_visible_link(
            self.repo.root,
            link_name="RENDERING_APPENDIX.md",
            target_relative=str(appendix_relative),
        )
        if not self._has_event("SCHOLARLY_RENDERING_PUBLISHED"):
            self.repo.events.append(
                "SCHOLARLY_RENDERING_PUBLISHED",
                result.model_dump(mode="json"),
                actor="CHAIR",
            )
        # This completion marker is intentionally last.  Its presence certifies
        # that publications, root links, provenance, and the event all exist.
        self.repo.docs.write_once(
            "public/scholarly_rendering/execution_result.json",
            result.model_dump_json(indent=2),
        )
        return result

    def _check_final_length_budget(
        self, completed: list[tuple[RenderingAssignment, str]]
    ) -> None:
        budget_path = self.repo.root / "public/scholarly_rendering/body_length_budget.json"
        if not budget_path.is_file():
            return
        budget = json.loads(budget_path.read_text(encoding="utf-8"))
        counted = {
            item["section_id"] for item in budget["sections"]
            if item["counts_toward_body_target"]
        }
        actual = sum(
            self._count_body_characters(markdown)
            for assignment, markdown in completed if assignment.section_id in counted
        )
        outcome = {
            "target_characters": budget["target_characters"],
            "actual_body_characters": actual,
            "difference_from_target_characters": actual - budget["target_characters"],
            "policy": "ADVISORY_ONLY; NO_HARD_LIMIT; FINAL_LENGTH_NOT_GUARANTEED",
            "measurement": budget["measurement"],
        }
        relative = Path("public/scholarly_rendering/body_length_outcome.json")
        outcome_path = self.repo.root / relative
        if outcome_path.exists():
            frozen = json.loads(outcome_path.read_text(encoding="utf-8"))
            # Keep historical advisory/variance metadata for the same frozen body.
            if (frozen.get("actual_body_characters") != actual
                    or frozen.get("target_characters") != budget["target_characters"]):
                raise ValueError("frozen scholarly rendering length outcome conflicts with the publication")
        else:
            self.repo.docs.write_once(relative, json.dumps(outcome, indent=2, ensure_ascii=False))
        progress = getattr(getattr(self, "engine", None), "progress", None)
        if progress is not None:
            progress.info(
                f"重绘正文实测 {actual:,} 个非空白字符；建议篇幅为 "
                f"{budget['target_characters']:,} 个字符。建议仅供参考，不限制出版，也不保证最终长度。"
            )

    @staticmethod
    def _assemble_publication_body(
        repo: MeetingRepository,
        manifest: dict,
        source_path: Path,
        completed: list[tuple[RenderingAssignment, str]],
    ) -> str:
        """Add reader-facing navigation without changing any frozen section prose.

        Rendering assignments are deliberately small, so the Chair may omit a
        document-level heading from each approved section. The publication
        layer must supply those headings from frozen metadata instead of
        asking the models to repeat or re-approve the prose.
        """
        language = manifest.get("rendering_language", "zh")
        default_title = {
            "zh": "文献综述", "en": "Literature review", "fr": "Revue de littérature",
        }.get(language, "Literature review")
        directory_label = {
            "zh": "目录", "en": "Contents", "fr": "Sommaire",
        }.get(language, "Contents")
        source_text = source_path.read_text(encoding="utf-8")
        document_title = ScholarlyRenderingRunner._first_document_title(source_text) or default_title
        module_titles: dict[str, str] = {}
        for line in source_text.splitlines():
            match = re.match(
                r"^#{1,6}\s+(?:\d+[.)]\s*)?(RM-\d{2})\s+(.+?)\s*$",
                line, flags=re.IGNORECASE,
            )
            if match:
                module_titles.setdefault(
                    match.group(1).upper(),
                    ScholarlyRenderingRunner._reader_heading(match.group(2)),
                )
        group_labels = {
            "zh": {"references": "参考文献", "unresolved": "未解决问题与知识状态"},
            "en": {"references": "References", "unresolved": "Unresolved questions and evidence status"},
            "fr": {"references": "Références", "unresolved": "Questions non résolues et état des connaissances"},
        }.get(language, {"references": "References", "unresolved": "Unresolved questions"})
        groups: list[dict] = []
        for index, (assignment, frozen_text) in enumerate(completed, start=1):
            draft_path = (
                repo.root / "public/scholarly_rendering/sections"
                / assignment.section_id / "chair_redraw.json"
            )
            title = assignment.title
            if draft_path.is_file():
                title = RenderedSection.model_validate_json(
                    draft_path.read_text(encoding="utf-8")
                ).title
            title = ScholarlyRenderingRunner._reader_heading(title) or f"{default_title} {index}"
            module = re.search(r"\bRM-\d{2}\b", assignment.title, flags=re.IGNORECASE)
            if module:
                key = module.group().upper()
                group_title = module_titles.get(key) or title
            elif re.search(r"引证文献|参考文献|\breferences\b|\bbibliography\b", assignment.title, re.IGNORECASE):
                key = "references"
                group_title = group_labels[key]
            elif re.search(r"未解决问题|知识状态|\bunresolved\b", assignment.title, re.IGNORECASE):
                key = "unresolved"
                group_title = group_labels[key]
            else:
                key = assignment.section_id
                group_title = title
            if not groups or groups[-1]["key"] != key:
                groups.append({"key": key, "title": group_title, "sections": []})
            groups[-1]["sections"].append((title, frozen_text))
        parts = [
            f"# {document_title}",
            f"## {directory_label}",
            "\n".join(
                f"{index}. {group['title']}"
                for index, group in enumerate(groups, start=1)
            ),
        ]
        for group_index, group in enumerate(groups, start=1):
            parts.append(f"## {group_index}. {group['title']}")
            for section_index, (title, frozen_text) in enumerate(group["sections"], start=1):
                direct_body = len(group["sections"]) == 1 and title == group["title"]
                text = ScholarlyRenderingRunner._nest_publication_headings(
                    frozen_text, minimum_level=3 if direct_body else 4
                )
                if not direct_body:
                    parts.append(f"### {group_index}.{section_index}. {title}")
                parts.append(text)
        module_chapters = {
            group["key"]: index for index, group in enumerate(groups, start=1)
            if re.fullmatch(r"RM-\d{2}", group["key"])
        }
        return replace_internal_references(
            "\n\n".join(parts).strip() + "\n", module_chapters, language=language
        )

    @staticmethod
    def _prepare_publication_presentation(markdown: str) -> str:
        return normalize_publication_headings(
            convert_intro_headings(
                split_inline_enumerations(sort_numeric_citations(markdown))
            )
        )

    @staticmethod
    def _first_document_title(markdown: str) -> str | None:
        fence: tuple[str, int] | None = None
        for line in markdown.splitlines():
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if marker:
                run = marker.group(1)
                if fence is None:
                    fence = (run[0], len(run))
                elif run[0] == fence[0] and len(run) >= fence[1]:
                    fence = None
                continue
            if fence is None:
                heading = re.match(r"^#\s+(.+?)\s*$", line)
                if heading:
                    return heading.group(1).strip()
        return None

    @staticmethod
    def _reader_heading(title: str) -> str:
        # Only presentation labels are cleaned. Source prose and its internal
        # identifiers remain byte-for-byte in the frozen section artifacts.
        cleaned = re.sub(r"\b(?:RM|SR)-\d{2,3}\b", "", title, flags=re.IGNORECASE)
        cleaned = re.sub(r"\brecord_id\b", "", cleaned, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", cleaned).strip(" ：:—-· ")

    @staticmethod
    def _nest_publication_headings(markdown: str, *, minimum_level: int = 3) -> str:
        """Keep section-local heading relationships below the chapter level."""
        lines = markdown.strip().splitlines()
        headings: list[tuple[int, int, str]] = []
        fence: tuple[str, int] | None = None
        for index, line in enumerate(lines):
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if marker:
                run = marker.group(1)
                if fence is None:
                    fence = (run[0], len(run))
                elif run[0] == fence[0] and len(run) >= fence[1]:
                    fence = None
                continue
            if fence is not None:
                continue
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if heading:
                headings.append((index, len(heading.group(1)), heading.group(2)))
        if headings:
            shift = max(0, minimum_level - min(level for _, level, _ in headings))
            for index, level, title in headings:
                target = level + shift
                lines[index] = (
                    f"{'#' * target} {title}" if target <= 6 else f"**{title}**"
                )
        return "\n".join(lines).strip()

    def _has_event(self, event_type: str) -> bool:
        path = self.repo.events.path
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as handle:
            return any(
                json.loads(line).get("event_type") == event_type
                for line in handle
                if line.strip()
            )

    def _resolve_publication_citation_validation(self, body: str) -> str:
        try:
            self._validate_numeric_citation_mapping(body)
            return body
        except ValueError as exc:
            issue = HumanConsultationIssue(
                issue_id="HC-SR-PUBLICATION-CITATIONS",
                meeting_id=self.repo.meeting_id,
                reason_code="SCHOLARLY_PUBLICATION_CITATION_INTEGRITY_WARNING",
                stage="SCHOLARLY_RENDERING_PUBLICATION",
                question=(
                    "最终出版稿的数字引文与参考文献表存在确定性不一致。"
                    "请选择保留当前正文并公开警告，或由 Human 提供完整替代表述。\n\n"
                    f"校验结果: {exc}"
                ),
                options=["PUBLISH_WITH_CITATION_WARNING", "USE_HUMAN_WORDING"],
                affected_items=["FINAL_PUBLICATION"],
            )
            resolution = self._open_integrity_consultation(issue)
            if resolution.decision == "USE_HUMAN_WORDING":
                if resolution.human_wording is None:
                    raise ValueError("Human publication resolution is missing wording")
                return resolution.human_wording.strip() + "\n"
            warning_relative = Path(
                "public/final/scholarly_rendering/citation_integrity_warning.json"
            )
            if not (self.repo.root / warning_relative).exists():
                self.repo.docs.write_once(
                    warning_relative,
                    json.dumps(
                        {
                            "status": "PUBLISHED_WITH_HUMAN_ACCEPTED_WARNING",
                            "validation_error": str(exc),
                            "body_sha256": hashlib.sha256(
                                body.encode("utf-8")
                            ).hexdigest(),
                            "consultation_id": issue.issue_id,
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                )
            return (
                "> **Citation integrity warning:** Human authorized publication despite this "
                f"unresolved deterministic check: {exc}\n\n{body}"
            )

    @staticmethod
    def _validate_numeric_citation_mapping(markdown: str) -> None:
        heading = re.search(
            r"(?im)^#{1,6}\s+(references|bibliography|works cited|参考文献|文献)\s*$",
            markdown,
        )
        # Without an explicit reference-list section, bracketed numbers can be
        # years, metrics, or domain notation.  They are not safely classifiable
        # as numeric citations by a deterministic parser.
        if heading is None:
            return
        body = markdown[: heading.start()]
        cited: set[int] = set()
        for match in re.finditer(r"\[([0-9]+(?:\s*[,;\-–]\s*[0-9]+)*)\]", body):
            value = match.group(1)
            for part in re.split(r"\s*[,;]\s*", value):
                range_match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
                if range_match:
                    start, end = map(int, range_match.groups())
                    if end < start or end - start > 1000:
                        raise ValueError(f"invalid numeric citation range [{part}]")
                    cited.update(range(start, end + 1))
                else:
                    cited.add(int(part))
        if not cited:
            return
        references = markdown[heading.end() :]
        entries = [
            int(next(group for group in match.groups() if group is not None))
            for match in re.finditer(
                r"(?m)^\s*(?:[-*]\s*)?(?:\[(\d+)\]|(\d+)[.)])\s+",
                references,
            )
        ]
        if len(entries) != len(set(entries)):
            raise ValueError("reference list contains duplicate numeric identifiers")
        if entries and sorted(entries) != list(range(1, max(entries) + 1)):
            raise ValueError("reference-list numeric identifiers must be continuous from 1")
        missing = sorted(cited - set(entries))
        if missing:
            raise ValueError(f"numeric citations have no reference entry: {missing}")

    def _ordered_science(self) -> list[dict]:
        by_model = {
            f"{record['runtime']['provider_id']}:{record['runtime']['model_id']}": record
            for record in self.science
        }
        order = self.manifest.get("rendering_science_order") or list(by_model)
        if set(order) != set(by_model):
            raise ValueError("frozen science-review order does not match the science panel")
        return [by_model[key] for key in order]

    def _resolved_runtime_key(self, record: dict) -> tuple[str, str]:
        return current_runtime_for(self.repo, str(record["representative_id"]))

    def _assignment_text(
        self, assignment: RenderingAssignment, blocks: list[SourceBlock]
    ) -> str:
        by_id = {block.block_id: block.body_markdown for block in blocks}
        return "\n\n".join(by_id[item] for item in assignment.source_block_ids)

    def _public_config(self) -> dict:
        config = {
            "language": self.manifest["rendering_language"],
            "strict_academic_skeleton": self.manifest["rendering_academic_skeleton"],
            "full_abstract": self.manifest["rendering_full_abstract"],
            "section_abstracts": self.manifest["rendering_section_abstracts"],
            "segmentation_aggressiveness_1_to_5": self.manifest["rendering_segmentation"],
            "liveliness_1_to_5": self.manifest["rendering_liveliness"],
            "reader_style_policy": reader_style_policy({
                "signposting_1_to_5": 4,
                "segmentation_1_to_5": self.manifest["rendering_segmentation"],
                "liveliness_1_to_5": self.manifest["rendering_liveliness"],
            }),
            "single_language_only_no_translation_appendix": True,
        }
        query_limit = self.manifest.get("rendering_final_science_query_limit")
        if query_limit is not None:
            config["final_science_queries_per_reviewer_per_section"] = query_limit
        return config

    def _rendering_writing_context(
        self, assignment: RenderingAssignment, source_path: Path, original: str
    ) -> dict:
        plan_path = self.repo.root / "public/scholarly_rendering/rendering_plan.json"
        if not plan_path.is_file():
            return {"allowed_reference_numbers_in_source": []}
        plan = RenderingPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        index = next(
            (i for i, item in enumerate(plan.assignments) if item.section_id == assignment.section_id),
            None,
        )
        if index is None:
            return {"allowed_reference_numbers_in_source": []}
        assignments = plan.assignments
        module = re.search(r"\bRM-\d{2}\b", assignment.title, re.I)
        module_key = module.group().upper() if module else None
        def same_module(other: RenderingAssignment | None) -> bool:
            return bool(other and module_key and re.search(re.escape(module_key), other.title, re.I))
        previous = assignments[index - 1] if index else None
        following = assignments[index + 1] if index + 1 < len(assignments) else None
        starts = not same_module(previous)
        ends = not same_module(following)
        numbers: set[int] = set()
        for citation in re.finditer(r"\[([0-9]+(?:\s*[,;\-–]\s*[0-9]+)*)\]", original):
            for part in re.split(r"\s*[,;]\s*", citation.group(1)):
                span = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
                if span:
                    first, last = map(int, span.groups())
                    if first <= last and last - first <= 1000:
                        numbers.update(range(first, last + 1))
                else:
                    numbers.add(int(part))
        previous_excerpt = None
        if previous:
            previous_path = (
                self.repo.root / "public/scholarly_rendering/sections"
                / previous.section_id / "final.md"
            )
            if previous_path.is_file():
                previous_excerpt = previous_path.read_text(encoding="utf-8")[-600:]
        next_excerpt = None
        if following:
            blocks = {block.block_id: block for block in self._source_blocks(source_path.read_text(encoding="utf-8"))}
            if following.source_block_ids and following.source_block_ids[0] in blocks:
                next_excerpt = blocks[following.source_block_ids[0]].body_markdown[:600]
        budget_context = None
        budget_path = self.repo.root / "public/scholarly_rendering/body_length_budget.json"
        if budget_path.is_file():
            budget = json.loads(budget_path.read_text(encoding="utf-8"))
            allocation = next(
                (item for item in budget["sections"] if item["section_id"] == assignment.section_id), None
            )
            if allocation is not None:
                budget_context = {
                    "total_target_characters": budget["target_characters"],
                    "policy": "ADVISORY_ONLY; NO_HARD_LIMIT; FINAL_LENGTH_NOT_GUARANTEED",
                    "this_section_target_characters": allocation["target_characters"],
                    "counts_toward_body_target": allocation["counts_toward_body_target"],
                    "measurement": budget["measurement"],
                    "instruction": "长度是柔性目标；不得删去结论、引文对应、条件、假设或不确定性以凑字数，也不得为凑下限填充重复内容。",
                }
        return {
            "global_outline": [self._reader_heading(item.title)[:140] for item in assignments],
            "block_number": index + 1,
            "total_blocks": len(assignments),
            "position_in_chapter": "START_AND_END" if starts and ends else
                "START" if starts else "END" if ends else "MIDDLE",
            "previous_block_title": self._reader_heading(previous.title) if previous else None,
            "previous_approved_ending": previous_excerpt,
            "next_block_title": self._reader_heading(following.title) if following else None,
            "next_source_opening": next_excerpt,
            "allowed_reference_numbers_in_source": sorted(numbers),
            "numerical_and_statistical_control": "保留 source_markdown 中每个数字、单位、分母、统计口径和时间限定；不得凭相邻编号增加引文。",
            **({"body_length_budget": budget_context} if budget_context is not None else {}),
        }

    def _chair_render_system(self) -> str:
        return (
            "在主席兼主渲染者权限内，将指定范围的原文重绘为真正的学术文献综述，"
            "而不是法规、政策、治理文书或 ENSEMBLE 程序记录。删去无助于读者理解的内部条款、动议和程序引用。"
            "改进结构及学术表达，同时保留所有科学结论、限制、不确定性、引文关系、未解决状态和假设。"
            "不得新增结论、暗中提高证据强度、翻译成额外语言或暴露内部代理身份。"
                "把主科学判断放在读者容易找到的位置；用完整句法表达对象、条件、比较和推论，"
            "只在原文已有逻辑关系时补足连接词。第一次完整说明重要证据边界，后文无新增边界时"
            "简短指回；不可把不确定性或异议压掉。实现细节、来源状态和开放问题与主论证分层呈现，"
            "但改变结论范围的限制仍须在正文可见。复杂论证比普通段落提供更积极的路标，"
            "说明当前比较什么、为何进入下一步；路标不得创造新的科学解释。"
            "辅助来源状态或次要实现细节可写成单行 > **来源状态**：说明 或"
            " > **实现说明**：说明，HTML 会把它呈现为小号[注]侧栏，PDF 则保留原文。"
            "不能把改变结论范围的关键限制只藏在注释里。"
            + (
                "若给出建议正文篇幅，只作为软性参考；不得为凑长度牺牲科学完整性，也不要求达到目标。"
                if getattr(self, "manifest", {}).get("rendering_target_body_characters") is not None else ""
            )
            + _READER_FACING_IDENTIFIER_RULE
            + _SCHOLARLY_PRESENTATION_RULE
            + _SCHOLARLY_QUANTITATIVE_PRESERVATION_RULE
        )

    def _evidence_index(
        self, participant_id: str, stage: str, *, max_chars: int = 60_000
    ) -> list[dict]:
        packet_root = self.repo.root / "public/research/evidence_packets"
        entries: list[dict] = []
        paths = sorted(packet_root.glob("*.json")) if packet_root.exists() else []
        for path in paths:
            try:
                raw = path.read_bytes()
                packet = EvidencePacket.model_validate_json(raw)
            except Exception:
                continue
            candidate = {
                "packet_id": packet.packet_id,
                "claim": packet.normalized_claim,
                "status": packet.knowledge_status.value,
                "sources": [
                    {
                        "source_id": source.source_id,
                        "title": source.title,
                        "url": source.url,
                        "publication_year": source.publication_year,
                        "evidence_use_class": source.evidence_use_class.value,
                    }
                    for source in packet.sources
                ],
            }
            projected = json.dumps(
                [*entries, candidate], ensure_ascii=False, separators=(",", ":")
            )
            if len(projected) > max_chars or len(entries) >= 300:
                entries.append(
                    {
                        "truncated": True,
                        "included_packet_count": len(entries),
                        "available_packet_count": len(paths),
                        "character_budget": max_chars,
                    }
                )
                break
            self._record_read(
                participant_id,
                path,
                stage,
                context_role="PUBLIC_EVIDENCE_PACKET",
            )
            entries.append(candidate)
        return entries

    def _invoke(
        self,
        participant_id: str,
        *,
        stage: str,
        schema: type[BaseModel],
        system: str,
        user: dict,
    ) -> BaseModel:
        if schema.__name__ in {"RenderedSection", "ChairScienceRevision"}:
            rendering = getattr(self, "manifest", {})
            system += reader_facing_prose_contract({
                "signposting_1_to_5": 4,
                "segmentation_1_to_5": rendering.get("rendering_segmentation"),
                "liveliness_1_to_5": rendering.get("rendering_liveliness"),
            })
        user_text = (
            json.dumps(user, indent=2, ensure_ascii=False)
            + "\n\n只返回一个符合以下结构的 JSON 对象：\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        response = self.engine.find_recorded_response(
            participant_id, system_text=system, user_text=user_text, stage=stage
        ) or self.engine.invoke_participant(
            participant_id,
            system_text=system,
            user_text=user_text,
            stage=stage,
            max_output_tokens=self.max_output_tokens,
        )
        return self.engine.validate_structured_response(
            participant_id,
            response=response,
            schema_model=schema,
            stage=stage,
            max_output_tokens=self.max_output_tokens,
            semantic_requirement="保留冻结的实质内容，只执行当前重绘或审阅动作。",
        )

    def _restore_pre_limit_chair_advice(
        self, *, stage: str, system: str, user: dict,
    ) -> ChairScienceIssueAdvice | None:
        """Reuse a frozen response whose only old defect was an excerpt cap.

        The current JSON schema no longer contains the arbitrary 1,600-character
        bound, so ordinary exact-request replay cannot see a response recorded
        under the previous schema. Match the frozen substantive input instead;
        never alter or replace the original exchange.
        """

        root = self.repo.root / "governance_private/provider_exchanges"
        if not root.is_dir():
            return None
        payload = json.dumps(user, indent=2, ensure_ascii=False)
        schema_separator = "\n\n只返回一个符合以下结构的 JSON 对象：\n"
        inherited = getattr(getattr(self, "engine", None), "_with_inherited_advisory_context", None)
        expected_system = inherited(system) if callable(inherited) else system
        candidates: list[tuple[int, ChairScienceIssueAdvice]] = []
        for path in root.glob("X-*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                request = record["request"]
                recorded_system = request["system_text"]
                if (
                    record.get("participant_id") != "CHAIR"
                    or record.get("stage") != stage
                    or not request["user_text"].startswith(payload + schema_separator)
                    or not (
                        recorded_system == expected_system
                        or recorded_system.startswith(
                            expected_system + "\n\nMEETING-SPECIFIC HUMAN PROCEDURAL RULING"
                        )
                    )
                ):
                    continue
                response = ChairScienceIssueAdvice.model_validate(
                    parse_json_object(record["response"]["text"])
                )
            except (KeyError, TypeError, ValueError, OSError):
                continue
            candidates.append((path.stat().st_mtime_ns, response))
        if not candidates:
            return None
        # The earliest complete response is the original Chair submission;
        # later schema-only repair attempts are not an alternative opinion.
        return min(candidates, key=lambda item: item[0])[1]

    def _record_read(
        self,
        participant_id: str,
        path: Path,
        stage: str,
        *,
        context_role: str = "PUBLIC_SOURCE_REPORT",
    ) -> None:
        raw = path.read_bytes()
        self.access_log.append(
            "REPRESENTATIVE_FILE_READ",
            {
                "representative_id": participant_id,
                "stage": stage,
                "context_role": context_role,
                "compartment": "PUBLIC",
                "path": str(path.relative_to(self.repo.root)),
                "decision": "AUTHORIZED_AND_READ",
                "byte_count": len(raw),
                "content_sha256": hashlib.sha256(raw).hexdigest(),
            },
            actor=participant_id,
        )

    def _appendix(
        self,
        source_path: Path,
        plan: RenderingPlan,
        completed: list[tuple[RenderingAssignment, str]],
    ) -> str:
        source_runtime_path = self.repo.root / "public/continuation/source_runtime_summary.json"
        lineage_path = self.repo.root / "public/continuation/lineage.json"
        lineage = (
            json.loads(lineage_path.read_text(encoding="utf-8"))
            if lineage_path.is_file() else {}
        )
        source_runtime = (
            json.loads(source_runtime_path.read_text(encoding="utf-8"))
            if source_runtime_path.exists()
            else {}
        )
        current_models = sorted(
            {
                f"{provider}:{model}"
                for provider, model in self.manifest.get("selected_models", [])
            }
            | {
                f"{self.manifest['chair_model'][0]}:{self.manifest['chair_model'][1]}",
                f"{self.manifest['research_model'][0]}:{self.manifest['research_model'][1]}",
            }
            | {f"{provider}:{model}" for provider, model in replacement_model_pairs(self.repo)}
        )
        justifications: list[str] = []
        for assignment, _ in completed:
            redraw_path = (
                self.repo.root
                / "public/scholarly_rendering/sections"
                / assignment.section_id
                / "chair_redraw.json"
            )
            if redraw_path.exists():
                redraw = RenderedSection.model_validate_json(
                    redraw_path.read_text(encoding="utf-8")
                )
                justifications.append(
                    f"### {assignment.section_id} · {assignment.title}\n\n"
                    f"{redraw.justification}\n\n"
                    + (
                        "Removed as reader-irrelevant:\n"
                        + "\n".join(
                            f"- {item}" for item in redraw.removed_irrelevant_material
                        )
                        if redraw.removed_irrelevant_material
                        else "No reader-irrelevant material was declared removed."
                    )
                )
        return (
            "# Scholarly Rendering Appendix\n\n"
            "> This appendix is reader-visible but is not part of the default report body.\n\n"
            f"## Source\n\n- Frozen source: `{source_path.relative_to(self.repo.root)}`\n"
            f"- Source status: `{lineage.get('source_status', 'COMPLETED')}`"
            " (PROVISIONAL_UNCERTIFIED means the parent meeting did not publish or pass its final readability check).\n"
            f"- Source SHA-256: `{hashlib.sha256(source_path.read_bytes()).hexdigest()}`\n\n"
            "## Rendering configuration\n\n```json\n"
            + json.dumps(self._public_config(), indent=2, ensure_ascii=False)
            + "\n```\n\n## Models and reasoning controls\n\n"
            + "- Source-meeting model union: "
            + ", ".join(source_runtime.get("models", []))
            + "\n- Source Representative effort: "
            + str(source_runtime.get("representative_reasoning_effort", "unavailable"))
            + "\n- Source Chair effort: "
            + str(source_runtime.get("chair_reasoning_effort", "unavailable"))
            + "\n- Rendering-meeting model union: "
            + ", ".join(current_models)
            + "\n- Rendering reviewer effort: "
            + str(self.manifest.get("representative_reasoning_effort", "default"))
            + "\n- Rendering Chair effort: "
            + str(self.manifest.get("chair_reasoning_effort", "default"))
            + "\n\n## Chair section plan\n\n```json\n"
            + plan.model_dump_json(indent=2)
            + "\n```\n\n"
            + "## Chair rendering justifications\n\n"
            + "\n\n".join(justifications)
            + "\n\n"
            + "## Section outputs\n\n"
            + "\n".join(
                f"- {assignment.section_id}: {assignment.title} ({len(text)} characters)"
                for assignment, text in completed
            )
            + "\n\n> Reviewer identities and full trajectories remain in the audit compartments.\n"
        )

    @staticmethod
    def _latex(markdown: str) -> str:
        return markdown_to_latex(markdown)


def reissue_structured_scholarly_publication(repo: MeetingRepository) -> dict:
    """Issue an additive presentation edition of a frozen rendering meeting.

    This does not rerun models or replace the original final artifacts. The
    approved section prose remains in its original immutable files; only
    document title, contents, and heading levels are assembled anew.
    """
    result_path = repo.root / "public/scholarly_rendering/execution_result.json"
    if not result_path.is_file():
        raise ValueError("学术重绘会议尚未完成；不能重新组装最终报告")
    result = ScholarlyRenderingResult.model_validate_json(
        result_path.read_text(encoding="utf-8")
    )
    if result.status != "HANDOFF_READY":
        raise ValueError("学术重绘会议尚未达到可交付状态")
    human_wording_path = (
        repo.root / "human_private/consultations"
        / "HC-SR-PUBLICATION-CITATIONS.resolution.json"
    )
    if human_wording_path.is_file() and json.loads(
        human_wording_path.read_text(encoding="utf-8")
    ).get("decision") == "USE_HUMAN_WORDING":
        raise ValueError("最终全文由 Human 逐字指定；不能自动重组其冻结表述")
    plan = RenderingPlan.model_validate_json(
        (repo.root / "public/scholarly_rendering/rendering_plan.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        repo.docs.read_text("identity_private/meeting_manifest.json")
    )
    completed: list[tuple[RenderingAssignment, str]] = []
    section_hashes: list[dict[str, str]] = []
    for assignment in plan.assignments:
        path = (
            repo.root / "public/scholarly_rendering/sections"
            / assignment.section_id / "final.md"
        )
        frozen = path.read_bytes()
        completed.append((assignment, frozen.decode("utf-8")))
        section_hashes.append({
            "section_id": assignment.section_id,
            "sha256": hashlib.sha256(frozen).hexdigest(),
        })
    source_path = repo.root / result.source_markdown_path
    body = ScholarlyRenderingRunner._assemble_publication_body(
        repo, manifest, source_path, completed
    )
    body = ScholarlyRenderingRunner._prepare_publication_presentation(body)
    original_path = repo.root / "public/final/scholarly_rendering/scholarly_review.md"
    if original_path.is_file():
        original = original_path.read_text(encoding="utf-8")
        if original.startswith("> **Citation integrity warning:**"):
            warning, _separator, _remaining = original.partition("\n\n")
            body = warning + "\n\n" + body
    ScholarlyRenderingRunner._validate_numeric_citation_mapping(body)

    final_root = Path("public/final/scholarly_rendering")

    def write_or_verify(relative: Path, content: str | bytes) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        path = repo.root / relative
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f"结构化再出版文件与现存版本冲突：{relative}")
        else:
            repo.docs.write_once(relative, content)

    markdown_relative = final_root / "scholarly_review_structured_v7.md"
    write_or_verify(markdown_relative, body)
    published: dict[str, str] = {"markdown_path": str(markdown_relative)}
    latex_relative = final_root / "scholarly_review_structured_v7.tex"
    write_or_verify(latex_relative, ScholarlyRenderingRunner._latex(body))
    published["latex_path"] = str(latex_relative)
    if result.final_pdf_path:
        pdf_relative = final_root / "scholarly_review_structured_v7.pdf"
        pdf_bytes, font_path = render_academic_review_pdf(body, meeting_id=repo.meeting_id)
        validate_pdf(pdf_bytes)
        write_or_verify(pdf_relative, pdf_bytes)
        provenance_relative = final_root / "scholarly_review_structured_v7.pdf.provenance.json"
        write_or_verify(provenance_relative, json.dumps({
            "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "font_filename": font_path.name,
            "font_sha256": hashlib.sha256(font_path.read_bytes()).hexdigest(),
            "markdown_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }, indent=2, ensure_ascii=False))
        published["pdf_path"] = str(pdf_relative)
    edition = {
        "meeting_id": repo.meeting_id,
        "edition": "STRUCTURED_PRESENTATION_V7",
        "policy": "PRESENTATION_ONLY_HIERARCHY_INLINE_LISTS_SORTED_CITATIONS_AND_INTERNAL_REFERENCES",
        "original_result_path": "public/scholarly_rendering/execution_result.json",
        "source_markdown_path": result.source_markdown_path,
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "frozen_sections": section_hashes,
        "markdown_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        **published,
    }
    edition_relative = final_root / "scholarly_review_structured_v7.manifest.json"
    write_or_verify(edition_relative, json.dumps(edition, indent=2, ensure_ascii=False))
    ensure_visible_link(
        repo.root, link_name="SCHOLARLY_REVIEW_STRUCTURED.md",
        target_relative=markdown_relative,
        replace_symlink=True,
    )
    ensure_visible_link(
        repo.root, link_name="FINAL_REPORT.md",
        target_relative=markdown_relative,
        replace_symlink=True,
    )
    if "latex_path" in published:
        ensure_visible_link(
            repo.root, link_name="SCHOLARLY_REVIEW_STRUCTURED.tex",
            target_relative=published["latex_path"],
            replace_symlink=True,
        )
        ensure_visible_link(
            repo.root, link_name="FINAL_REPORT.tex",
            target_relative=published["latex_path"],
            replace_symlink=True,
        )
    if "pdf_path" in published:
        ensure_visible_link(
            repo.root, link_name="SCHOLARLY_REVIEW_STRUCTURED.pdf",
            target_relative=published["pdf_path"],
            replace_symlink=True,
        )
        ensure_visible_link(
            repo.root, link_name="FINAL_REPORT.pdf",
            target_relative=published["pdf_path"],
            replace_symlink=True,
        )
    return edition
