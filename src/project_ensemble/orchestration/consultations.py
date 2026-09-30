from __future__ import annotations

import json
import re
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from project_ensemble.storage.meeting import MeetingRepository, PublicMeetingManifest

if TYPE_CHECKING:
    from project_ensemble.orchestration.engine import MeetingEngine


CONSULTATION_OPTION_LABELS = {
    "SEQUENTIAL_BINARY_WITH_CURRENT_TEXT": "先按当前文本表决第一项",
    "DEFER_CURRENT_AMENDMENT": "暂缓当前修正案，待文本更新后重新改写",
    "REQUEST_CHAIR_RECLASSIFICATION": "要求主席重新审查两项修正案的关系",
    "APPROVE_OUTLINE": "批准模块划分，开始正式文献研究",
    "REJECT_AND_REPLAN": "不批准，说明异议并彻底重做模块划分",
    "REVISE_SKELETON_ONLY": "只修改文章骨架；保留已审阅的研究模块",
    "USE_SOURCE_TEXT": "采用冻结原文",
    "USE_CURRENT_REDRAW": "采用当前重绘稿",
    "USE_HUMAN_WORDING": "由 Human 提供最终表述",
    "ACCEPT_SCIENCE_OBJECTION": "采纳这条科学事实异议，交主席修稿",
    "REJECT_SCIENCE_OBJECTION": "不采纳这条异议，交主席继续处理",
    "DIRECT_CHAIR_SCIENCE_REVISION": "用一句自然语言给主席具体修改方向",
    "USE_FROZEN_VERIFICATION": "沿用已经冻结的科学核验结果",
    "USE_CURRENT_REVIEWS_AND_REVERIFY": "按当前审阅内容重新核验并另存结果",
    "RESTORE_SOURCE_FIDELITY_BALLOT": "仅恢复对原文忠实性的最终表决资格；不得加入未经核验的新结论",
    "KEEP_PAUSED": "保持暂停，暂不继续",
    "ACCEPT_LENGTH_VARIANCE": "接受正文超出预设长度范围并出版",
    "SKIP_AMBIGUOUS_CITATION_AMENDMENTS": "跳过锚点仍有歧义的引文修正案",
    "KEEP_PRE_CITATION_TEXT": "保留本轮引文修改前的文本",
    "RETRY_INVALID_CITATION_VOTE": "重新请求该核校员提交完整票集",
    "EXCLUDE_INVALID_CITATION_VOTE": "将该核校员本轮票记为缺失票并继续",
    "PUBLISH_WITH_CITATION_WARNING": "保留当前正文并附带引文一致性警告",
    "APPROVE_RENDERING_SCOPE": "批准主席划定的局部重绘范围",
    "REVISE_RENDERING_SCOPE": "退回范围划分，并说明需要增删的部分",
    "RETURN_TO_WRITER_LOCAL_REPAIR": "要求主笔只修复本次指出的段落，再作一次局部核验",
    "PUBLISH_WITH_DISCLOSED_LIMITATION": "知悉具体科学疑点后保留当前稿，并在报告中列出限制",
    "PAUSE_FOR_MANUAL_REVIEW": "继续暂停，待人工复核具体科学疑点",
    "RETRY_WRITER_CITATION_REPAIR": "保留已完成工作，让主笔再次修复未解决的引文",
    "APPROVE_TASKBOOK": "批准任务书与模块划分，开始检索",
    "REVISE_TASKBOOK": "修改任务书或模块数量（最多 12 个）",
    "KEEP_APPROVED_SCOPE": "按已批准的范围继续，保留范围问题记录",
    "RETRY_WRITER_REVISION": "要求主笔再修订一次并复核",
    "RETRY_WRITER_LOCAL_REPAIR": "只针对当前异议局部修稿，再交科学复核",
    "ACCEPT_WITH_DISCLOSED_LIMITATION": "知悉未解决的科学异议，附限制说明后继续",
    "REMOVE_UNSUPPORTED_SYNTHESIS": "去除未获认可的跨模块结论后出版",
    "USE_WRITER_DEFAULT": "此项由主笔采用合理默认值，并在任务书标明",
    "APPROVE_SCOPE_CHANGE": "批准本模块提出的局部研究范围变更",
}


