from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.domain import DeliverableType, MeetingPhase, Persona
from project_ensemble.errors import ResearchQualityControlError
from project_ensemble.orchestration.consultations import (
    HumanConsultationIssue,
    HumanConsultationService,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.research.exploration import ResearchExplorationService
from project_ensemble.research.models import FreshnessClass
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.storage.meeting import MeetingRepository


class ResearchModulePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    local_id: str = Field(min_length=1, max_length=24)
    title: str = Field(min_length=1, max_length=160)
    research_questions: list[str] = Field(min_length=1, max_length=4)
    included_scope: list[str] = Field(default_factory=list, max_length=6)
    excluded_scope: list[str] = Field(default_factory=list, max_length=6)
    required_evidence: list[str] = Field(min_length=1, max_length=6)
    related_local_ids: list[str] = Field(default_factory=list, max_length=8)


class ResearchDecompositionSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    framing_rationale: str = Field(min_length=1)
    scope_objection: str | None = Field(default=None, min_length=1, max_length=1000)
    modules: list[ResearchModulePlan] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def local_ids_are_consistent(self) -> "ResearchDecompositionSubmission":
        ids = [module.local_id for module in self.modules]
        if len(ids) != len(set(ids)):
            raise ValueError("research-module local IDs must be unique")
        known = set(ids)
        for module in self.modules:
            unknown = set(module.related_local_ids) - known
            if unknown:
                raise ValueError(f"related_local_ids reference unknown modules: {sorted(unknown)}")
        return self


class ExplorationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1)
    search_queries: list[str] = Field(min_length=1, max_length=16)
    freshness_class: FreshnessClass = FreshnessClass.VERSIONED


class ExplorationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finished: bool = False
    questions: list[ExplorationQuestion] = Field(default_factory=list, max_length=16)


class OutlineModule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    module_id: str = Field(pattern=r"^RM-[0-9]{2}$")
    title: str = Field(min_length=1, max_length=160)
    research_questions: list[str] = Field(min_length=1, max_length=5)
    included_scope: list[str] = Field(default_factory=list, max_length=8)
    excluded_scope: list[str] = Field(default_factory=list, max_length=8)
    required_evidence: list[str] = Field(min_length=1, max_length=8)
    cross_module_links: list[str] = Field(default_factory=list, max_length=10)
    source_submission_refs: list[str] = Field(min_length=1, max_length=16)


