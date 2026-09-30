from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.labels import persona_display_label
from project_ensemble.runtime.model_lanes import run_bounded_model_lanes
from project_ensemble.storage.human_outputs import ensure_visible_link
from project_ensemble.storage.meeting import MeetingRepository


class AuditPetitionItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    involved_procedure: str = Field(min_length=1)
    observed_problem: str = Field(min_length=1)
    why_procedural_not_substantive: str = Field(min_length=1)
    institutional_consequence: str = Field(min_length=1)
    references: list[str] = Field(min_length=1)


class MinorityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    preserved_dissent: str = Field(min_length=1)
    alternative: str = Field(min_length=1)
    predicted_failure_mode: str = Field(min_length=1)
    suggested_future_test: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class PostMeetingSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    representative_id: str = Field(pattern=r"^R-[0-9A-F]+$")
    requests_audit: bool
    audit_petitions: list[AuditPetitionItem] = Field(default_factory=list)
    minority_report: MinorityReport | None = None

    @model_validator(mode="after")
    def request_matches_petitions(self) -> "PostMeetingSubmission":
        if self.requests_audit != bool(self.audit_petitions):
            raise ValueError("requests_audit must exactly match whether petitions were submitted")
        return self


class PostMeetingAccountabilityResult(BaseModel):
    minority_report_count: int
    audit_requester_count: int
    audit_petition_count: int
    audit_petition_bundle_path: str
    chair_accountability_report_path: str
    chair_accountability_cli_brief_path: str


class ChairAccountabilityDebrief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    cli_brief: str = Field(min_length=1, max_length=1200)
    full_report_markdown: str = Field(min_length=1)