def consultation_option_label(option: str) -> str:
    return CONSULTATION_OPTION_LABELS.get(option, option)


def normalize_consultation_decision(value: str) -> str:
    normalized = value.strip()
    for code, label in CONSULTATION_OPTION_LABELS.items():
        if normalized in {code, label}:
            return code
    return normalized


class ThresholdRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    comparison: str = Field(min_length=1)
    eligible_count: int = Field(gt=0)
    required_votes: int = Field(gt=0)


class HumanConsultationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issue_id: str
    meeting_id: str
    reason_code: str
    stage: str
    question: str = Field(min_length=1)
    options: list[str] = Field(min_length=2)
    affected_items: list[str] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)
    thresholds: list[ThresholdRecord] = Field(default_factory=list)
    status: str = "OPEN"

    @field_validator("options")
    @classmethod
    def options_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("consultation options must be unique")
        return value


class HumanConsultationResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issue_id: str
    meeting_id: str
    decision: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    human_wording: str | None = Field(default=None, min_length=1)
    scope: str = Field(min_length=1)
    thresholds: list[ThresholdRecord]
    authority: Literal["HUMAN", "DELEGATED_CHAIR"] = "HUMAN"
    authorization_record_path: str | None = None

    @model_validator(mode="after")
    def wording_matches_decision(self) -> "HumanConsultationResolution":
        if self.decision == "USE_HUMAN_WORDING" and self.human_wording is None:
            raise ValueError("USE_HUMAN_WORDING requires separate final wording")
        if self.decision != "USE_HUMAN_WORDING" and self.human_wording is not None:
            raise ValueError("human wording is only valid for USE_HUMAN_WORDING")
        return self


class ChairDelegatedScienceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal[
        "ACCEPT_SCIENCE_OBJECTION",
        "REJECT_SCIENCE_OBJECTION",
        "DIRECT_CHAIR_SCIENCE_REVISION",
    ]
    rationale: str = Field(min_length=1)


