import hashlib
import json
import re

from project_ensemble.domain import (
    DeliverableType,
    GenerationResponse,
    InheritanceMode,
    MeetingPhase,
    ReasoningEffort,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.literature_report import (
    ClusteredResearchOutline,
    LiteratureReportPlanningRunner,
    ResearchDecompositionSubmission,
    ResearchOutlineReview,
)
from project_ensemble.orchestration.consultations import HumanConsultationService
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


class NoopNotifier:
    def send_escalation(self, **kwargs):
        return True


def test_decomposition_rationale_has_no_hard_character_limit():
    submission = ResearchDecompositionSubmission.model_validate({
        "framing_rationale": "边界理由。" * 300,
        "modules": [{
            "local_id": "M1",
            "title": "Research direction",
            "research_questions": ["What is known?"],
            "required_evidence": ["review literature"],
        }],
    })
    assert len(submission.framing_rationale) > 1000


def test_outline_structural_proposal_can_include_supporting_issues():
    review = ResearchOutlineReview.model_validate(
        {
            "action": "STRUCTURAL_PROPOSAL",
            "issues": [
                {
                    "affected_module_ids": ["RM-01"],
                    "issue": "The module boundary is too broad.",
                }
            ],
            "proposal": {
                "operation": "SPLIT",
                "target_module_ids": ["RM-01"],
                "proposed_change": "Split the module into two bounded questions.",
                "rationale": "Each half requires a different evidence base.",
            },
        }
    )

    assert review.proposal is not None
    assert len(review.issues) == 1


def test_outline_accepts_descriptive_cross_module_links():
    outline = ClusteredResearchOutline.model_validate({
        "report_title": "Evidence review",
        "scope_note": "Evidence-neutral review.",
        "modules": [{
            "module_id": "RM-01",
            "title": "Applications",
            "research_questions": ["What has direct evidence?"],
            "included_scope": [],
            "excluded_scope": [],
            "required_evidence": ["primary studies"],
            "cross_module_links": ["RM-02/RM-03（parallel technical scope）"],
            "source_submission_refs": ["R-TEST:M1"],
        }, {
            "module_id": "RM-02",
            "title": "Limitations",
            "research_questions": ["What are the limits?"],
            "included_scope": [],
            "excluded_scope": [],
            "required_evidence": ["reviews"],
            "cross_module_links": [],
            "source_submission_refs": ["R-TEST:M1"],
        }, {
            "module_id": "RM-03",
            "title": "Alternatives",
            "research_questions": ["What alternatives exist?"],
            "included_scope": [],
            "excluded_scope": [],
            "required_evidence": ["reviews"],
            "cross_module_links": [],
            "source_submission_refs": ["R-TEST:M1"],
        }],
        "clustering_notes": [],
    })
    assert outline.modules[0].cross_module_links == ["RM-02", "RM-03"]


class PlanningAdapter(ScriptedProviderAdapter):
    def __init__(self):
        super().__init__("fake", ["m"])

    @staticmethod
    def outline():
        return {
            "report_title": "Evidence review",
            "scope_note": "Evidence-neutral review.",
            "modules": [{
                "module_id": "RM-01", "title": "Applications",
                "research_questions": ["What has direct evidence?"],
                "included_scope": ["published applications"],
                "excluded_scope": ["unverified claims"],
                "required_evidence": ["primary studies"],
                "cross_module_links": [],
                "source_submission_refs": ["R-TEST:M1"],
            }],
            "clustering_notes": ["Equivalent modules were merged."],
        }

    def generate(self, request):
        # A rejected older outline can appear earlier in the prompt. Classify
        # the requested output by the final schema, not by reference material.
        target_schema = request.user_text.rsplit("目标结构：", 1)[-1]
        if "review_dispositions" in target_schema:
            ids = sorted(set(re.findall(r'"representative_id"\s*:\s*"(R-[^"]+)"', request.user_text)))
            payload = {**self.outline(), "review_dispositions": [
                {"representative_id": rid, "disposition": "NO_OBJECTION",
                 "rationale": "No structural objection was submitted."}
                for rid in ids
            ]}
        elif "clustering_notes" in request.user_text and "report_title" in request.user_text:
            payload = self.outline()
        elif "STRUCTURAL_PROPOSAL" in request.user_text:
            payload = {"action": "NO_OBJECTION", "issues": [], "proposal": None}
        else:
            payload = {
                "framing_rationale": "Separate applications from limitations.",
                "modules": [{
                    "local_id": "M1", "title": "Applications",
                    "research_questions": ["What has direct evidence?"],
                    "included_scope": ["published applications"],
                    "excluded_scope": ["unverified claims"],
                    "required_evidence": ["primary studies"],
                    "related_local_ids": [],
                }],
            }
        return GenerationResponse(text=json.dumps(payload), provider_id="fake", model_id=request.model_id)


def make_source(tmp_path):
    source = MeetingRepository.create(
        tmp_path / "source", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Produce a source analysis.", research_enabled=True,
        research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
    )
    packet = b'{"packet_id":"RP-INHERITED"}'
    source.docs.write_once("public/research/evidence_packets/RP-INHERITED.json", packet)
    source.docs.write_once("public/research/literature_bundle/documents/source.pdf", b"%PDF-test")
    source.docs.write_once("public/final/procedurally_certified_resolution.md", "# Source")
    return source, packet


def make_derived(tmp_path, source):
    return MeetingRepository.create(
        tmp_path / "derived", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Produce a complete literature review.", research_enabled=True,
        research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        parent_meeting_id=source.meeting_id, parent_meeting_path=source.root,
    )


def test_derived_meeting_copies_research_assets_with_hash_lineage(tmp_path):
    source, packet = make_source(tmp_path)
    derived = make_derived(tmp_path, source)
    assert source.meeting_id.startswith("DL-")
    assert derived.meeting_id.startswith("LR-")
    source_manifest = json.loads(source.docs.read_text("identity_private/meeting_manifest.json"))
    derived_manifest = json.loads(derived.docs.read_text("identity_private/meeting_manifest.json"))
    assert source_manifest["representative_prompt_family"] == "deliberation"
    assert derived_manifest["representative_prompt_family"] == "literature_research"
    copied = derived.root / "public/research/evidence_packets/RP-INHERITED.json"
    assert copied.read_bytes() == packet
    lineage = json.loads((derived.root / "public/continuation/lineage.json").read_text())
    assert lineage["parent_meeting_id"] == source.meeting_id
    assert lineage["inherited_evidence_packet_count"] == 1
    record = next(x for x in lineage["files"] if x["destination_path"].endswith("RP-INHERITED.json"))
    assert record["sha256"] == hashlib.sha256(packet).hexdigest()
    assert lineage["inheritance_mode"] == "both"
    advisory = derived.root / "public/continuation/advisory_context.md"
    assert advisory.is_file()
    assert "建议性前置材料" in advisory.read_text(encoding="utf-8")


def test_continuation_can_inherit_only_evidence_or_only_final_document(tmp_path):
    source, packet = make_source(tmp_path)
    evidence_only = MeetingRepository.create(
        tmp_path / "evidence-only",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Use inherited evidence.",
        parent_meeting_id=source.meeting_id,
        parent_meeting_path=source.root,
        inheritance_mode=InheritanceMode.EVIDENCE,
    )
    assert (
        evidence_only.root / "public/research/evidence_packets/RP-INHERITED.json"
    ).read_bytes() == packet
    assert not (evidence_only.root / "public/continuation/advisory_context.md").exists()

    document_only = MeetingRepository.create(
        tmp_path / "document-only",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Use inherited recommendations.",
        parent_meeting_id=source.meeting_id,
        parent_meeting_path=source.root,
        inheritance_mode=InheritanceMode.FINAL_DOCUMENT,
    )
    assert not (document_only.root / "public/research/evidence_packets").exists()
    advisory_text = (
        document_only.root / "public/continuation/advisory_context.md"
    ).read_text(encoding="utf-8")
    assert "不是 Constitution" in advisory_text
    assert "# Source" in advisory_text


def test_from_scratch_report_starts_with_empty_research_origin(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "scratch", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Review a new topic from scratch.", research_enabled=True,
        research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
    )
    origin = json.loads((repo.root / "public/literature_report/origin.json").read_text())
    assert origin["origin_type"] == "FROM_SCRATCH"
    assert origin["parent_meeting_id"] is None
    assert origin["inherited_evidence_packet_count"] == 0
    assert not (repo.root / "public/continuation/lineage.json").exists()
    result = LiteratureReportPlanningRunner(
        repo=repo,
        engine=MeetingEngine(
            repo=repo, adapters={"fake": PlanningAdapter()}, notifier=NoopNotifier(),
            configured_concurrency_limits={("fake", "m"): 1},
        ),
        governance_docs="docs/governance",
        max_output_tokens=2000,
    ).run()
    assert result.origin_type == "FROM_SCRATCH"
    assert result.parent_meeting_id is None
    assert result.inherited_evidence_packet_count == 0


def test_report_planning_uses_four_personas_and_full_review(tmp_path):
    source, _ = make_source(tmp_path)
    derived = make_derived(tmp_path, source)
    engine = MeetingEngine(
        repo=derived, adapters={"fake": PlanningAdapter()}, notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    runner = LiteratureReportPlanningRunner(
        repo=derived, engine=engine, governance_docs="docs/governance", max_output_tokens=2000,
    )
    result = runner.run()
    assert (result.planning_panel_size, result.decomposition_submission_count) == (4, 4)
    assert result.outline_review_count == 4
    assert result.inherited_evidence_packet_count == 1
    assert result.origin_type == "DERIVED_DELIVERABLE"
    assert result.paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    assert result.next_phase.value == "PAUSED"
    exchanges = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (derived.root / "governance_private/provider_exchanges").glob("X-*.json")
    ]
    decomposition_prompts = [
        item["request"]["system_text"]
        for item in exchanges if item["stage"] == "research_decomposition"
    ]
    assert decomposition_prompts
    assert all("出处尚不确定时标为“待核查”" in text for text in decomposition_prompts)
    chair_clustering_prompts = [
        item["request"]["system_text"]
        for item in exchanges if item["stage"] == "chair_research_outline_clustering"
    ]
    assert chair_clustering_prompts
    assert all("不阻断流程的提醒" in text for text in chair_clustering_prompts)
    derived.docs.write_once(
        "public/literature_report/audience_profile-C001.json",
        json.dumps({
            "meeting_id": derived.meeting_id,
            "outline_issue_id": "HC-LROUTLINE-C001",
            "proficiency_scale": "1_LEAST_FAMILIAR_TO_5_MOST_FAMILIAR",
            "disciplines": {"相关学科": 3},
            "article_skeleton": [],
            "glossary_appendix": False,
        }, ensure_ascii=False),
    )
    HumanConsultationService(derived).resolve(
        issue_id="HC-LROUTLINE-C001",
        decision="APPROVE_OUTLINE",
        rationale="模块边界清楚，可以开始正式检索。",
        scope="LITERATURE_OUTLINE_CYCLE",
    )
    result = runner.run()
    assert result.paused_reason is None
    assert result.next_phase.value == "LITERATURE_MODULE_RESEARCH"
    panel = json.loads((derived.root / "governance_private/literature_report/planning_panel.json").read_text())
    assert {x["persona"] for x in panel["assignments"]} == {
        "systems_integrator", "pragmatic_minimalist", "exploratory_synthesist", "librarian"
    }
    assert (derived.root / result.frozen_outline_path).is_file()
    assert runner.run().frozen_outline_path == result.frozen_outline_path


def test_human_can_revise_only_article_skeleton_without_replanning_modules(tmp_path, monkeypatch):
    source, _ = make_source(tmp_path)
    derived = make_derived(tmp_path, source)
    runner = LiteratureReportPlanningRunner(
        repo=derived,
        engine=MeetingEngine(
            repo=derived, adapters={"fake": PlanningAdapter()}, notifier=NoopNotifier(),
            configured_concurrency_limits={("fake", "m"): 1},
        ),
        governance_docs="docs/governance", max_output_tokens=2000,
    )
    assert runner.run().paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    first_outline = (derived.root / "public/literature_report/frozen_research_outline.json").read_bytes()
    service = HumanConsultationService(derived)
    service.resolve(
        issue_id="HC-LROUTLINE-C001", decision="REVISE_SKELETON_ONLY",
        rationale="请把综述主线放在方法比较之前。", scope="LITERATURE_OUTLINE_CYCLE",
    )
    monkeypatch.setattr(runner, "_revise_article_skeleton", lambda **_: ["研究主线", "方法比较"])
    assert runner.run().paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    assert (derived.root / "public/literature_report/frozen_research_outline.json").read_bytes() == first_outline
    followup = service.open_issues()[0]
    assert followup.issue_id == "HC-LROUTLINE-C001-S001"
    assert followup.context["article_skeleton"] == ["研究主线", "方法比较"]
    derived.docs.write_once(
        "public/literature_report/audience_profile-C001.json",
        json.dumps({
            "outline_issue_id": followup.issue_id, "disciplines": {"相关学科": 3},
            "article_skeleton": followup.context["article_skeleton"],
        }, ensure_ascii=False),
    )
    service.resolve(
        issue_id=followup.issue_id, decision="APPROVE_OUTLINE",
        rationale="骨架已修改。", scope="LITERATURE_OUTLINE_CYCLE",
    )
    assert runner.run().next_phase.value == "LITERATURE_MODULE_RESEARCH"


def test_scope_objections_reach_human_review_without_representative_scope_change(tmp_path):
    class ScopeConcernAdapter(PlanningAdapter):
        def generate(self, request):
            response = super().generate(request)
            payload = json.loads(response.text)
            if "framing_rationale" in payload:
                payload["scope_objection"] = "原始任务范围过宽，建议由 Human 决定是否聚焦。"
            elif "clustering_notes" in payload and "review_dispositions" not in payload:
                payload["scope_concern_notice"] = (
                    "规划代表认为范围过宽；主席请 Human 确认是否保持原范围。"
                )
            return GenerationResponse(
                text=json.dumps(payload, ensure_ascii=False),
                provider_id="fake",
                model_id=request.model_id,
            )

    source, _ = make_source(tmp_path)
    derived = make_derived(tmp_path, source)
    engine = MeetingEngine(
        repo=derived,
        adapters={"fake": ScopeConcernAdapter()},
        notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    result = LiteratureReportPlanningRunner(
        repo=derived, engine=engine, governance_docs="docs/governance", max_output_tokens=2000,
    ).run()
    assert result.paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    issue = HumanConsultationService(derived).open_issues()[0]
    assert "主席转呈的任务范围意见" in issue.question
    assert "主席请 Human 确认" in issue.question
    assert "若你决定改变范围，请选择退回重做" in issue.question

    exchanges = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (derived.root / "governance_private/provider_exchanges").glob("X-*.json")
    ]
    decomposition_requests = [
        item["request"] for item in exchanges if item["stage"] == "research_decomposition"
    ]
    assert decomposition_requests
    assert all("不得自行缩小或排除" in item["user_text"] for item in decomposition_requests)
    related = HumanConsultationService(derived)._related_public_records(issue)
    assert len(related["representative_scope_objections"]) == 4
    assert related["frozen_outline"]["scope_concern_notice"] is not None


def test_legacy_planning_result_infers_origin_without_rewriting(tmp_path):
    source, _ = make_source(tmp_path)
    derived = make_derived(tmp_path, source)
    legacy = {
        "meeting_id": derived.meeting_id,
        "parent_meeting_id": source.meeting_id,
        "planning_panel_size": 4,
        "decomposition_submission_count": 4,
        "outline_review_count": 4,
        "research_module_count": 1,
        "inherited_evidence_packet_count": 1,
        "candidate_outline_path": "public/literature_report/candidate_research_outline.json",
        "frozen_outline_path": "public/literature_report/frozen_research_outline.json",
        "next_phase": "PAUSED",
        "paused_reason": "LITERATURE_MODULE_DRAFTING_POLICY_NOT_CONFIGURED",
    }
    derived.docs.write_once(
        "public/literature_report/planning_result.json", json.dumps(legacy)
    )
    before = (derived.root / "public/literature_report/planning_result.json").read_bytes()
    result = LiteratureReportPlanningRunner(
        repo=derived,
        engine=MeetingEngine(
            repo=derived,
            adapters={"fake": PlanningAdapter()},
            notifier=NoopNotifier(),
        ),
        governance_docs="docs/governance",
    ).run()
    assert result.origin_type == "DERIVED_DELIVERABLE"
    assert (derived.root / "public/literature_report/planning_result.json").read_bytes() == before


def test_rejected_outline_starts_a_fresh_versioned_planning_cycle(tmp_path):
    source, _ = make_source(tmp_path)
    derived = make_derived(tmp_path, source)
    engine = MeetingEngine(
        repo=derived, adapters={"fake": PlanningAdapter()}, notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    runner = LiteratureReportPlanningRunner(
        repo=derived, engine=engine, governance_docs="docs/governance", max_output_tokens=2000,
    )
    first = runner.run()
    assert first.next_phase == MeetingPhase.PAUSED
    HumanConsultationService(derived).resolve(
        issue_id="HC-LROUTLINE-C001",
        decision="REJECT_AND_REPLAN",
        rationale="模块过宽，必须重新按可核验问题拆分。",
        scope="LITERATURE_OUTLINE_CYCLE",
    )
    second = runner.run()
    assert second.next_phase == MeetingPhase.PAUSED
    assert second.paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    assert "-C002" in second.frozen_outline_path
    assert (derived.root / "public/literature_report/frozen_research_outline.json").is_file()
    assert (derived.root / "public/literature_report/frozen_research_outline-C002.json").is_file()
    manifest = json.loads(derived.docs.read_text("public/meeting_manifest.json"))
    assert manifest["planning_replan_reference_enabled"] is True
    exchanges = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (derived.root / "governance_private/provider_exchanges").glob("X-*.json")
    ]
    second_decompositions = [
        item["request"] for item in exchanges
        if item["stage"] == "research_decomposition"
        and "规划轮次：C002" in item["request"]["user_text"]
    ]
    assert len(second_decompositions) == 4
    assert all('"report_title": "Evidence review"' in item["system_text"] for item in second_decompositions)
    assert all('"review_dispositions"' in item["system_text"] for item in second_decompositions)
    assert all("已否决的对照资料" in item["user_text"] for item in second_decompositions)
    assert all("模块过宽" in item["user_text"] for item in second_decompositions)
    second_reviews = [
        item["request"] for item in exchanges
        if item["stage"] == "research_outline_review"
        and "规划轮次：C002" in item["request"]["user_text"]
    ]
    assert len(second_reviews) == 4
    assert all('"report_title": "Evidence review"' in item["system_text"] for item in second_reviews)
    assert all('"review_dispositions"' in item["system_text"] for item in second_reviews)
    chair_prompts = [
        item["request"]["user_text"] for item in exchanges
        if item["stage"] in {
            "chair_research_outline_clustering", "chair_research_outline_finalization"
        } and "已否决的上一轮冻结总纲" in item["request"]["user_text"]
    ]
    assert len(chair_prompts) == 2


def test_existing_report_without_replan_switch_keeps_legacy_context(tmp_path):
    source, _ = make_source(tmp_path)
    derived = make_derived(tmp_path, source)
    manifest_path = derived.root / "public/meeting_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("planning_replan_reference_enabled")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runner = LiteratureReportPlanningRunner(
        repo=derived,
        engine=MeetingEngine(
            repo=derived, adapters={"fake": PlanningAdapter()}, notifier=NoopNotifier(),
        ),
        governance_docs="docs/governance",
    )
    assert runner._rejected_outline_reference(2) is None