class PostMeetingAccountabilityRunner:
    """Freeze post-meeting rights, then produce the Chair's Human-only debrief."""

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
        self.governance_docs = Path(governance_docs)

    def run(
        self,
        *,
        final_report_path: Path,
        publication_manifest_path: Path,
        certification_path: Path | None = None,
    ) -> PostMeetingAccountabilityResult:
        minority_path, petition_path, bundle = self._collect_post_meeting_submissions(
            final_report_path=final_report_path,
            certification_path=certification_path,
        )
        evidence_path = self._freeze_performance_evidence(
            petition_bundle=bundle,
            publication_manifest_path=publication_manifest_path,
        )
        report_path, cli_brief_path = self._chair_debrief(
            final_report_path=final_report_path,
            publication_manifest_path=publication_manifest_path,
            minority_bundle_path=minority_path,
            petition_bundle_path=petition_path,
            performance_evidence_path=evidence_path,
        )
        self._publish_human_entry_points(
            chair_report_path=report_path,
            petition_bundle_path=petition_path,
            cli_brief_path=cli_brief_path,
            publication_manifest_path=publication_manifest_path,
        )
        return PostMeetingAccountabilityResult(
            minority_report_count=int(bundle["minority_report_count"]),
            audit_requester_count=int(bundle["audit_requester_count"]),
            audit_petition_count=int(bundle["audit_petition_count"]),
            audit_petition_bundle_path=str(petition_path.relative_to(self.repo.root)),
            chair_accountability_report_path=str(report_path.relative_to(self.repo.root)),
            chair_accountability_cli_brief_path=str(
                cli_brief_path.relative_to(self.repo.root)
            ),
        )

    def _collect_post_meeting_submissions(
        self,
        *,
        final_report_path: Path,
        certification_path: Path | None = None,
    ) -> tuple[Path, Path, dict]:
        minority_relative = Path("public/final/minority_reports.json")
        petition_relative = Path("human_private/final/audit_petitions.json")
        minority_path = self.repo.root / minority_relative
        petition_path = self.repo.root / petition_relative
        if minority_path.exists() and petition_path.exists():
            petitions = json.loads(petition_path.read_text(encoding="utf-8"))
            return minority_path, petition_path, petitions
        if minority_path.exists() != petition_path.exists():
            raise ValueError("post-meeting freeze is incomplete: only one bundle exists")

        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        transition = json.loads(
            self.repo.docs.read_text("identity_private/general_principle/status_transition.json")
        )
        statuses = transition["representative_statuses"]
        self.engine.status.phase = MeetingPhase.HUMAN_REVIEW
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.HUMAN_REVIEW,
            f"开启 {len(registry)} 名 Representative 的会后密封提交窗口：少数意见与审计申请",
        )
        submissions_by_id: dict[str, PostMeetingSubmission] = {}
        missing: list[dict] = []
        for index, record in enumerate(registry, start=1):
            representative_id = record["representative_id"]
            relative = Path("governance_private/post_meeting/submissions") / f"{representative_id}.json"
            absolute = self.repo.root / relative
            if absolute.exists():
                submissions_by_id[representative_id] = PostMeetingSubmission.model_validate_json(
                    absolute.read_text(encoding="utf-8")
                )
                self.engine.progress.info(
                    f"已恢复 {index}/{len(registry)} 份完整会后密封提交"
                )
            else:
                missing.append(record)

        # Calls sharing one provider/model lane are serialized in Persona order, while
        # independent model lanes run concurrently. This is the same collision-avoidance
        # rule used by Research Round sealed submissions.
        lanes: dict[tuple[str, str], list[dict]] = {}
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
            representative_view_path = self._post_meeting_representative_view(
                final_report_path
            )

            persistence_lock = threading.Lock()

            def persist_submission(
                result: tuple[str, PostMeetingSubmission],
            ) -> None:
                representative_id, submission = result
                relative = (
                    Path("governance_private/post_meeting/submissions")
                    / f"{representative_id}.json"
                )
                with persistence_lock:
                    self.repo.docs.write_once(
                        relative, submission.model_dump_json(indent=2)
                    )
                    self.repo.events.append(
                        "POST_MEETING_SEALED_SUBMISSION_RECORDED",
                        {
                            "meeting_id": self.repo.meeting_id,
                            "representative_id": representative_id,
                            "record_path": str(relative),
                        },
                        actor=representative_id,
                    )
                    submissions_by_id[representative_id] = submission
                    self.engine.progress.info(
                        f"已收到 {len(submissions_by_id)}/{len(registry)} 份会后密封提交"
                    )

            run_bounded_model_lanes(
                lanes,
                lambda record: self._collect_one_submission(
                    record=record,
                    current_status=statuses[record["representative_id"]],
                    final_report_path=final_report_path,
                    representative_view_path=representative_view_path,
                    certification_path=certification_path,
                ),
                getattr(
                    self.engine,
                    "model_concurrency_limit",
                    lambda _provider_id, _model_id: 1,
                ),
                on_result=persist_submission,
                progress=self.engine.progress,
            )

        submissions = [submissions_by_id[item["representative_id"]] for item in registry]

        minority_reports = [
            {
                "representative_id": item.representative_id,
                "report": item.minority_report.model_dump(mode="json"),
            }
            for item in submissions
            if item.minority_report is not None
        ]
        minority_bundle = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_status": "CONSULTATIVE",
            "report_count": len(minority_reports),
            "reports": minority_reports,
            "authority_limit": "ADVISORY_ONLY_NO_AUTOMATIC_REOPEN",
        }
        self.repo.docs.write_once(
            minority_relative,
            json.dumps(minority_bundle, indent=2, ensure_ascii=False),
        )
        self.repo.events.append(
            "POST_MEETING_MINORITY_MATERIAL_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "report_count": len(minority_reports),
                "record_path": str(minority_relative),
            },
            actor="orchestrator",
        )

        petitions = []
        requester_ids = []
        for submission in submissions:
            if submission.requests_audit:
                requester_ids.append(submission.representative_id)
            for number, petition in enumerate(submission.audit_petitions, start=1):
                petitions.append(
                    {
                        "petition_id": f"P-{submission.representative_id[2:]}-{number:02d}",
                        "petitioner_rep_id": submission.representative_id,
                        **petition.model_dump(mode="json"),
                    }
                )
        petition_bundle = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_representative_count": len(registry),
            "submission_count": len(submissions),
            "audit_requester_count": len(requester_ids),
            "audit_petition_count": len(petitions),
            "requester_ids": requester_ids,
            "petitions": petitions,
            "minority_report_count": len(minority_reports),
            "automatic_effect": "NONE_HUMAN_DECIDES_WHETHER_TO_CONVENE_AUDIT",
        }
        self.repo.docs.write_once(
            petition_relative,
            json.dumps(petition_bundle, indent=2, ensure_ascii=False),
        )
        self.repo.events.append(
            "POST_MEETING_AUDIT_PETITIONS_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "eligible_representative_count": len(registry),
                "submission_count": len(submissions),
                "audit_requester_count": len(requester_ids),
                "audit_petition_count": len(petitions),
                "record_path": str(petition_relative),
            },
            actor="orchestrator",
        )
        return minority_path, petition_path, petition_bundle

    def _collect_one_submission(
        self,
        *,
        record: dict,
        current_status: str,
        final_report_path: Path,
        representative_view_path: Path | None = None,
        certification_path: Path | None = None,
    ) -> tuple[str, PostMeetingSubmission]:
        representative_id = record["representative_id"]
        own_files = self._own_history_files(representative_id)
        context_report_path = representative_view_path or final_report_path
        context_report = context_report_path.read_text(encoding="utf-8")
        publication_includes_reviews = (
            "# 附录 A：" in context_report and "# 附录 B：" in context_report
        )
        public_files = tuple(
            path
            for path in (
                self.repo.root / "public/task.json",
                context_report_path,
                self.repo.root / "public/general_principle/status_transition.json",
                certification_path
                or self.repo.root / "public/final/chair_procedural_certification.json",
                *(
                    ()
                    if publication_includes_reviews
                    else (
                        self.repo.root
                        / "public/final/think_tank_epistemic_reviews.json",
                        self.repo.root
                        / "public/final/think_tank_execution_reviews.json",
                    )
                ),
            )
            if path.exists()
        )
        spec = self.resolver.representative_context_spec(
            persona=Persona(record["runtime"]["persona"]),
            stage="post_meeting_submission",
            representative_id=representative_id,
            public_state_files=public_files,
            own_state_files=own_files,
        )
        system_text = self.assembler.assemble(spec)
        user_text = (
            "Submit your isolated post-meeting record. Return exactly one JSON object with "
            "representative_id, requests_audit, audit_petitions, and minority_report. Set "
            "requests_audit=true exactly when audit_petitions is non-empty. Each audit petition "
            "must contain involved_procedure, observed_problem, why_procedural_not_substantive, "
            "institutional_consequence, and references. Keep every field concise. "
            f"Your Representative ID is {representative_id}; your frozen status is {current_status}. "
            + (
                "As a CONSULTATIVE Representative, you may include one minority_report with "
                "preserved_dissent, alternative, predicted_failure_mode, suggested_future_test, "
                "and evidence_refs; use null if you have none."
                if current_status == "CONSULTATIVE"
                else "As an ACTIVE Representative, minority_report must be null."
            )
            + " Do not use Markdown."
        )
        response = self.engine.find_recorded_response(
            representative_id,
            system_text=system_text,
            user_text=user_text,
            stage="post_meeting_sealed_submission",
        )
        if response is None and context_report_path != final_report_path:
            # Compatibility for a call completed immediately before the bounded
            # view was introduced.  A response produced from the complete report
            # is at least as informed as the new view and can be safely retained.
            legacy_public_files = tuple(
                path
                for path in (
                    self.repo.root / "public/task.json",
                    final_report_path,
                    self.repo.root
                    / "public/general_principle/status_transition.json",
                    certification_path
                    or self.repo.root
                    / "public/final/chair_procedural_certification.json",
                    self.repo.root
                    / "public/final/think_tank_epistemic_reviews.json",
                    self.repo.root
                    / "public/final/think_tank_execution_reviews.json",
                )
                if path.exists()
            )
            legacy_spec = self.resolver.representative_context_spec(
                persona=Persona(record["runtime"]["persona"]),
                stage="post_meeting_submission",
                representative_id=representative_id,
                public_state_files=legacy_public_files,
                own_state_files=own_files,
            )
            legacy_system_text = self.assembler.assemble(legacy_spec)
            response = self.engine.find_recorded_response(
                representative_id,
                system_text=legacy_system_text,
                user_text=user_text,
                stage="post_meeting_sealed_submission",
            )
            if response is not None:
                self.engine.progress.info(
                    f"已恢复 {representative_id} 使用完整最终报告生成的会后密封响应"
                )
        if response is None:
            response = self.engine.invoke_participant(
                representative_id,
                system_text=system_text,
                user_text=user_text,
                stage="post_meeting_sealed_submission",
                max_output_tokens=self.max_output_tokens,
            )
        else:
            self.engine.progress.info(
                f"已恢复 {representative_id} 的会后密封提交原始响应"
            )
        submission = self.engine.validate_structured_response(
            representative_id,
            response=response,
            schema_model=PostMeetingSubmission,
            stage="post_meeting_sealed_submission",
            max_output_tokens=self.max_output_tokens,
            semantic_requirement=(
                "Audit petitions must be procedural rather than disagreement with a losing "
                "substantive option; ACTIVE Representatives cannot submit a minority report."
            ),
        )
        if submission.representative_id != representative_id:
            raise ValueError("post-meeting submission uses the wrong Representative ID")
        if current_status != "CONSULTATIVE" and submission.minority_report is not None:
            self.engine.pause_for_unconfigured_policy(
                participant_id=representative_id,
                reason_code="INELIGIBLE_MINORITY_REPORT_SUBMISSION",
            )
        return representative_id, submission

    def _post_meeting_representative_view(self, final_report_path: Path) -> Path:
        """Exclude the literature catalogue from the bounded post-meeting prompt.

        The complete final publication and literature bundle remain unchanged and
        Human-visible.  Representatives deciding whether to file a procedural audit
        petition need the certified body and review findings, not hundreds of repeated
        evidence-packet bibliography entries already preserved in public artifacts.
        """

        source = final_report_path.read_text(encoding="utf-8")
        appendix_c = "# 附录 C：会议文献证据与参考文献"
        appendix_d = "# 附录 D：来源与完整性"
        start = source.find(appendix_c)
        end = source.find(appendix_d, start + len(appendix_c)) if start >= 0 else -1
        if start < 0 or end < 0:
            return final_report_path

        source_relative = final_report_path.relative_to(self.repo.root)
        source_sha = self._sha256(source)
        omitted = source[start:end]
        notice = (
            f"{appendix_c}\n\n"
            "本节未注入会后代表的模型上下文。完整证据包索引、参考文献和可下载原文仍保存在"
            f" `{source_relative}` 与会议顶层 `LITERATURE_BUNDLE.zip`；省略不表示证据不存在。\n\n"
            f"- 完整最终报告 SHA-256：`{source_sha}`\n"
            f"- 本阶段省略字符数：`{len(omitted)}`\n"
            "- 省略范围：仅附录 C；正文、程序认证结果、附录 A/B 审查意见和附录 D 完整性信息均保留。\n\n"
        )
        view = source[:start] + notice + source[end:]
        relative = Path("public/final/post_meeting_representative_view.md")
        provenance_relative = Path(
            "public/final/post_meeting_representative_view.provenance.json"
        )
        absolute = self.repo.root / relative
        provenance = {
            "meeting_id": self.repo.meeting_id,
            "purpose": "BOUNDED_POST_MEETING_REPRESENTATIVE_CONTEXT",
            "source_path": str(source_relative),
            "source_sha256": source_sha,
            "view_path": str(relative),
            "view_sha256": self._sha256(view),
            "omitted_section": appendix_c,
            "omitted_character_count": len(omitted),
            "retained_sections": [
                "CERTIFIED_BODY",
                "APPENDIX_A_EPISTEMIC_REVIEWS",
                "APPENDIX_B_EXECUTION_REVIEWS",
                "APPENDIX_D_PROVENANCE_AND_INTEGRITY",
            ],
            "authority": "DERIVED_CONTEXT_VIEW_ONLY_FINAL_PUBLICATION_UNCHANGED",
        }
        created = False
        if absolute.exists():
            if absolute.read_text(encoding="utf-8") != view:
                raise ValueError("post-meeting Representative context view conflicts with source")
        else:
            self.repo.docs.write_once(relative, view)
            created = True
        provenance_path = self.repo.root / provenance_relative
        rendered_provenance = json.dumps(provenance, indent=2, ensure_ascii=False)
        if provenance_path.exists():
            if provenance_path.read_text(encoding="utf-8") != rendered_provenance:
                raise ValueError("post-meeting Representative context provenance conflicts")
        else:
            self.repo.docs.write_once(provenance_relative, rendered_provenance)
            created = True
        if created:
            self.repo.events.append(
                "POST_MEETING_REPRESENTATIVE_CONTEXT_VIEW_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "source_path": str(source_relative),
                    "source_sha256": source_sha,
                    "record_path": str(relative),
                    "view_sha256": provenance["view_sha256"],
                    "omitted_section": appendix_c,
                    "omitted_character_count": len(omitted),
                    "provenance_path": str(provenance_relative),
                },
                actor="orchestrator",
            )
        return absolute

    def _chair_debrief(
        self,
        *,
        final_report_path: Path,
        publication_manifest_path: Path,
        minority_bundle_path: Path,
        petition_bundle_path: Path,
        performance_evidence_path: Path,
    ) -> tuple[Path, Path]:
        relative = Path("human_private/final/chair_accountability_report.md")
        absolute = self.repo.root / relative
        brief_relative = Path("human_private/final/chair_accountability_cli_brief.txt")
        brief_path = self.repo.root / brief_relative
        provenance_relative = Path(
            "human_private/final/chair_accountability_report.provenance.json"
        )
        if absolute.exists():
            if not (self.repo.root / provenance_relative).exists():
                raise ValueError("Chair accountability report exists without provenance")
            if not brief_path.exists():
                # Compatibility for a report frozen by the immediately preceding implementation.
                report_text = absolute.read_text(encoding="utf-8")
                self.repo.docs.write_once(
                    brief_relative,
                    self._mechanical_brief_from_report(report_text),
                )
            self.engine.progress.speech(
                "CHAIR",
                "会后述职简报（完整报告已落盘）",
                brief_path.read_text(encoding="utf-8"),
            )
            self.engine.progress.info(f"完整述职：{relative}")
            return absolute, brief_path

        self.engine.status.phase = MeetingPhase.HUMAN_REVIEW
        self.engine.progress.status(
            MeetingPhase.HUMAN_REVIEW,
            "全部会后提交已冻结；Chair 正在向 Human 作最终述职",
        )
        system_text = self._chair_system_text()
        user_text = (
            "Return exactly one JSON object with cli_brief and full_report_markdown. The cli_brief must "
            "be a self-contained command-line summary of no more than 6 short lines and 1200 characters, "
            "covering the outcome, participant performance at a high level, main process problems, and "
            "whether/how many Representatives requested audit intervention. The full_report_markdown is "
            "a concise final accountability debrief in the original task's primary language. This report "
            "is separate from and cannot alter the certified result. Cover: (1) what the "
            "meeting accomplished and its final state; (2) the main substantive, procedural, provider, "
            "and recovery problems encountered and how they affected the process; (3) one evidence-based "
            "performance paragraph for every Representative ID in PERFORMANCE EVIDENCE, including concrete "
            "contributions, strengths, limitations, and operational reliability; and (4) exactly which "
            "Representatives requested Audit Conference intervention, how many petitions they submitted, "
            "and the procedural issues raised. Distinguish zero petitions from missing submissions. Do not "
            "treat CONSULTATIVE status or a low trial Drafting Alignment score as proof of low ability. Do "
            "not infer hidden reasoning or model/persona identity. Clearly state that petitions do not "
            "automatically reopen the decision and Human controls whether an Audit Conference is convened. "
            "Use readable Markdown in full_report_markdown, do not reproduce the entire resolution, do "
            "not add keys, and do not use a Markdown fence around the JSON.\n\n"
            "FINAL PUBLICATION MANIFEST:\n"
            + publication_manifest_path.read_text(encoding="utf-8")
            + "\n\nPERFORMANCE EVIDENCE:\n"
            + performance_evidence_path.read_text(encoding="utf-8")
            + "\n\nFROZEN MINORITY REPORTS:\n"
            + minority_bundle_path.read_text(encoding="utf-8")
            + "\n\nFROZEN AUDIT PETITIONS:\n"
            + petition_bundle_path.read_text(encoding="utf-8")
        )
        response = self.engine.find_recorded_response(
            "CHAIR",
            system_text=system_text,
            user_text=user_text,
            stage="chair_final_accountability_debrief",
        )
        if response is None:
            response = self.engine.invoke_participant(
                "CHAIR",
                system_text=system_text,
                user_text=user_text,
                stage="chair_final_accountability_debrief",
                max_output_tokens=self.max_output_tokens,
            )
        else:
            self.engine.progress.info("已恢复 Chair 的最终述职原始响应")
        debrief = self.engine.validate_structured_response(
            "CHAIR",
            response=response,
            schema_model=ChairAccountabilityDebrief,
            stage="chair_final_accountability_debrief",
            max_output_tokens=self.max_output_tokens,
            semantic_requirement=(
                "Preserve both the short command-line brief and the complete Human accountability "
                "report; neither may alter the certified result or infer hidden identity/reasoning."
            ),
        )
        report = debrief.full_report_markdown.strip()
        brief = debrief.cli_brief.strip()
        roster = self._human_only_roster()
        rendered = (
            report
            + "\n\n---\n\n## 机器附录：Human-only 身份映射\n\n"
            + "本附录由 Orchestrator 在 Chair 完成述职后机械附加；Chair 未读取真实模型/人格映射。\n\n"
            + "\n".join(
                f"- `{item['representative_id']}`：`{item['provider_id']}:{item['model_id']}`；"
                f"职位 `{persona_display_label(item['persona'])}`；最终状态 `{item['status']}`"
                for item in roster
            )
            + "\n"
        )
        self.repo.docs.write_once(relative, rendered)
        self.repo.docs.write_once(brief_relative, brief + "\n")
        sources = {
            "final_report": str(final_report_path.relative_to(self.repo.root)),
            "publication_manifest": str(publication_manifest_path.relative_to(self.repo.root)),
            "minority_reports": str(minority_bundle_path.relative_to(self.repo.root)),
            "audit_petitions": str(petition_bundle_path.relative_to(self.repo.root)),
            "performance_evidence": str(performance_evidence_path.relative_to(self.repo.root)),
        }
        self.repo.docs.write_once(
            provenance_relative,
            json.dumps(
                {
                    "meeting_id": self.repo.meeting_id,
                    "report_path": str(relative),
                    "report_sha256": self._sha256(rendered),
                    "source_paths": sources,
                    "source_sha256": {
                        key: self._sha256((self.repo.root / value).read_text(encoding="utf-8"))
                        for key, value in sources.items()
                    },
                    "identity_mapping_visibility": "HUMAN_ONLY_MECHANICALLY_APPENDED_AFTER_CHAIR_RESPONSE",
                    "cli_brief_path": str(brief_relative),
                    "cli_brief_sha256": self._sha256(brief + "\n"),
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        self.repo.events.append(
            "CHAIR_FINAL_ACCOUNTABILITY_REPORT_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "record_path": str(relative),
                "provenance_path": str(provenance_relative),
                "report_sha256": self._sha256(rendered),
            },
            actor="CHAIR",
        )
        self.engine.progress.speech(
            "CHAIR", "会后述职简报（完整报告已落盘）", brief
        )
        self.engine.progress.info(f"完整述职：{relative}")
        return absolute, brief_path

    def _publish_human_entry_points(
        self,
        *,
        chair_report_path: Path,
        petition_bundle_path: Path,
        cli_brief_path: Path,
        publication_manifest_path: Path,
    ) -> None:
        aliases = {
            "CHAIR_DEBRIEF.md": chair_report_path.relative_to(self.repo.root),
            "CHAIR_BRIEF.txt": cli_brief_path.relative_to(self.repo.root),
            "AUDIT_PETITIONS.json": petition_bundle_path.relative_to(self.repo.root),
        }
        access_ledger = self.repo.root / "governance_private/representative_file_access.jsonl"
        if access_ledger.exists():
            aliases["REPRESENTATIVE_FILE_ACCESS.jsonl"] = access_ledger.relative_to(
                self.repo.root
            )
        created = []
        for link_name, target in aliases.items():
            _path, was_created = ensure_visible_link(
                self.repo.root,
                link_name=link_name,
                target_relative=target,
            )
            if was_created:
                created.append(link_name)

        publication = json.loads(publication_manifest_path.read_text(encoding="utf-8"))
        literature_line = ""
        if (self.repo.root / "LITERATURE_BUNDLE.zip").exists():
            literature_line = "- `LITERATURE_BUNDLE.zip`：本次会议的可下载文献包。\n"
        index_text = (
            f"# {self.repo.meeting_id} · 会议成果入口\n\n"
            "以下是位于会议目录顶层的 Human 入口；冻结原件仍保存在分区目录中。\n\n"
            "- `FINAL_REPORT.pdf`：最终正文与审阅附录，适合直接阅读或下载。\n"
            "- `FINAL_REPORT.md`：与 PDF 对应的冻结 Markdown。\n"
            "- `CHAIR_BRIEF.txt`：主席会后述职的命令行简报。\n"
            "- `CHAIR_DEBRIEF.md`：主席完整会后述职及 Human-only 身份映射。\n"
            "- `AUDIT_PETITIONS.json`：代表提出的会后程序审计申请。\n"
            "- `REPRESENTATIVE_FILE_ACCESS.jsonl`：代表文件读取与拒绝记录的哈希链账本。\n"
            + literature_line
            + "\n## 冻结原件\n\n"
            f"- PDF：`{publication['report_pdf_path']}`\n"
            f"- 述职：`{chair_report_path.relative_to(self.repo.root)}`\n"
            f"- 文件访问账本：`governance_private/representative_file_access.jsonl`\n"
        )
        index_relative = Path("MEETING_RESULTS.md")
        index_path = self.repo.root / index_relative
        if index_path.exists():
            if index_path.read_text(encoding="utf-8") != index_text:
                raise ValueError("visible meeting-results index conflicts with frozen outputs")
        else:
            self.repo.docs.write_once(index_relative, index_text)
            created.append(str(index_relative))
        if created:
            self.repo.events.append(
                "HUMAN_VISIBLE_FINAL_ENTRY_POINTS_CREATED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "entry_points": created,
                    "index_path": str(index_relative),
                },
                actor="orchestrator",
            )

    @staticmethod
    def _mechanical_brief_from_report(report: str) -> str:
        body = report.split("\n\n---\n\n", 1)[0]
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        selected = lines[:6]
        brief = "\n".join(selected)
        if len(brief) > 1200:
            brief = brief[:1197].rstrip() + "..."
        return brief + "\n"

    def _freeze_performance_evidence(
        self,
        *,
        petition_bundle: dict,
        publication_manifest_path: Path,
    ) -> Path:
        relative = Path("human_private/final/representative_performance_evidence.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return absolute
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        transition = json.loads(
            self.repo.docs.read_text("identity_private/general_principle/status_transition.json")
        )
        statuses = transition["representative_statuses"]
        evidence = {
            record["representative_id"]: {
                "representative_id": record["representative_id"],
                "final_status": statuses[record["representative_id"]],
                "initial_drafter": False,
                "primary_drafter": transition["primary_drafter_id"] == record["representative_id"],
                "general_position_submissions": 0,
                "general_amendments_proposed": 0,
                "detailed_clause_proposals": 0,
                "detailed_split_motions": 0,
                "detailed_suspension_motions": 0,
                "model_call_count": 0,
                "retry_count": 0,
                "recorded_failure_count": 0,
                "telemetry_record_count": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "cache_miss_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "aggregate_cache_hit_rate": None,
                "authorized_context_file_read_count": 0,
                "denied_context_file_access_count": 0,
                "distinct_authorized_context_file_count": 0,
                "drafting_alignment_trial": None,
                "audit_petition_count": 0,
                "contribution_excerpts": [],
            }
            for record in registry
        }
        initial_path = self.repo.root / "governance_private/general_principle/initial_drafter_selection.json"
        if initial_path.exists():
            initial = json.loads(initial_path.read_text(encoding="utf-8"))["representative_id"]
            evidence[initial]["initial_drafter"] = True
        for path in sorted((self.repo.root / "public/general_principle").glob("general_positions*.json")):
            for item in json.loads(path.read_text(encoding="utf-8")):
                rid = item.get("representative_id")
                if rid in evidence:
                    evidence[rid]["general_position_submissions"] += 1
        for path in sorted((self.repo.root / "public/general_principle").glob("amendment_docket_window_*.json")):
            for item in json.loads(path.read_text(encoding="utf-8")).get("amendments", []):
                rid = item.get("proposer_id")
                if rid in evidence:
                    evidence[rid]["general_amendments_proposed"] += 1
                    self._append_excerpt(evidence[rid], "general_amendment", item.get("text", ""))
        detailed_path = self.repo.root / "public/detailed_clauses/active_reviews.json"
        if detailed_path.exists():
            detailed = json.loads(detailed_path.read_text(encoding="utf-8"))
            for field, count_key in (
                ("proposals", "detailed_clause_proposals"),
                ("split_motions", "detailed_split_motions"),
                ("suspension_motions", "detailed_suspension_motions"),
            ):
                for item in detailed.get(field, []):
                    rid = item.get("proposer_id")
                    if rid in evidence:
                        evidence[rid][count_key] += 1
                        self._append_excerpt(
                            evidence[rid], field.rstrip("s"), item.get("text") or item.get("reason", "")
                        )
        scores_path = self.repo.root / "governance_private/drafting_alignment/scores.json"
        if scores_path.exists():
            result = json.loads(scores_path.read_text(encoding="utf-8"))["result"]
            for rid in evidence:
                evidence[rid]["drafting_alignment_trial"] = {
                    "score": result["scores"].get(rid),
                    "authored_adopted_items": result["authored_adopted"].get(rid),
                    "cosponsored_adopted_items": result["cosponsored_adopted"].get(rid),
                    "interpretation_limit": "TRIAL_MECHANICAL_PROVENANCE_MEASURE_NOT_GENERAL_ABILITY",
                }
        for petition in petition_bundle["petitions"]:
            rid = petition["petitioner_rep_id"]
            evidence[rid]["audit_petition_count"] += 1

        # Provider exchanges and normalized telemetry are durable source records.  Reading
        # them directly also covers legacy exchanges whose telemetry was backfilled without
        # emitting one event per historical record.
        exchange_root = self.repo.root / "governance_private/provider_exchanges"
        if exchange_root.exists():
            for path in exchange_root.glob("X-*.json"):
                exchange = json.loads(path.read_text(encoding="utf-8"))
                rid = exchange.get("participant_id")
                if rid in evidence:
                    evidence[rid]["model_call_count"] += 1
        telemetry_root = self.repo.root / "governance_private/telemetry"
        if telemetry_root.exists():
            for path in telemetry_root.glob("X-*.json"):
                telemetry = json.loads(path.read_text(encoding="utf-8"))
                rid = telemetry.get("participant_id")
                if rid not in evidence:
                    continue
                evidence[rid]["telemetry_record_count"] += 1
                for field in (
                    "prompt_tokens",
                    "cached_tokens",
                    "cache_miss_tokens",
                    "completion_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                ):
                    value = telemetry.get(field)
                    if isinstance(value, int) and not isinstance(value, bool):
                        evidence[rid][field] += value
            for record in evidence.values():
                cache_denominator = record["cached_tokens"] + record["cache_miss_tokens"]
                if cache_denominator:
                    record["aggregate_cache_hit_rate"] = (
                        record["cached_tokens"] / cache_denominator
                    )

        accessed_paths = {representative_id: set() for representative_id in evidence}
        access_event_counts: Counter = Counter()
        access_ledger = self.repo.root / "governance_private/representative_file_access.jsonl"
        if access_ledger.exists():
            for line in access_ledger.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                access = json.loads(line)
                access_event_counts[access["event_type"]] += 1
                payload = access.get("payload", {})
                rid = payload.get("representative_id")
                if rid not in evidence:
                    continue
                if access["event_type"] == "REPRESENTATIVE_FILE_READ":
                    evidence[rid]["authorized_context_file_read_count"] += 1
                    accessed_paths[rid].add(
                        (payload.get("compartment"), payload.get("path"))
                    )
                elif access["event_type"] == "REPRESENTATIVE_FILE_ACCESS_DENIED":
                    evidence[rid]["denied_context_file_access_count"] += 1
        for rid, paths in accessed_paths.items():
            evidence[rid]["distinct_authorized_context_file_count"] = len(paths)

        for event in self._events():
            event_type = event["event_type"]
            payload = event.get("payload", {})
            rid = payload.get("participant_id") or payload.get("representative_id")
            if rid not in evidence:
                continue
            if event_type == "PROVIDER_RETRY_SCHEDULED":
                evidence[rid]["retry_count"] += 1
            elif event_type in {"MEETING_FAILED", "REPRESENTATIVE_UNAVAILABLE"}:
                evidence[rid]["recorded_failure_count"] += 1
        event_counts = Counter(event["event_type"] for event in self._events())
        meeting_record = {
            "meeting_id": self.repo.meeting_id,
            "publication_manifest_path": str(publication_manifest_path.relative_to(self.repo.root)),
            "representative_count": len(evidence),
            "human_consultation_count": event_counts["HUMAN_CONSULTATION_OPENED"],
            "meeting_pause_count": event_counts["MEETING_PAUSED"],
            "provider_retry_count": event_counts["PROVIDER_RETRY_SCHEDULED"],
            "schema_repair_request_count": event_counts["MODEL_OUTPUT_SCHEMA_REPAIR_REQUESTED"],
            "meeting_failure_event_count": event_counts["MEETING_FAILED"],
            "audit_requester_count": petition_bundle["audit_requester_count"],
            "audit_petition_count": petition_bundle["audit_petition_count"],
            "authorized_context_file_read_count": access_event_counts[
                "REPRESENTATIVE_FILE_READ"
            ],
            "denied_context_file_access_count": access_event_counts[
                "REPRESENTATIVE_FILE_ACCESS_DENIED"
            ],
            "representative_file_access_ledger_path": (
                "governance_private/representative_file_access.jsonl"
                if access_ledger.exists()
                else None
            ),
            "representatives": list(evidence.values()),
            "evaluation_limits": [
                "Only frozen submissions, provenance metrics, telemetry, and event records are used.",
                "No hidden reasoning is available or inferred.",
                "Consultative conversion and trial Drafting Alignment are not general ability ratings.",
                "Provider retries and failures may reflect transport availability rather than reasoning quality.",
                "Token totals are measured observables summed from normalized per-exchange telemetry; zero may mean either zero use or an unreported provider field.",
                "Aggregate cache hit rate is cached input tokens divided by cached plus cache-miss input tokens across records that reported those fields.",
                "Authorized file-read counts measure orchestrator context assembly (what a Representative was shown), not autonomous browsing or intent.",
                "A denied access records an attempted context inclusion blocked before content release; with current provider APIs it normally indicates an orchestration path error, not a model-originated file request.",
            ],
        }
        self.repo.docs.write_once(
            relative,
            json.dumps(meeting_record, indent=2, ensure_ascii=False),
        )
        self.repo.events.append(
            "REPRESENTATIVE_PERFORMANCE_EVIDENCE_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "representative_count": len(evidence),
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        return absolute

    def _own_history_files(self, representative_id: str) -> tuple[Path, ...]:
        candidates = (
            self.repo.root / "representatives" / representative_id / "status_transition_001.json",
            self.repo.root / "governance_private/general_principle/positions" / f"{representative_id}.json",
            self.repo.root / "governance_private/detailed_clauses/reviews" / f"{representative_id}.json",
        )
        return tuple(path for path in candidates if path.exists())

    def _human_only_roster(self) -> list[dict]:
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        transition = json.loads(
            self.repo.docs.read_text("identity_private/general_principle/status_transition.json")
        )
        statuses = transition["representative_statuses"]
        return [
            {
                "representative_id": item["representative_id"],
                "provider_id": item["runtime"]["provider_id"],
                "model_id": item["runtime"]["model_id"],
                "persona": item["runtime"]["persona"],
                "status": statuses[item["representative_id"]],
            }
            for item in registry
        ]

    def _chair_system_text(self) -> str:
        paths = (
            self.governance_docs / "03_roles/chair/chair_role.md",
            self.governance_docs / "07_runtime_memory/other_participants/deliberation_chair.md",
            self.governance_docs / "02_deliberation/post_meeting_petition.md",
            self.governance_docs / "02_deliberation/minority_and_advisory.md",
        )
        return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)

    def _events(self) -> list[dict]:
        path = self.repo.root / "governance_private/events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    @staticmethod
    def _append_excerpt(record: dict, kind: str, text: str) -> None:
        if text and len(record["contribution_excerpts"]) < 4:
            compact = " ".join(text.split())
            record["contribution_excerpts"].append(
                {"kind": kind, "text": compact[:600]}
            )

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