class ScienceConsultationAuthorityService:
    """Human-controlled, append-only authorization for science consultations only."""

    def __init__(self, repo: MeetingRepository):
        self.repo = repo

    def _check_meeting(self) -> PublicMeetingManifest:
        manifest = PublicMeetingManifest.model_validate_json(
            (self.repo.root / "public/meeting_manifest.json").read_text(encoding="utf-8")
        )
        if manifest.meeting_type.value != "scholarly_rendering":
            raise ValueError("science consultation delegation is only available in scholarly rendering")
        return manifest

    def current(self) -> tuple[Literal["human", "chair"], str]:
        manifest = self._check_meeting()
        root = self.repo.root / "human_private/science_consultation_authority"
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._current_unlocked(manifest)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _current_unlocked(self, manifest: PublicMeetingManifest) -> tuple[Literal["human", "chair"], str]:
        root = self.repo.root / "human_private/science_consultation_authority"
        changes = sorted(root.glob("*.json"))
        if changes:
            latest = changes[-1]
            record = json.loads(latest.read_text(encoding="utf-8"))
            return record["authority"], str(latest.relative_to(self.repo.root))
        return manifest.rendering_science_consultation_authority, "public/meeting_manifest.json"

    def set_mode(self, authority: Literal["human", "chair"]) -> str:
        self._check_meeting()
        if authority not in {"human", "chair"}:
            raise ValueError("authority must be human or chair")
        root = self.repo.root / "human_private/science_consultation_authority"
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                changes = sorted(root.glob("*.json"))
                sequence = int(changes[-1].stem) + 1 if changes else 1
                relative = Path("human_private/science_consultation_authority") / f"{sequence:06d}.json"
                record = {
                    "meeting_id": self.repo.meeting_id,
                    "sequence": sequence,
                    "authority": authority,
                    "changed_by": "HUMAN",
                    "changed_at": datetime.now(timezone.utc).isoformat(),
                    "scope": "SCHOLARLY_RENDERING_SCIENCE_OBJECTIONS_ONLY",
                }
                content = json.dumps(record, indent=2, ensure_ascii=False)
                self.repo.docs.write_once(relative, content)
                self.repo.docs.write_once(
                    Path("public/procedural_consultations/science_authority_changes") / f"{sequence:06d}.json",
                    content,
                )
                return str(relative)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def resolve_delegated(self, service: "HumanConsultationService", *, issue_id: str,
                          decision: ChairDelegatedScienceDecision) -> HumanConsultationResolution | None:
        """Commit only while the Human's authorization still holds the lock."""
        manifest = self._check_meeting()
        root = self.repo.root / "human_private/science_consultation_authority"
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                mode, authorization_path = self._current_unlocked(manifest)
                if mode != "chair":
                    return None
                return service._resolve(
                    issue_id=issue_id,
                    decision=decision.decision,
                    rationale=decision.rationale,
                    scope="THIS_CONSULTATION_ONLY",
                    authority="DELEGATED_CHAIR",
                    authorization_record_path=authorization_path,
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ChairConsultationDialogueTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issue_id: str
    turn_number: int = Field(gt=0)
    human_question: str = Field(min_length=1)
    chair_answer: str = Field(min_length=1)

    @field_validator("human_question")
    @classmethod
    def question_is_one_terminal_entry(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("ask one Human question per dialogue turn")
        return value


class HumanConsultationService:
    """Immutable per-issue Human consultation and resolution records."""

    def __init__(self, repo: MeetingRepository):
        self.repo = repo

    @staticmethod
    def validate_issue_id(issue_id: str) -> str:
        if not re.fullmatch(r"HC-[A-Z0-9][A-Z0-9_-]{2,127}", issue_id):
            raise ValueError("invalid consultation issue ID")
        return issue_id

    def open_issue(self, issue: HumanConsultationIssue) -> tuple[Path, bool]:
        self.validate_issue_id(issue.issue_id)
        if issue.meeting_id != self.repo.meeting_id:
            raise ValueError("consultation meeting ID mismatch")
        private_relative = Path("human_private/consultations") / f"{issue.issue_id}.issue.json"
        private_path = self.repo.root / private_relative
        created = False
        if private_path.exists():
            persisted = HumanConsultationIssue.model_validate_json(private_path.read_text(encoding="utf-8"))
            if persisted != issue:
                raise ValueError("consultation issue ID already exists with different content")
        else:
            self.repo.docs.write_once(private_relative, issue.model_dump_json(indent=2))
            public_relative = Path("public/procedural_consultations") / f"{issue.issue_id}.json"
            self.repo.docs.write_once(
                public_relative,
                json.dumps(
                    {
                        "issue_id": issue.issue_id,
                        "reason_code": issue.reason_code,
                        "stage": issue.stage,
                        "question": issue.question,
                        "options": issue.options,
                        "affected_items": issue.affected_items,
                        "context": issue.context,
                        "thresholds": [x.model_dump(mode="json") for x in issue.thresholds],
                        "status": "OPEN",
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "HUMAN_CONSULTATION_OPENED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "issue_id": issue.issue_id,
                    "reason_code": issue.reason_code,
                    "record_path": str(private_relative),
                },
                actor="orchestrator" if issue.stage.startswith("FAST_") else "CHAIR",
            )
            created = True
        return private_path, created

    def resolution(self, issue_id: str) -> HumanConsultationResolution | None:
        self.validate_issue_id(issue_id)
        path = self.repo.root / "human_private/consultations" / f"{issue_id}.resolution.json"
        if not path.exists():
            return None
        return HumanConsultationResolution.model_validate_json(path.read_text(encoding="utf-8"))

    def open_issues(self) -> list[HumanConsultationIssue]:
        root = self.repo.root / "human_private/consultations"
        if not root.exists():
            return []
        issues: list[HumanConsultationIssue] = []
        for path in sorted(root.glob("*.issue.json"), key=lambda item: (item.stat().st_mtime_ns, item.name)):
            issue = HumanConsultationIssue.model_validate_json(path.read_text(encoding="utf-8"))
            superseded = root / f"{issue.issue_id}.superseded.json"
            withdrawn = root / f"{issue.issue_id}.withdrawn.json"
            if self.resolution(issue.issue_id) is None and not superseded.exists() and not withdrawn.exists():
                issues.append(issue)
        return issues

    def ask_chair_to_decide_science_objection(
        self, *, issue: HumanConsultationIssue, engine: "MeetingEngine",
        max_output_tokens: int | None = None,
    ) -> HumanConsultationResolution | None:
        """Decide one authorized science objection; never impersonate a Human ruling."""
        authority = ScienceConsultationAuthorityService(self.repo)
        if authority.current()[0] != "chair":
            return None
        if (issue.stage != "SCHOLARLY_SCIENCE_REVIEW"
                or issue.reason_code != "SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED"):
            return None
        if self.resolution(issue.issue_id) is not None:
            return self.resolution(issue.issue_id)
        system_text = (
            "本次学术重绘会议的人类已授权主席代裁科学事实异议。"
            "只裁决当前这一条异议，不能改变其他人工咨询的权限。"
            "比较冻结原文、当前重绘稿、审阅异议和已有证据；避免无证据的新科学结论。"
            "可以采纳异议、驳回异议，或给自己一个具体的局部修稿方向。"
            "理由简短、具体，不得声称人类亲自作出了本条决定。"
        )
        user_text = (
            json.dumps(
                {"issue": issue.model_dump(mode="json"),
                 "related_public_records": self._related_public_records(issue)},
                ensure_ascii=False, sort_keys=True,
            )
            + "\n\n只返回符合以下 JSON schema 的对象：\n"
            + json.dumps(ChairDelegatedScienceDecision.model_json_schema(), ensure_ascii=False)
        )
        stage = f"delegated_science_consultation_{issue.issue_id}"
        response = engine.find_recorded_response(
            "CHAIR", system_text=system_text, user_text=user_text, stage=stage
        ) or engine.invoke_participant(
            "CHAIR", system_text=system_text, user_text=user_text,
            stage=stage, max_output_tokens=max_output_tokens,
        )
        decision = engine.validate_structured_response(
            "CHAIR", response=response, schema_model=ChairDelegatedScienceDecision,
            stage=stage, max_output_tokens=max_output_tokens,
            semantic_requirement="只对这一条科学事实异议给出可执行的简短裁决。",
        )
        return authority.resolve_delegated(self, issue_id=issue.issue_id, decision=decision)

    def withdraw(self, *, issue_id: str, reason: str) -> None:
        """Archive an unanswered question invalidated by a later frozen correction."""
        self.validate_issue_id(issue_id)
        root = Path("human_private/consultations")
        if not (self.repo.root / root / f"{issue_id}.issue.json").exists():
            raise ValueError(f"unknown consultation issue {issue_id}")
        if self.resolution(issue_id) is not None:
            raise ValueError("a resolved consultation cannot be withdrawn")
        if (self.repo.root / root / f"{issue_id}.superseded.json").exists():
            raise ValueError("a superseded consultation cannot be withdrawn")
        relative = root / f"{issue_id}.withdrawn.json"
        record = {"issue_id": issue_id, "reason": reason}
        if (self.repo.root / relative).exists():
            if json.loads((self.repo.root / relative).read_text(encoding="utf-8")) != record:
                raise ValueError("withdrawn consultation reason conflicts with frozen record")
            return
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.docs.write_once(
            Path("public/procedural_consultations") / f"{issue_id}.withdrawn.json",
            json.dumps(record, indent=2, ensure_ascii=False),
        )
        self.repo.events.append(
            "HUMAN_CONSULTATION_WITHDRAWN",
            {"meeting_id": self.repo.meeting_id, **record, "record_path": str(relative)},
            actor="orchestrator",
        )

    def supersede(self, *, issue_id: str, successor_issue_id: str, reason: str) -> None:
        self.validate_issue_id(issue_id)
        self.validate_issue_id(successor_issue_id)
        root = Path("human_private/consultations")
        issue_path = self.repo.root / root / f"{issue_id}.issue.json"
        if not issue_path.exists():
            raise ValueError(f"unknown consultation issue {issue_id}")
        if self.resolution(issue_id) is not None:
            raise ValueError("a resolved consultation cannot be superseded")
        if (self.repo.root / root / f"{issue_id}.withdrawn.json").exists():
            raise ValueError("a withdrawn consultation cannot be superseded")
        relative = root / f"{issue_id}.superseded.json"
        if not (self.repo.root / relative).exists():
            record = {
                "issue_id": issue_id,
                "successor_issue_id": successor_issue_id,
                "reason": reason,
            }
            self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
            self.repo.docs.write_once(
                Path("public/procedural_consultations") / f"{issue_id}.superseded.json",
                json.dumps(record, indent=2, ensure_ascii=False),
            )
            self.repo.events.append(
                "HUMAN_CONSULTATION_SUPERSEDED",
                {"meeting_id": self.repo.meeting_id, **record, "record_path": str(relative)},
                actor="orchestrator",
            )

    def dialogue(self, issue_id: str) -> list[ChairConsultationDialogueTurn]:
        self.validate_issue_id(issue_id)
        root = self.repo.root / "human_private/consultations" / issue_id / "dialogue"
        if not root.exists():
            return []
        return [
            ChairConsultationDialogueTurn.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(root.glob("turn_*.json"))
        ]

    def ask_chair(
        self,
        *,
        issue_id: str,
        question: str,
        engine: "MeetingEngine",
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
    ) -> ChairConsultationDialogueTurn:
        self.validate_issue_id(issue_id)
        issue_path = self.repo.root / "human_private/consultations" / f"{issue_id}.issue.json"
        if not issue_path.exists():
            raise ValueError(f"unknown consultation issue {issue_id}")
        issue = HumanConsultationIssue.model_validate_json(issue_path.read_text(encoding="utf-8"))
        if self.resolution(issue_id) is not None:
            raise ValueError("cannot ask Chair after the consultation was resolved")
        if (self.repo.root / "human_private/consultations" / f"{issue_id}.withdrawn.json").exists():
            raise ValueError("cannot ask Chair about a withdrawn consultation")
        if "\n" in question or "\r" in question or not question.strip():
            raise ValueError("ask exactly one non-empty question per dialogue turn")
        if issue.stage == "SCHOLARLY_RENDERING_SCOPE":
            system_text = (
                "向人类解释局部重绘的范围划分。只能说明原文块为何被选中或保留、"
                "边界歧义和不同选择的后果；不能代替人类批准范围，也不能修改原文。"
            )
        elif issue.stage == "SCHOLARLY_SCIENCE_REVIEW":
            system_text = (
                "当前调用承担学术重绘会议主席的程序解释职责。人类拥有本咨询的最终决定权。"
                "只解释原文、重绘稿、科学审阅异议、证据状态和可行的局部修订方式；"
                "不得替人类作决定、改变冻结来源会议的结论、隐藏异议或推断审阅者身份。"
            )
        else:
            governance_root = Path(governance_docs)
            system_text = "\n\n".join(
                path.read_text(encoding="utf-8")
                for path in (
                    governance_root / "03_roles/chair/chair_role.md",
                    governance_root / "07_runtime_memory/other_participants/deliberation_chair.md",
                )
            )
        prior = [turn.model_dump(mode="json") for turn in self.dialogue(issue_id)]
        related = self._related_public_records(issue)
        if issue.stage == "SCHOLARLY_RENDERING_SCOPE":
            instruction = (
                "人类正在询问当前局部重绘范围。根据自然语言任务、候选清单和原文块标题，"
                "具体解释被选中和被保留的边界。若边界不当，请指出可写入退回理由的调整，"
                "但不代替人类批准或改写候选方案。每次只回答这一条问题。"
            )
        elif issue.stage == "SCHOLARLY_SCIENCE_REVIEW":
            instruction = (
                "人类正在就学术重绘的科学事实异议提出一条问题。忠实解释原文、当前重绘稿、"
                "这条异议、你的建议及不同处理方式的后果。不要替人类决定、直接改稿、"
                "增加未经核验的科学结论或隐瞒反对意见；只回答当前这一条问题。"
            )
        elif issue.stage == "LITERATURE_RESEARCH_OUTLINE":
            instruction = (
                "Task: explain one Human question about the frozen literature-review outline and any "
                "scope objections raised by planning Representatives. Relay the objections faithfully, "
                "explain your own scope assessment and the consequences of approving or rejecting the "
                "outline, and help the Human clarify intent. Neither the Representatives nor Chair may "
                "narrow the original task without a Human decision. Dialogue does not edit the frozen "
                "outline: a requested scope change requires REJECT_AND_REPLAN with a specific rationale. "
                "Do not decide for the Human or conceal an objection. Answer only this question."
            )
        else:
            instruction = (
                "The Human is asking one natural-language question about an open procedural consultation. "
                "Explain the procedural meaning, alternatives, thresholds, and consequences in clear language. "
                "You may identify an apparent classification error and explain what reconsideration would require. "
                "Do not decide for the Human, express a substantive preference, create a new proposal, or reveal "
                "private Representative identities or evaluation rules. Answer only the single current question."
            )
        response = engine.invoke_participant(
            "CHAIR",
            system_text=system_text,
            user_text=(
                instruction
                + "\n\n"
                "CONSULTATION:\n"
                + issue.model_dump_json(indent=2)
                + "\n\nRELATED PUBLIC RECORDS:\n"
                + json.dumps(related, indent=2, ensure_ascii=False)
                + "\n\nPRIOR HUMAN-CHAIR DIALOGUE:\n"
                + json.dumps(prior, indent=2, ensure_ascii=False)
                + "\n\nHUMAN QUESTION:\n"
                + question.strip()
            ),
            stage="human_consultation_explanation",
            max_output_tokens=max_output_tokens,
        )
        turn = ChairConsultationDialogueTurn(
            issue_id=issue_id,
            turn_number=len(prior) + 1,
            human_question=question.strip(),
            chair_answer=response.text.strip(),
        )
        relative = (
            Path("human_private/consultations")
            / issue_id
            / "dialogue"
            / f"turn_{turn.turn_number:03d}.json"
        )
        self.repo.docs.write_once(relative, turn.model_dump_json(indent=2))
        self.repo.events.append(
            "HUMAN_CHAIR_CONSULTATION_TURN_RECORDED",
            {
                "meeting_id": self.repo.meeting_id,
                "issue_id": issue_id,
                "turn_number": turn.turn_number,
                "record_path": str(relative),
            },
            actor="CHAIR",
        )
        return turn

    def _related_public_records(self, issue: HumanConsultationIssue) -> dict:
        if issue.stage == "SCHOLARLY_SCIENCE_REVIEW":
            match = re.fullmatch(r"HC-SR-(SR-\d+)-C(\d+)-O(\d+)(-CLARIFIED)?", issue.issue_id)
            if match is not None:
                section_id, cycle, objection, clarified = match.groups()
                context = (
                    self.repo.root / "public/scholarly_rendering/sections" / section_id
                    / f"science_human_context_c{cycle}_o{objection}{'_clarified' if clarified else ''}.json"
                )
                if context.is_file():
                    return json.loads(context.read_text(encoding="utf-8"))
            return {}
        if issue.stage == "LITERATURE_RESEARCH_OUTLINE":
            match = re.fullmatch(r"HC-LROUTLINE-C(\d+)", issue.issue_id)
            if match is None:
                return {}
            cycle = int(match.group(1))
            suffix = "" if cycle == 1 else f"-C{cycle:03d}"
            task_path = self.repo.root / "public/task.json"
            outline_path = (
                self.repo.root
                / f"public/literature_report/frozen_research_outline{suffix}.json"
            )
            submissions_path = (
                self.repo.root
                / f"public/literature_report/decomposition_submissions{suffix}.json"
            )
            records: dict[str, object] = {}
            if task_path.exists():
                records["original_task"] = json.loads(task_path.read_text(encoding="utf-8"))
            if outline_path.exists():
                records["frozen_outline"] = json.loads(
                    outline_path.read_text(encoding="utf-8")
                )
            if submissions_path.exists():
                submissions = json.loads(submissions_path.read_text(encoding="utf-8"))
                records["representative_scope_objections"] = [
                    {
                        "representative_id": item.get("representative_id"),
                        "objection": item["submission"]["scope_objection"],
                    }
                    for item in submissions.get("submissions", [])
                    if item.get("submission", {}).get("scope_objection")
                ]
            return records
        wanted = set(issue.affected_items)
        records: dict[str, object] = {"amendments": [], "chair_dispositions": []}
        for path in sorted((self.repo.root / "public/general_principle").glob("amendment_docket_window_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            records["amendments"].extend(
                item for item in data.get("amendments", []) if item.get("amendment_id") in wanted
            )
        for path in sorted((self.repo.root / "public/general_principle/chair_plans").rglob("step_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            records["chair_dispositions"].extend(
                item for item in data.get("dispositions", []) if item.get("amendment_id") in wanted
            )
        return records

    def resolve(
        self,
        *,
        issue_id: str,
        decision: str,
        rationale: str,
        scope: str,
        human_wording: str | None = None,
    ) -> HumanConsultationResolution:
        return self._resolve(
            issue_id=issue_id, decision=decision, rationale=rationale,
            scope=scope, human_wording=human_wording,
            authority="HUMAN", authorization_record_path=None,
        )

    def _resolve(
        self, *, issue_id: str, decision: str, rationale: str, scope: str,
        human_wording: str | None = None,
        authority: Literal["HUMAN", "DELEGATED_CHAIR"],
        authorization_record_path: str | None,
    ) -> HumanConsultationResolution:
        self.validate_issue_id(issue_id)
        issue_path = self.repo.root / "human_private/consultations" / f"{issue_id}.issue.json"
        if not issue_path.exists():
            raise ValueError(f"unknown consultation issue {issue_id}")
        issue = HumanConsultationIssue.model_validate_json(issue_path.read_text(encoding="utf-8"))
        if (self.repo.root / "human_private/consultations" / f"{issue_id}.superseded.json").exists():
            raise ValueError("consultation was superseded; resolve its successor instead")
        if (self.repo.root / "human_private/consultations" / f"{issue_id}.withdrawn.json").exists():
            raise ValueError("consultation was withdrawn after ballot clarification")
        if decision not in issue.options:
            raise ValueError(f"decision must be one of: {', '.join(issue.options)}")
        if decision == "APPROVE_OUTLINE" and "proposed_disciplines" in issue.context:
            cycle = int(issue.context["cycle"])
            profile_path = (
                self.repo.root / "public/literature_report"
                / f"audience_profile-C{cycle:03d}.json"
            )
            if not profile_path.exists():
                raise ValueError("outline approval requires a frozen Human audience profile")
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            if profile.get("outline_issue_id") != issue_id:
                raise ValueError("audience profile belongs to a different outline issue")
        if authority == "DELEGATED_CHAIR" and (
            issue.stage != "SCHOLARLY_SCIENCE_REVIEW"
            or issue.reason_code != "SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED"
            or decision not in {
                "ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION",
                "DIRECT_CHAIR_SCIENCE_REVISION",
            }
            or not authorization_record_path
        ):
            raise ValueError("Chair delegation is limited to scholarly science objections")
        resolution = HumanConsultationResolution(
            issue_id=issue_id,
            meeting_id=self.repo.meeting_id,
            decision=decision,
            rationale=rationale,
            human_wording=human_wording,
            scope=scope,
            thresholds=issue.thresholds,
            authority=authority,
            authorization_record_path=authorization_record_path,
        )
        relative = Path("human_private/consultations") / f"{issue_id}.resolution.json"
        self.repo.docs.write_once(relative, resolution.model_dump_json(indent=2))
        public_relative = Path("public/procedural_consultations") / f"{issue_id}.resolution.json"
        self.repo.docs.write_once(
            public_relative,
            json.dumps(
                {
                    "issue_id": issue_id,
                    "decision": decision,
                    "human_wording": human_wording,
                    "scope": scope,
                    "thresholds": [x.model_dump(mode="json") for x in issue.thresholds],
                    "status": "RESOLVED",
                    "authority": authority,
                    "authorization_record_path": authorization_record_path,
                    **({"chair_rationale": rationale} if authority == "DELEGATED_CHAIR" else {}),
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        self.repo.events.append(
            "HUMAN_CONSULTATION_RESOLVED" if authority == "HUMAN" else "DELEGATED_CHAIR_CONSULTATION_RESOLVED",
            {
                "meeting_id": self.repo.meeting_id,
                "issue_id": issue_id,
                "decision": decision,
                "scope": scope,
                "record_path": str(relative),
                "authorization_record_path": authorization_record_path,
            },
            actor="HUMAN" if authority == "HUMAN" else "CHAIR",
        )
        return resolution