class ClusteredResearchOutline(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    report_title: str = Field(min_length=1, max_length=240)
    scope_note: str = Field(min_length=1, max_length=2000)
    modules: list[OutlineModule] = Field(min_length=1, max_length=12)
    clustering_notes: list[str] = Field(default_factory=list, max_length=24)
    scope_concern_notice: str | None = Field(default=None, min_length=1)
    proposed_disciplines: list[str] = Field(default_factory=list, max_length=20)
    article_skeleton: list[str] = Field(default_factory=list, max_length=24)
    glossary_recommended: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_descriptive_cross_module_links(cls, value: object) -> object:
        """Accept links that include a short human-readable explanation.

        The public schema asks Chair for module IDs, but models commonly return
        useful labels such as ``RM-02/RM-03 (parallel technical scope)``.  The
        explanation is not a module identifier and used to make an otherwise
        usable outline fail schema validation.  Keep the typed field as IDs,
        extracting every explicit ``RM-xx`` token while leaving strings with no
        token untouched so genuinely malformed references still fail below.
        """
        if not isinstance(value, dict):
            return value
        modules = value.get("modules")
        if not isinstance(modules, list):
            return value
        normalized_modules: list[object] = []
        for module in modules:
            if not isinstance(module, dict):
                normalized_modules.append(module)
                continue
            links = module.get("cross_module_links")
            if not isinstance(links, list):
                normalized_modules.append(module)
                continue
            normalized_links: list[object] = []
            for link in links:
                if isinstance(link, str):
                    ids = re.findall(r"RM-[0-9]{2}", link.upper())
                    if ids:
                        for module_id in ids:
                            if module_id not in normalized_links:
                                normalized_links.append(module_id)
                        continue
                normalized_links.append(link)
            normalized_module = dict(module)
            normalized_module["cross_module_links"] = normalized_links
            normalized_modules.append(normalized_module)
        normalized = dict(value)
        normalized["modules"] = normalized_modules
        return normalized

    @model_validator(mode="after")
    def module_ids_are_consistent(self) -> "ClusteredResearchOutline":
        ids = [module.module_id for module in self.modules]
        if len(ids) != len(set(ids)):
            raise ValueError("outline module IDs must be unique")
        known = set(ids)
        for module in self.modules:
            unknown = set(module.cross_module_links) - known
            if unknown:
                raise ValueError(f"cross_module_links reference unknown modules: {sorted(unknown)}")
        return self


class OutlineIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    affected_module_ids: list[str] = Field(default_factory=list, max_length=8)
    issue: str = Field(min_length=1, max_length=700)


class StructuralProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation: Literal["ADD", "REMOVE", "MERGE", "SPLIT", "REORDER", "REDEFINE"]
    target_module_ids: list[str] = Field(default_factory=list, max_length=8)
    proposed_change: str = Field(min_length=1, max_length=1200)
    rationale: str = Field(min_length=1, max_length=700)


class ResearchOutlineReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["NO_OBJECTION", "RAISE_ISSUES", "STRUCTURAL_PROPOSAL"]
    issues: list[OutlineIssue] = Field(default_factory=list, max_length=2)
    proposal: StructuralProposal | None = None

    @model_validator(mode="after")
    def action_matches_payload(self) -> "ResearchOutlineReview":
        if self.action == "NO_OBJECTION" and (self.issues or self.proposal is not None):
            raise ValueError("NO_OBJECTION cannot include issues or a proposal")
        if self.action == "RAISE_ISSUES" and not self.issues:
            raise ValueError("RAISE_ISSUES requires one or two issues")
        # A Representative may explain why a structural proposal is needed in
        # the same sealed submission. These issues support the proposal; they
        # are not a second action and must not invalidate the review.
        if self.action == "STRUCTURAL_PROPOSAL" and self.proposal is None:
            raise ValueError("STRUCTURAL_PROPOSAL requires one proposal")
        return self


class ReviewDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    representative_id: str
    disposition: Literal["ACCEPTED", "PARTIALLY_ACCEPTED", "NOT_ADOPTED", "NO_OBJECTION"]
    rationale: str = Field(min_length=1, max_length=700)


class FrozenResearchOutline(ClusteredResearchOutline):
    review_dispositions: list[ReviewDisposition]


class ArticleSkeletonRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    article_skeleton: list[str] = Field(min_length=1, max_length=24)


class LiteratureReportPlanningResult(BaseModel):
    meeting_id: str
    parent_meeting_id: str | None
    origin_type: Literal["DERIVED_DELIVERABLE", "FROM_SCRATCH"]
    planning_panel_size: int
    decomposition_submission_count: int
    outline_review_count: int
    research_module_count: int
    inherited_evidence_packet_count: int
    candidate_outline_path: str
    frozen_outline_path: str
    next_phase: MeetingPhase
    paused_reason: str | None = None


_PERSONA_ORDER = (
    Persona.SYSTEMS_INTEGRATOR,
    Persona.PRAGMATIC_MINIMALIST,
    Persona.EXPLORATORY_SYNTHESIST,
    Persona.LIBRARIAN,
)


class LiteratureReportPlanningRunner:
    """Freeze a derived report's research outline without inventing drafting policy."""

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
        exploration_service: ResearchExplorationService | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.resolver = GovernanceDocumentResolver(governance_docs)
        self.assembler = RepresentativeContextAssembler()
        self.max_output_tokens = max_output_tokens
        self.exploration_service = exploration_service
        self.exploration_enabled = bool(
            exploration_service
            and json.loads(self.repo.docs.read_text("identity_private/meeting_manifest.json"))
            .get("planning_exploration_enabled", False)
        )
        self.prompt_family = json.loads(
            self.repo.docs.read_text("identity_private/meeting_manifest.json")
        ).get("representative_prompt_family", "legacy_shared")
        self.replan_reference_enabled = bool(json.loads(
            self.repo.docs.read_text("public/meeting_manifest.json")
        ).get("planning_replan_reference_enabled", False))

    def run(self) -> LiteratureReportPlanningResult:
        manifest = json.loads(self.repo.docs.read_text("public/meeting_manifest.json"))
        if manifest.get("deliverable_type") != DeliverableType.LITERATURE_REVIEW.value:
            raise ValueError("literature-report runner requires a literature_review deliverable")
        result_relative = Path("public/literature_report/planning_result.json")
        result_path = self.repo.root / result_relative
        if result_path.exists():
            # v0.7 planning artifacts created before from-scratch support did not
            # carry origin_type.  Infer it from the immutable lineage/origin file
            # at read time; never rewrite the frozen legacy artifact.
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if "origin_type" not in payload:
                payload["origin_type"] = (
                    "DERIVED_DELIVERABLE"
                    if (self.repo.root / "public/continuation/lineage.json").exists()
                    else "FROM_SCRATCH"
                )
            return LiteratureReportPlanningResult.model_validate(payload)
        continuation_path = self.repo.root / "public/continuation/lineage.json"
        if continuation_path.exists():
            origin = json.loads(continuation_path.read_text(encoding="utf-8"))
            origin_type = "DERIVED_DELIVERABLE"
        else:
            origin = json.loads(
                self.repo.docs.read_text("public/literature_report/origin.json")
            )
            origin_type = "FROM_SCRATCH"
        task_path = self.repo.root / "public/task.json"
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        if len(registry) < 4:
            raise ValueError("literature-review planning requires at least four Representatives")

        cycle = self._planning_cycle()
        replan_notes = self._prior_replan_notes(cycle)
        panel = self._load_or_select_panel(registry, cycle=cycle)
        self.engine.status.phase = MeetingPhase.RESEARCH_PLANNING
        if self.exploration_enabled:
            self.engine.progress.status(
                MeetingPhase.RESEARCH_PLANNING,
                "Chair 正在进行规划前概览检索；原始问答与来源将公开给四名规划代表",
            )
        chair_exploration = self._run_exploration(
            "CHAIR", task_path, cycle=cycle, role="CHAIR", chair_overview=None,
            replan_notes=replan_notes,
        ) if self.exploration_enabled else None
        self.engine.status.phase = MeetingPhase.RESEARCH_PLANNING
        self.engine.progress.status(
            MeetingPhase.RESEARCH_PLANNING,
            "4 名研究规划代表正在密封提交问题拆分方案；每份最多 8 个一级模块",
        )
        submissions = self._collect_decompositions(
            panel, task_path, cycle=cycle, replan_notes=replan_notes,
            chair_exploration=chair_exploration,
        )
        self.engine.progress.status(
            MeetingPhase.RESEARCH_PLANNING,
            "本轮四份规划方案已收齐；正在归档探索资料并组装候选总纲",
        )
        released_submissions = self._release_decompositions(panel, submissions, cycle=cycle)
        if self.exploration_enabled:
            self._release_representative_exploration(panel, cycle=cycle)
        candidate_path = self._ensure_candidate_outline(
            task_path, released_submissions, cycle=cycle, replan_notes=replan_notes
        )

        self.engine.status.phase = MeetingPhase.RESEARCH_OUTLINE_REVIEW
        self.engine.progress.status(
            MeetingPhase.RESEARCH_OUTLINE_REVIEW,
            f"全体 {len(registry)} 名 Representative 限量审阅候选研究总纲",
        )
        reviews = self._collect_reviews(
            registry, task_path, candidate_path, cycle=cycle, replan_notes=replan_notes
        )
        public_reviews = self._release_reviews(registry, reviews, cycle=cycle)
        frozen_path = self._ensure_frozen_outline(
            task_path, candidate_path, public_reviews, cycle=cycle, replan_notes=replan_notes
        )
        frozen = FrozenResearchOutline.model_validate_json(frozen_path.read_text(encoding="utf-8"))

        human_decision = self._human_outline_review(cycle=cycle, outline=frozen, outline_path=frozen_path)
        if human_decision == "REJECT_AND_REPLAN":
            self.engine.progress.info(
                f"Human 未批准研究模块划分；已记录具体异议，开始第 {cycle + 1} 轮完整重做"
            )
            return self.run()
        if human_decision is None:
            self.engine.status.phase = MeetingPhase.PAUSED
            self.engine.status.paused_reason = "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
            self.engine.progress.status(
                MeetingPhase.PAUSED,
                "模块划分已形成；等待 Human 审阅（批准继续，或填写异议后彻底重做）",
            )
            return LiteratureReportPlanningResult(
                meeting_id=self.repo.meeting_id,
                parent_meeting_id=(
                    str(origin["parent_meeting_id"])
                    if origin.get("parent_meeting_id") is not None
                    else None
                ),
                origin_type=origin_type,
                planning_panel_size=len(panel),
                decomposition_submission_count=len(submissions),
                outline_review_count=len(reviews),
                research_module_count=len(frozen.modules),
                inherited_evidence_packet_count=int(origin["inherited_evidence_packet_count"]),
                candidate_outline_path=str(candidate_path.relative_to(self.repo.root)),
                frozen_outline_path=str(frozen_path.relative_to(self.repo.root)),
                next_phase=MeetingPhase.PAUSED,
                paused_reason="HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED",
            )

        self.engine.status.phase = MeetingPhase.LITERATURE_MODULE_RESEARCH
        self.engine.status.paused_reason = None
        result = LiteratureReportPlanningResult(
            meeting_id=self.repo.meeting_id,
            parent_meeting_id=(
                str(origin["parent_meeting_id"])
                if origin.get("parent_meeting_id") is not None
                else None
            ),
            origin_type=origin_type,
            planning_panel_size=len(panel),
            decomposition_submission_count=len(submissions),
            outline_review_count=len(reviews),
            research_module_count=len(frozen.modules),
            inherited_evidence_packet_count=int(origin["inherited_evidence_packet_count"]),
            candidate_outline_path=str(candidate_path.relative_to(self.repo.root)),
            frozen_outline_path=str(frozen_path.relative_to(self.repo.root)),
            next_phase=MeetingPhase.LITERATURE_MODULE_RESEARCH,
            paused_reason=None,
        )
        self.repo.docs.write_once(result_relative, result.model_dump_json(indent=2))
        self.repo.events.append(
            "LITERATURE_RESEARCH_OUTLINE_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "module_count": len(frozen.modules),
                "frozen_outline_path": str(frozen_path.relative_to(self.repo.root)),
                "next_phase": MeetingPhase.LITERATURE_MODULE_RESEARCH.value,
            },
            actor="CHAIR",
        )
        return result

    @staticmethod
    def _cycle_suffix(cycle: int) -> str:
        return "" if cycle == 1 else f"-C{cycle:03d}"

    def _planning_length_guidance(self) -> str:
        preferences_path = self.repo.root / "public/literature_report/writing_preferences.json"
        if not preferences_path.exists():
            return ""
        target = json.loads(preferences_path.read_text(encoding="utf-8")).get("target_body_characters")
        if target is None:
            return ""
        return (
            f"整篇报告正文的目标约为 {target:,} 个非空白字符，参考范围为上下浮动 20%；"
            "参考文献和独立附录不计。请据此控制模块颗粒度，但不得为了长度而遗漏任务范围。\n"
        )

    def _planning_cycle(self) -> int:
        """Return the current outline cycle, advancing after Human rejection."""

        service = HumanConsultationService(self.repo)
        cycle = 1
        for path in (self.repo.root / "human_private/consultations").glob(
            "HC-LROUTLINE-C*.issue.json"
        ):
            issue_id = path.name.removesuffix(".issue.json")
            match = re.fullmatch(r"HC-LROUTLINE-C(\d{3})(?:-S\d{3})?", issue_id)
            if match is None:
                continue
            current = int(match.group(1))
            cycle = max(cycle, current)
            resolution = service.resolution(issue_id)
            if resolution is not None and resolution.decision == "REJECT_AND_REPLAN":
                cycle = max(cycle, current + 1)
        return cycle

    def _prior_replan_notes(self, cycle: int) -> str:
        """Expose prior Human objections to the fresh planning cycle only."""

        service = HumanConsultationService(self.repo)
        notes: list[str] = []
        for current in range(1, cycle):
            for path in sorted((self.repo.root / "human_private/consultations").glob(
                f"HC-LROUTLINE-C{current:03d}*.resolution.json"
            )):
                resolution = service.resolution(path.name.removesuffix(".resolution.json"))
                if resolution is not None and resolution.decision == "REJECT_AND_REPLAN":
                    notes.append(f"C{current:03d} Human 异议：{resolution.rationale}")
        return "\n".join(notes)[-6000:]

    def _rejected_outline_reference(self, cycle: int) -> Path | None:
        """Expose only the latest rejected *frozen* plan in newly created meetings.

        Individual submissions remain governed by their original sealed-release
        rules. Existing meetings lack the manifest switch and keep old prompts.
        """
        if not self.replan_reference_enabled or cycle <= 1:
            return None
        prior_cycle = cycle - 1
        suffix = self._cycle_suffix(prior_cycle)
        path = self.repo.root / f"public/literature_report/frozen_research_outline{suffix}.json"
        if not path.is_file():
            raise FileNotFoundError(f"rejected frozen outline is missing: {path}")
        return path

    @staticmethod
    def _rejected_outline_instruction() -> str:
        return (
            "上一轮冻结总纲仅作为已否决的对照资料；结合 Human 异议辨认范围、遗漏和边界问题。"
            "它不是获批方案，不得直接沿用旧模块划分，也不得据此缩小原始任务。\n"
        )

    def _outline_review_issue_id(self, cycle: int) -> str:
        return f"HC-LROUTLINE-C{cycle:03d}"

    def _human_outline_review(
        self, *, cycle: int, outline: FrozenResearchOutline, outline_path: Path
    ) -> str | None:
        """Create/read the Human gate after module decomposition and review."""

        issue_id = self._outline_review_issue_id(cycle)
        suffix = self._cycle_suffix(cycle)
        submissions_relative = Path(
            f"public/literature_report/decomposition_submissions{suffix}.json"
        )
        submissions_path = self.repo.root / submissions_relative
        scope_objections: list[str] = []
        if submissions_path.exists():
            for entry in json.loads(submissions_path.read_text(encoding="utf-8")).get(
                "submissions", []
            ):
                submission = entry.get("submission", {})
                objection = submission.get("scope_objection")
                if objection:
                    scope_objections.append(str(objection))
        module_digest = "；".join(
            f"{module.module_id}《{module.title}》" for module in outline.modules
        )
        audience_context = {
            "outline_path": str(outline_path.relative_to(self.repo.root)),
            "proposed_disciplines": outline.proposed_disciplines,
            "article_skeleton": outline.article_skeleton,
            "glossary_recommended": outline.glossary_recommended,
            "cycle": cycle,
        }
        scope_notice = outline.scope_concern_notice
        if not scope_notice and scope_objections:
            scope_notice = (
                f"有 {len(scope_objections)} 份规划代表的范围异议；主席未提供摘要。"
                + "；".join(scope_objections)
            )
        audience_context["scope_notice"] = scope_notice
        scope_message = (
            f"{'主席转呈的任务范围意见' if outline.scope_concern_notice else '原始范围异议'}："
            f"{scope_notice}。"
            "代表和主席均不得据此自行缩减原始任务；若你决定改变范围，请选择退回重做并在理由中说明。"
            if scope_notice
            else "代表和主席均不得自行缩减原始任务；如需改变范围，请选择退回重做并说明。"
        )
        service = HumanConsultationService(self.repo)
        current_skeleton = outline.article_skeleton
        revision = 0
        while True:
            current_issue_id = issue_id if revision == 0 else f"{issue_id}-S{revision:03d}"
            audience_context["article_skeleton"] = current_skeleton
            issue = HumanConsultationIssue(
                issue_id=current_issue_id,
                meeting_id=self.repo.meeting_id,
                reason_code="HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED",
                stage="LITERATURE_RESEARCH_OUTLINE",
                question=(
                    f"请审阅研究模块划分（{len(outline.modules)} 个模块）。模块总纲位于 "
                    f"{outline_path.relative_to(self.repo.root)}。批准后才会开始正式文献检索；如不批准，"
                    f"请在理由中逐条说明模块过宽、遗漏、重叠或边界错误，系统将完整重做规划、拆分和审阅。"
                    f"{scope_message}可用自然语言向主席询问。模块清单：{module_digest}。"
                    f"主席建议学科：{', '.join(outline.proposed_disciplines) or '未列出'}；"
                    f"文章骨架：{' → '.join(current_skeleton) or '未列出'}。"
                    "如仅对文章骨架有异议，可要求主席局部修改，不必重做研究模块。"
                ),
                options=["APPROVE_OUTLINE", "REJECT_AND_REPLAN", "REVISE_SKELETON_ONLY"],
                affected_items=[module.module_id for module in outline.modules],
                context=dict(audience_context),
            )
            existing_issue_path = (
                self.repo.root / "human_private/consultations" / f"{current_issue_id}.issue.json"
            )
            if existing_issue_path.exists():
                created = False
            else:
                _, created = service.open_issue(issue)
            resolution = service.resolution(current_issue_id)
            if resolution is None:
                if created:
                    self.engine.escalation.request(
                        reason_code=issue.reason_code,
                        summary=(
                            f"Meeting {self.repo.meeting_id} requires Human review of the research outline "
                            f"before literature-module execution (cycle {cycle})."
                        ),
                    )
                return None
            if resolution.decision == "REVISE_SKELETON_ONLY":
                revision += 1
                current_skeleton = self._revise_article_skeleton(
                    outline=outline, prior=current_skeleton,
                    human_rationale=resolution.rationale, cycle=cycle, revision=revision,
                )
                continue
            if resolution.decision == "REJECT_AND_REPLAN":
                self.repo.events.append(
                    "LITERATURE_RESEARCH_OUTLINE_REJECTED",
                    {
                        "meeting_id": self.repo.meeting_id, "cycle": cycle,
                        "issue_id": current_issue_id,
                        "rationale": resolution.rationale, "next_cycle": cycle + 1,
                    },
                    actor="HUMAN",
                )
                return "REJECT_AND_REPLAN"
            if resolution.decision != "APPROVE_OUTLINE":
                raise ValueError(f"unknown Human outline decision: {resolution.decision}")
            self.repo.events.append(
                "LITERATURE_RESEARCH_OUTLINE_APPROVED",
                {
                    "meeting_id": self.repo.meeting_id, "cycle": cycle,
                    "issue_id": current_issue_id,
                    "outline_path": str(outline_path.relative_to(self.repo.root)),
                    "article_skeleton": current_skeleton,
                },
                actor="HUMAN",
            )
            return "APPROVE_OUTLINE"

    def _revise_article_skeleton(
        self, *, outline: FrozenResearchOutline, prior: list[str],
        human_rationale: str, cycle: int, revision: int,
    ) -> list[str]:
        relative = Path(
            f"public/literature_report/article_skeleton-C{cycle:03d}-S{revision:03d}.json"
        )
        path = self.repo.root / relative
        if path.exists():
            return ArticleSkeletonRevision.model_validate_json(
                path.read_text(encoding="utf-8")
            ).article_skeleton
        system_text = (
            "仅按 Human 意见局部修改整篇文献综述的文章骨架。"
            "研究模块、范围、问题和代表审阅结果全部冻结，不得改变。"
            "返回新的主题章节次序；不要给每个模块机械套用统一模板。"
        )
        user_text = json.dumps({
            "prior_article_skeleton": prior,
            "module_titles": [module.title for module in outline.modules],
            "human_objection": human_rationale,
            "schema": ArticleSkeletonRevision.model_json_schema(),
        }, ensure_ascii=False)
        stage = f"chair_article_skeleton_revision_C{cycle:03d}_S{revision:03d}"
        response = self.engine.find_recorded_response(
            "CHAIR", system_text=system_text, user_text=user_text, stage=stage
        ) or self.engine.invoke_participant(
            "CHAIR", system_text=system_text, user_text=user_text,
            stage=stage, max_output_tokens=self.max_output_tokens,
        )
        parsed = self.engine.validate_structured_response(
            "CHAIR", response=response, schema_model=ArticleSkeletonRevision,
            stage=stage, max_output_tokens=self.max_output_tokens,
        )
        self.repo.docs.write_once(relative, parsed.model_dump_json(indent=2))
        return parsed.article_skeleton

    def _load_or_select_panel(self, registry: list[dict], *, cycle: int) -> list[dict]:
        suffix = self._cycle_suffix(cycle)
        relative = Path(f"governance_private/literature_report/planning_panel{suffix}.json")
        path = self.repo.root / relative
        by_id = {record["representative_id"]: record for record in registry}
        if path.exists():
            frozen = json.loads(path.read_text(encoding="utf-8"))
            return [by_id[representative_id] for representative_id in frozen["representative_ids"]]

        providers = sorted({record["runtime"]["provider_id"] for record in registry})
        if not providers:
            raise ValueError("Representative registry has no model providers")
        rotation = int(
            hashlib.sha256(f"{self.repo.meeting_id}:C{cycle:03d}".encode()).hexdigest()[:8], 16
        ) % len(providers)
        provider_order = providers[rotation:] + providers[:rotation]
        selected: list[dict] = []
        for index, persona in enumerate(_PERSONA_ORDER):
            preferred = provider_order[index % len(provider_order)]
            candidates = [
                record
                for record in registry
                if record["runtime"]["persona"] == persona.value
                and record["runtime"]["provider_id"] == preferred
            ]
            if not candidates:
                candidates = [
                    record
                    for record in registry
                    if record["runtime"]["persona"] == persona.value
                ]
            candidates.sort(
                key=lambda record: (
                    record["runtime"]["provider_id"],
                    record["runtime"]["model_id"],
                    record["representative_id"],
                )
            )
            choice = int(
                hashlib.sha256(
                    f"{self.repo.meeting_id}:C{cycle:03d}:{persona.value}".encode()
                ).hexdigest()[:8],
                16,
            ) % len(candidates)
            selected.append(candidates[choice])

        record = {
            "meeting_id": self.repo.meeting_id,
            "selection_policy": "ONE_PER_PERSONA_MAXIMIZE_PROVIDER_COVERAGE_DETERMINISTIC_ROTATION_V1",
            "panel_size": 4,
            "representative_ids": [item["representative_id"] for item in selected],
            "assignments": [
                {
                    "representative_id": item["representative_id"],
                    "persona": item["runtime"]["persona"],
                    "provider_id": item["runtime"]["provider_id"],
                    "model_id": item["runtime"]["model_id"],
                }
                for item in selected
            ],
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "LITERATURE_PLANNING_PANEL_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "panel_size": 4,
                "selection_policy": record["selection_policy"],
            },
            actor="orchestrator",
        )
        return selected

    def _collect_decompositions(
        self,
        panel: list[dict],
        task_path: Path,
        *,
        cycle: int,
        replan_notes: str = "",
        chair_exploration: Path | None = None,
    ) -> dict[str, dict]:
        completed: dict[str, dict] = {}
        missing: list[dict] = []
        for record in panel:
            representative_id = record["representative_id"]
            path = self.repo.root / self._decomposition_relative(representative_id, cycle=cycle)
            if path.exists():
                completed[representative_id] = json.loads(path.read_text(encoding="utf-8"))
                self.engine.progress.info(f"已恢复研究规划方案 {len(completed)}/4")
            else:
                missing.append(record)

        lock = threading.Lock()
        task_ids = {
            record["representative_id"]: f'{record["representative_id"]}:{index}'
            for index, record in enumerate(panel)
        }

        def persist(result: tuple[str, dict]) -> None:
            representative_id, payload = result
            with lock:
                self.repo.docs.write_once(
                    self._decomposition_relative(representative_id, cycle=cycle),
                    json.dumps(payload, indent=2, ensure_ascii=False),
                )
                completed[representative_id] = payload
                self.engine.progress.task_finished(
                    task_ids[representative_id], detail="方案已校验并提交"
                )
                self.engine.progress.info(f"已收到研究规划方案 {len(completed)}/4；继续密封")

        run_bounded_representative_lanes(
            missing,
            lambda record: self._collect_one_decomposition(
                record, task_path, cycle=cycle, replan_notes=replan_notes,
                chair_exploration=chair_exploration,
            ),
            self.engine.model_concurrency_limit,
            on_result=persist,
            progress=self.engine.progress,
            progress_records=panel,
            completed_participant_ids=completed,
            batch_title="研究规划 · 代表方案与 Research Desk 探索",
            completion_requires_commit=True,
            exploration_layout=self.exploration_enabled,
        )
        return {record["representative_id"]: completed[record["representative_id"]] for record in panel}

    def _collect_one_decomposition(
        self, record: dict, task_path: Path, *, cycle: int, replan_notes: str = "",
        chair_exploration: Path | None = None,
    ) -> tuple[str, dict]:
        representative_id = record["representative_id"]
        private_exploration = (
            self._run_exploration(
                representative_id, task_path, cycle=cycle, role="REPRESENTATIVE",
                chair_overview=chair_exploration,
                persona=Persona(record["runtime"]["persona"]),
                replan_notes=replan_notes,
            ) if self.exploration_enabled else None
        )
        prior_outline = self._rejected_outline_reference(cycle)
        spec = self.resolver.representative_context_spec(
            persona=Persona(record["runtime"]["persona"]),
            stage="research_decomposition",
            representative_id=representative_id,
            public_state_files=(task_path, prior_outline) if prior_outline else (task_path,),
            prompt_family=self.prompt_family,
        )
        system_text = self.assembler.assemble(spec)
        user_text = (
            f"规划轮次：C{cycle:03d}。若人类否决了先前方案，本轮须重新完整拆分，不沿用旧模块划分。\n"
            + self._planning_length_guidance()
            + (f"需要回应的人类先前异议：\n{replan_notes}\n" if replan_notes else "")
            + (self._rejected_outline_instruction() if prior_outline else "")
            + "在 framing_rationale 中解释拆分理由与模块边界，供人类审阅；无硬性字数限制，但避免冗长。"
            "普通思路不必注明灵感来源；明确借鉴特定已发表方法或框架时应注明来源。\n"
            "不得自行缩小或排除人类原始任务的任何部分。如果认为范围过宽，仍须针对完整任务规划，"
            "并在 scope_objection 中简要提出异议；没有异议则用 null。主席会在总纲冻结审阅时转告人类。\n"
            + "只返回一个符合以下结构的 JSON 对象：\n"
            + json.dumps(ResearchDecompositionSubmission.model_json_schema(), ensure_ascii=False)
        )
        if chair_exploration is not None:
            user_text += (
                "\n\n主席公开的探索结果仅为资料，不是模块划分指令；请独立覆盖原任务：\n"
                + self._exploration_context(chair_exploration)
            )
        if private_exploration is not None:
            user_text += (
                "\n\n你自己的私有探索结果；只依据已返回的资料拟订方案，不自行缩小任务范围：\n"
                + self._exploration_context(private_exploration)
            )
        response = self.engine.find_recorded_response(
            representative_id, system_text=system_text, user_text=user_text, stage="research_decomposition"
        ) or self.engine.invoke_participant(
            representative_id,
            system_text=system_text,
            user_text=user_text,
            stage="research_decomposition",
            max_output_tokens=self.max_output_tokens,
        )
        parsed = self.engine.validate_structured_response(
            representative_id,
            response=response,
            schema_model=ResearchDecompositionSubmission,
            stage="research_decomposition",
            max_output_tokens=self.max_output_tokens,
            semantic_requirement="保留完整研究拆分，不得添加第九个模块。",
        )
        return representative_id, {
            "representative_id": representative_id,
            "submission": parsed.model_dump(mode="json"),
        }

    @staticmethod
    def _exploration_context(index_path: Path) -> str:
        """Bound model context while preserving full answers in on-disk artifacts."""
        index = json.loads(index_path.read_text(encoding="utf-8"))
        excerpts = []
        omitted = 0
        for item in index.get("answers", []):
            if item.get("status") == "SKIPPED_QUOTA":
                continue
            excerpt = {
                "question": item.get("question"),
                "answer_excerpt": str(item.get("answer_text", ""))[:1800],
                "sources": [
                    {
                        "source_id": source.get("source_id"),
                        "title": source.get("title"),
                        "authors": source.get("authors", [])[:3],
                        "publication_year": source.get("publication_year"),
                        "url": source.get("url"),
                    }
                    for source in item.get("source_catalog", [])[:8]
                ],
                "full_answer_path": item.get("answer_path"),
            }
            if len(json.dumps({"answers": excerpts + [excerpt]}, ensure_ascii=False)) > 40000:
                omitted += 1
            else:
                excerpts.append(excerpt)
        return json.dumps(
            {"answers": excerpts, "omitted_due_to_context_limit": omitted},
            ensure_ascii=False,
        )

    def _run_exploration(
        self, actor: str, task_path: Path, *, cycle: int, role: str,
        chair_overview: Path | None,
        persona: Persona | None = None,
        replan_notes: str = "",
    ) -> Path:
        """Ask, answer, and checkpoint one actor's bounded exploratory dialogue."""
        assert self.exploration_service is not None
        self.engine.progress.exploration_actor(actor)
        private_root = Path("governance_private/literature_report/exploration/planning")
        actor_root = private_root / f"C{cycle:03d}" / actor
        index_relative = actor_root / "index.json"
        index_path = self.repo.root / index_relative
        if index_path.exists():
            self.engine.progress.exploration_desk(
                actor, "completed", "已恢复完整检索结果"
            )
            if role == "CHAIR":
                return self._publish_exploration_index(
                    json.loads(index_path.read_text(encoding="utf-8")),
                    actor=actor, cycle=cycle,
                )
            return index_path
        task_text = task_path.read_text(encoding="utf-8")
        answers: list[dict] = []
        searches_used = 0
        seen: set[str] = set()
        for turn in range(1, 17):
            remaining = 16 - searches_used
            turn_relative = actor_root / f"turn-{turn:02d}.json"
            turn_path = self.repo.root / turn_relative
            if turn_path.exists():
                completed_turn = json.loads(turn_path.read_text(encoding="utf-8"))
                answers.extend(completed_turn["answers"])
                searches_used += completed_turn["searches_used"]
                seen.update(item.get("fingerprint", "") for item in completed_turn["answers"])
                if completed_turn["finished"]:
                    break
                continue
            self.engine.progress.info(
                f"{actor} 正在提出探索性文献问题；剩余检索额度 {remaining}/16"
            )
            previous = [
                {"question": item.get("question"), "answer": str(item.get("answer_text", ""))[:2500]}
                for item in answers[-6:] if item.get("answer_text")
            ]
            if role == "CHAIR":
                system_text = (
                    "任务：在研究总纲规划前提出必要的概览性文献检索问题。只提问，不总结、"
                    "解释或提出模块划分。不得把自己猜测的模块名称带入检索问题。只在有意义时继续追问；"
                    "可一次提出多个问题，每个问题附一条或多条具体搜索语句。收到答复后再继续。"
                    "检索额度按实际执行的搜索语句计算，上限 16；缓存命中不扣额度。"
                )
            else:
                office_focus = {
                    Persona.SYSTEMS_INTEGRATOR: "建构者：整体结构、依赖与接口的一致性",
                    Persona.PRAGMATIC_MINIMALIST: "监管者：可维持性、资源负担与不必要的复杂度",
                    Persona.EXPLORATORY_SYNTHESIST: "制图者：方案空间和未被覆盖的可行路径",
                    Persona.LIBRARIAN: "智库长：知识来源、定义、假设与可核验性",
                }.get(persona, "独立规划代表")
                system_text = (
                    f"任务：为独立研究拆分方案收集资料。办公室视角：{office_focus}。"
                    "可提出探索性问题、暂拟模块名称并追问；"
                    "这份问答在四份规划方案全部冻结前对其他规划代表保密。收到答复后再继续。"
                    "可一次提出多个问题，每个问题附一条或多条具体搜索语句。"
                    "检索额度按实际执行的搜索语句计算，上限 16；缓存命中不扣额度。"
                    "资料不能授权自行缩小人类原任务的范围。"
                )
            user_text = (
                f"原始任务：\n{task_text}\n\n当前剩余检索额度：{remaining}/16。"
                f"\n\n已获得的近期答复：\n{json.dumps(previous, ensure_ascii=False)}"
            )
            prior_outline = self._rejected_outline_reference(cycle)
            if prior_outline is not None:
                user_text += (
                    "\n\n" + self._rejected_outline_instruction()
                    + f"Human 先前异议：\n{replan_notes}\n"
                    + "已否决的上一轮冻结总纲：\n"
                    + prior_outline.read_text(encoding="utf-8")
                )
            if chair_overview is not None:
                user_text += "\n\n主席公开检索结果：\n" + self._exploration_context(chair_overview)
            user_text += (
                "\n\n若不需继续检索，设 finished=true 且 questions=[]；否则提交按顺序处理的问题。"
                "不要声称 Research Desk 已核验尚未提交的问题。返回 JSON：\n"
                + json.dumps(ExplorationBatch.model_json_schema(), ensure_ascii=False)
            )
            stage = f"research_planning_exploration:C{cycle:03d}:{actor}:T{turn:02d}"
            batch_relative = actor_root / f"batch-{turn:02d}.json"
            batch_path = self.repo.root / batch_relative
            if batch_path.exists():
                batch = ExplorationBatch.model_validate_json(batch_path.read_text(encoding="utf-8"))
            else:
                response = self.engine.find_recorded_response(
                    actor, system_text=system_text, user_text=user_text, stage=stage
                ) or self.engine.invoke_participant(
                    actor, system_text=system_text, user_text=user_text, stage=stage,
                    max_output_tokens=self.max_output_tokens,
                )
                try:
                    batch = self.engine.validate_structured_response(
                        actor, response=response, schema_model=ExplorationBatch, stage=stage,
                        max_output_tokens=self.max_output_tokens,
                        nonblocking_quality_failure_code="EXPLORATION_BATCH_INVALID",
                    )
                except ResearchQualityControlError:
                    batch = ExplorationBatch(finished=True, questions=[])
                    self.engine.progress.info(
                        f"{actor} 的探索性提问在格式修复后仍无效；已记录并继续规划"
                    )
                self.repo.docs.write_once(batch_relative, batch.model_dump_json(indent=2))
            turn_answers: list[dict] = []
            used_this_turn = 0
            for number, question in enumerate(batch.questions, start=1):
                fingerprint = ResearchExplorationService._fingerprint(
                    question.question, question.search_queries
                )
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                available = 16 - searches_used - used_this_turn
                required = self.exploration_service.searches_needed(
                    question.question, question.search_queries, question.freshness_class
                )
                if required > available:
                    turn_answers.append({
                        "question": question.question, "fingerprint": fingerprint,
                        "status": "SKIPPED_QUOTA", "searches_used": 0,
                        "answer_text": "超过本轮剩余检索额度，未执行搜索。",
                    })
                    continue
                result = self.exploration_service.answer(
                    requester_id=actor, cycle=cycle, turn=turn, question_number=number,
                    question=question.question, search_queries=question.search_queries,
                    freshness_class=question.freshness_class,
                )
                used_this_turn += result["searches_used"]
                turn_answers.append(result)
                self.engine.progress.exploration_desk(
                    actor, "running", f"已整理 {len(answers) + len(turn_answers)} 条答复"
                )
            finished = (
                batch.finished or not batch.questions or not turn_answers
                or all(item.get("status") == "SKIPPED_QUOTA" for item in turn_answers)
            )
            completed_turn = {
                "answers": turn_answers, "searches_used": used_this_turn,
                "finished": finished,
            }
            self.repo.docs.write_once(
                turn_relative, json.dumps(completed_turn, indent=2, ensure_ascii=False)
            )
            answers.extend(turn_answers)
            searches_used += used_this_turn
            if finished:
                break
        index = {
            "actor": actor, "cycle": cycle, "search_budget": 16,
            "searches_used": searches_used, "answers": answers,
        }
        self.repo.docs.write_once(index_relative, json.dumps(index, indent=2, ensure_ascii=False))
        self.engine.progress.exploration_desk(
            actor, "completed", f"检索阶段已提交 · {len(answers)} 条答复"
        )
        if role == "CHAIR":
            return self._publish_exploration_index(index, actor=actor, cycle=cycle)
        return index_path

    def _publish_exploration_index(self, index: dict, *, actor: str, cycle: int) -> Path:
        assert self.exploration_service is not None
        public_root = Path("public/literature_report/exploration") / f"C{cycle:03d}" / actor
        index_relative = public_root / "index.json"
        index_path = self.repo.root / index_relative
        if index_path.exists():
            return index_path
        answered = [item for item in index["answers"] if item.get("answer_id")]
        published = self.exploration_service.publish(answered, public_prefix=public_root)
        public_index = {
            "actor": actor, "cycle": cycle, "search_budget": 16,
            "searches_used": index["searches_used"],
            "answers": [
                {**item, "answer_path": str(public_root / f"{item['answer_id']}.json")}
                if item.get("answer_id") else item
                for item in (published + [
                    entry for entry in index["answers"] if not entry.get("answer_id")
                ])
            ],
        }
        self.repo.docs.write_once(index_relative, json.dumps(public_index, indent=2, ensure_ascii=False))
        return index_path

    def _release_representative_exploration(self, panel: list[dict], *, cycle: int) -> None:
        """Publish private Q&A only after all four plans have frozen."""
        for record in panel:
            actor = record["representative_id"]
            private_path = (
                self.repo.root / "governance_private/literature_report/exploration/planning"
                / f"C{cycle:03d}" / actor / "index.json"
            )
            index = json.loads(private_path.read_text(encoding="utf-8"))
            self._publish_exploration_index(index, actor=actor, cycle=cycle)

    def _release_decompositions(
        self, panel: list[dict], submissions: dict[str, dict], *, cycle: int
    ) -> Path:
        suffix = self._cycle_suffix(cycle)
        relative = Path(f"public/literature_report/decomposition_submissions{suffix}.json")
        path = self.repo.root / relative
        if not path.exists():
            payload = {
                "meeting_id": self.repo.meeting_id,
                "status": "FROZEN",
                "submission_count": len(submissions),
                "submissions": [submissions[item["representative_id"]] for item in panel],
            }
            self.repo.docs.write_once(relative, json.dumps(payload, indent=2, ensure_ascii=False))
        return path

    def _ensure_candidate_outline(
        self,
        task_path: Path,
        submissions_path: Path,
        *,
        cycle: int,
        replan_notes: str = "",
    ) -> Path:
        suffix = self._cycle_suffix(cycle)
        relative = Path(f"public/literature_report/candidate_research_outline{suffix}.json")
        path = self.repo.root / relative
        if path.exists():
            return path
        system_text = (
            "只编辑研究总纲：将四份密封拆分方案聚类、去重，形成连贯的文献综述提纲。"
            "另外提出读者可能涉及的学科门类 proposed_disciplines，以及适合本题的文章骨架 article_skeleton。"
            "文章骨架不是每个模块都套用的固定模板；模块通常对应一个主题章节。"
            "可建议是否另设术语表，不得预设读者专业度。"
            "在 source_submission_refs 保留各来源方案的追溯关系，在 clustering_notes 解释合并或遗漏，"
            "并逐模块保留 framing_rationale 中可辨认的方法或框架借鉴出处。疑似漏引时记录模块编号和待核来源，"
            "作为不阻断流程的提醒；不得仅凭怀疑认定不端、编造引文或停止规划。"
            "不得缩小人类原始任务。将代表异议及主席独立发现的范围问题中立地写入 scope_concern_notice，"
            "供人类审阅；范围异议不授权遗漏内容。无此问题时用 null。"
            "cross_module_links 只能填 RM-02 之类的模块编号，解释写入 clustering_notes。"
            "不得裁决科学真伪、预设研究结论或撰写报告正文。只返回一个 JSON 对象。"
        )
        user_text = (
            "任务：\n" + task_path.read_text(encoding="utf-8") + "\n\n拆分方案：\n"
            + submissions_path.read_text(encoding="utf-8") + "\n\n目标结构：\n"
            + json.dumps(ClusteredResearchOutline.model_json_schema(), ensure_ascii=False)
        )
        length_guidance = self._planning_length_guidance()
        if length_guidance:
            user_text += "\n\n正文长度约束：\n" + length_guidance
        if replan_notes:
            user_text += "\n\n需要回应的人类先前异议：\n" + replan_notes
        prior_outline = self._rejected_outline_reference(cycle)
        if prior_outline is not None:
            # Keep the requested output schema after the rejected reference so
            # the old frozen document cannot be mistaken for the new schema.
            schema_marker = "\n\n目标结构：\n"
            before_schema, _ = user_text.split(schema_marker, 1)
            user_text = (
                before_schema
                + ("\n\n正文长度约束：\n" + length_guidance if length_guidance else "")
                + ("\n\n需要回应的人类先前异议：\n" + replan_notes if replan_notes else "")
                + "\n\n" + self._rejected_outline_instruction()
                + "已否决的上一轮冻结总纲：\n"
                + prior_outline.read_text(encoding="utf-8")
                + schema_marker
                + json.dumps(ClusteredResearchOutline.model_json_schema(), ensure_ascii=False)
            )
        response = self.engine.find_recorded_response(
            "CHAIR", system_text=system_text, user_text=user_text, stage="chair_research_outline_clustering"
        ) or self.engine.invoke_participant(
            "CHAIR",
            system_text=system_text,
            user_text=user_text,
            stage="chair_research_outline_clustering",
            max_output_tokens=self.max_output_tokens,
        )
        parsed = self.engine.validate_structured_response(
            "CHAIR",
            response=response,
            schema_model=ClusteredResearchOutline,
            stage="chair_research_outline_clustering",
            max_output_tokens=self.max_output_tokens,
            semantic_requirement=(
                "source_submission_refs 必须能追溯到代表及其本地模块编号；"
                "cross_module_links 必须指向现有的 RM-xx 模块编号。"
            ),
        )
        self.repo.docs.write_once(relative, parsed.model_dump_json(indent=2))
        return path

    def _collect_reviews(
        self,
        registry: list[dict],
        task_path: Path,
        outline_path: Path,
        *,
        cycle: int,
        replan_notes: str = "",
    ) -> dict[str, dict]:
        completed: dict[str, dict] = {}
        missing: list[dict] = []
        for record in registry:
            representative_id = record["representative_id"]
            path = self.repo.root / self._review_relative(representative_id, cycle=cycle)
            if path.exists():
                completed[representative_id] = json.loads(path.read_text(encoding="utf-8"))
            else:
                missing.append(record)
        lock = threading.Lock()

        def persist(result: tuple[str, dict]) -> None:
            representative_id, payload = result
            with lock:
                self.repo.docs.write_once(
                    self._review_relative(representative_id, cycle=cycle),
                    json.dumps(payload, indent=2, ensure_ascii=False),
                )
                completed[representative_id] = payload
                self.engine.progress.info(
                    f"已收到研究总纲结构审阅 {len(completed)}/{len(registry)}；继续密封"
                )

        run_bounded_representative_lanes(
            missing,
            lambda record: self._collect_one_review(
                record, task_path, outline_path, cycle=cycle, replan_notes=replan_notes
            ),
            self.engine.model_concurrency_limit,
            on_result=persist,
            progress=self.engine.progress,
            progress_records=registry,
            completed_participant_ids=completed,
        )
        return {record["representative_id"]: completed[record["representative_id"]] for record in registry}

    def _collect_one_review(
        self,
        record: dict,
        task_path: Path,
        outline_path: Path,
        *,
        cycle: int,
        replan_notes: str = "",
    ) -> tuple[str, dict]:
        representative_id = record["representative_id"]
        prior_outline = self._rejected_outline_reference(cycle)
        spec = self.resolver.representative_context_spec(
            persona=Persona(record["runtime"]["persona"]),
            stage="research_outline_review",
            representative_id=representative_id,
            public_state_files=(task_path, outline_path, prior_outline) if prior_outline else (task_path, outline_path),
            prompt_family=self.prompt_family,
        )
        system_text = self.assembler.assemble(spec)
        user_text = (
            f"规划轮次：C{cycle:03d}。独立审阅本轮总纲；可对照被否决的旧稿识别缺陷，"
            "但不得把旧稿视为获批方案或直接复用。\n"
            + (f"需要回应的人类先前异议：\n{replan_notes}\n" if replan_notes else "")
            + (self._rejected_outline_instruction() if prior_outline else "")
            + "只返回一个符合以下结构的 JSON 对象：\n"
            + json.dumps(ResearchOutlineReview.model_json_schema(), ensure_ascii=False)
        )
        response = self.engine.find_recorded_response(
            representative_id, system_text=system_text, user_text=user_text, stage="research_outline_review"
        ) or self.engine.invoke_participant(
            representative_id,
            system_text=system_text,
            user_text=user_text,
            stage="research_outline_review",
            max_output_tokens=self.max_output_tokens,
        )
        parsed = self.engine.validate_structured_response(
            representative_id,
            response=response,
            schema_model=ResearchOutlineReview,
            stage="research_outline_review",
            max_output_tokens=self.max_output_tokens,
            semantic_requirement="只选一种允许的行动；最多两项问题或一项结构建议。",
        )
        return representative_id, {
            "representative_id": representative_id,
            "review": parsed.model_dump(mode="json"),
        }

    def _release_reviews(
        self, registry: list[dict], reviews: dict[str, dict], *, cycle: int
    ) -> Path:
        suffix = self._cycle_suffix(cycle)
        relative = Path(f"public/literature_report/outline_reviews{suffix}.json")
        path = self.repo.root / relative
        if not path.exists():
            payload = {
                "meeting_id": self.repo.meeting_id,
                "status": "FROZEN",
                "review_count": len(reviews),
                "reviews": [reviews[item["representative_id"]] for item in registry],
            }
            self.repo.docs.write_once(relative, json.dumps(payload, indent=2, ensure_ascii=False))
        return path

    def _ensure_frozen_outline(
        self,
        task_path: Path,
        candidate_path: Path,
        reviews_path: Path,
        *,
        cycle: int,
        replan_notes: str = "",
    ) -> Path:
        suffix = self._cycle_suffix(cycle)
        relative = Path(f"public/literature_report/frozen_research_outline{suffix}.json")
        path = self.repo.root / relative
        if path.exists():
            return path
        system_text = (
            "在限量结构审阅后形成文献综述研究总纲。采纳有根据的结构修正，保留不预设结果的研究问题，"
            "同时更新拟议学科门类和整篇文章骨架，供 Human 在批准模块划分时一并审阅。"
            "并记录对每条代表意见的处理。在 clustering_notes 中保留各模块的文献借鉴出处和待核查来源提醒；"
            "疑似漏引本身不构成不端，也不阻断总纲。保留或更新 scope_concern_notice，但不得自行缩小人类任务；"
            "范围变更须由人类在审阅时决定。不得投票裁定或编造科学发现。只返回一个 JSON 对象。"
        )
        user_text = (
            "任务：\n" + task_path.read_text(encoding="utf-8")
            + "\n\n候选总纲：\n" + candidate_path.read_text(encoding="utf-8")
            + "\n\n审阅意见：\n" + reviews_path.read_text(encoding="utf-8")
            + "\n\n目标结构：\n" + json.dumps(FrozenResearchOutline.model_json_schema(), ensure_ascii=False)
        )
        if replan_notes:
            user_text += "\n\nPRIOR HUMAN OBJECTIONS TO ADDRESS:\n" + replan_notes
        prior_outline = self._rejected_outline_reference(cycle)
        if prior_outline is not None:
            user_text += (
                "\n\n" + self._rejected_outline_instruction()
                + "已否决的上一轮冻结总纲：\n"
                + prior_outline.read_text(encoding="utf-8")
            )
        response = self.engine.find_recorded_response(
            "CHAIR", system_text=system_text, user_text=user_text, stage="chair_research_outline_finalization"
        ) or self.engine.invoke_participant(
            "CHAIR",
            system_text=system_text,
            user_text=user_text,
            stage="chair_research_outline_finalization",
            max_output_tokens=self.max_output_tokens,
        )
        parsed = self.engine.validate_structured_response(
            "CHAIR",
            response=response,
            schema_model=FrozenResearchOutline,
            stage="chair_research_outline_finalization",
            max_output_tokens=self.max_output_tokens,
            semantic_requirement="对每份已提交的代表审阅恰好记录一项 review_disposition。",
        )
        expected = {
            item["representative_id"]
            for item in json.loads(reviews_path.read_text(encoding="utf-8"))["reviews"]
        }
        actual = {item.representative_id for item in parsed.review_dispositions}
        if actual != expected or len(parsed.review_dispositions) != len(expected):
            raise ValueError("Chair must record exactly one disposition per outline review")
        candidate = ClusteredResearchOutline.model_validate_json(
            candidate_path.read_text(encoding="utf-8")
        )
        if candidate.scope_concern_notice and not parsed.scope_concern_notice:
            # Final editing must not silently erase a concern already raised
            # for the Human; no scope decision is made here.
            parsed.scope_concern_notice = candidate.scope_concern_notice
        self.repo.docs.write_once(relative, parsed.model_dump_json(indent=2))
        return path

    @staticmethod
    def _decomposition_relative(representative_id: str, *, cycle: int = 1) -> Path:
        suffix = LiteratureReportPlanningRunner._cycle_suffix(cycle)
        return Path("governance_private/literature_report/decompositions") / f"{representative_id}{suffix}.json"

    @staticmethod
    def _review_relative(representative_id: str, *, cycle: int = 1) -> Path:
        suffix = LiteratureReportPlanningRunner._cycle_suffix(cycle)
        return Path("governance_private/literature_report/outline_reviews") / f"{representative_id}{suffix}.json"
