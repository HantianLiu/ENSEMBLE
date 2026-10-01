import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from project_ensemble.errors import RepresentativeUnavailableError, ResearchRequestRejectedError
from project_ensemble.orchestration.scholarly_rendering import (
    ChairCitationApplication,
    ChairScienceIssueAdvice,
    ChairScienceRevision,
    FinalScienceBallot,
    FinalScienceResearchDecision,
    DisqualifiedScienceBallot,
    ScienceReview,
    CitationReview,
    CitationAnchorRepair,
    CitationAmendment,
    CitationDisposition,
    CitationDocket,
    CitationIssue,
    CitationVoteItem,
    CitationVoteSet,
    RenderedSection,
    RenderingAssignment,
    RenderingPlan,
    RenderingScopeProposal,
    RenderingLengthPriorities,
    RenderingLengthPriority,
    SourceBlock,
    ScholarlyRenderingPaused,
    ScholarlyRenderingRunner,
    reissue_structured_scholarly_publication,
)
from project_ensemble.orchestration.consultations import HumanConsultationIssue, HumanConsultationService
from project_ensemble.runtime.structured_output import parse_json_object
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.domain import MeetingPhase


def test_new_rendering_length_budget_is_frozen_and_excludes_references(tmp_path):
    repo = MeetingRepository(tmp_path)
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.manifest = {"rendering_target_body_characters": 1000}
    blocks = [
        SourceBlock(block_id="SB-001", heading="A", body_markdown="甲" * 100),
        SourceBlock(block_id="SB-002", heading="B", body_markdown="乙" * 200),
        SourceBlock(block_id="SB-003", heading="References", body_markdown="丙" * 400),
    ]
    plan = RenderingPlan(assignments=[
        RenderingAssignment(section_id="SR-001", title="方法", source_block_ids=["SB-001"]),
        RenderingAssignment(section_id="SR-002", title="结果", source_block_ids=["SB-002"]),
        RenderingAssignment(section_id="SR-003", title="参考文献", source_block_ids=["SB-003"]),
    ], rationale="Ordered plan.")
    runner._invoke = lambda *_args, **_kwargs: RenderingLengthPriorities(
        priorities=[
            RenderingLengthPriority(section_id="SR-001", priority=2),
            RenderingLengthPriority(section_id="SR-002", priority=5),
            RenderingLengthPriority(section_id="SR-003", priority=1),
        ], rationale="Results need more space."
    )
    budget = runner._ensure_length_budget(plan, blocks)
    assert budget["policy"] == "ADVISORY_ONLY; NO_HARD_LIMIT; FINAL_LENGTH_NOT_GUARANTEED"
    assert "lower_bound_characters" not in budget
    assert "upper_bound_characters" not in budget
    assert sum(item["target_characters"] for item in budget["sections"]) == 1000
    assert budget["sections"][1]["target_characters"] > budget["sections"][0]["target_characters"]
    assert budget["sections"][2]["target_characters"] == 0
    assert runner._ensure_length_budget(plan, blocks) == budget
    runner._check_final_length_budget([
        (plan.assignments[0], "甲" * 400),
        (plan.assignments[1], "乙" * 600),
        (plan.assignments[2], "丙" * 5000),
    ])
    outcome = json.loads((tmp_path / "public/scholarly_rendering/body_length_outcome.json").read_text())
    assert outcome["actual_body_characters"] == 1000
    assert outcome["policy"] == "ADVISORY_ONLY; NO_HARD_LIMIT; FINAL_LENGTH_NOT_GUARANTEED"


def test_empty_science_panel_requires_human_source_fidelity_ruling(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.science = [{"representative_id": "R-A"}, {"representative_id": "R-B"}]
    assignment = RenderingAssignment(section_id="SR-026", title="Section", source_block_ids=["SB-001"])
    with pytest.raises(ScholarlyRenderingPaused, match="SCHOLARLY_SCIENCE_BALLOT_NO_ELIGIBLE_VOTERS"):
        runner._recover_empty_science_panel(assignment, cycle=1)
    service = HumanConsultationService(repo)
    issue = service.open_issues()[0]
    assert issue.options == ["RESTORE_SOURCE_FIDELITY_BALLOT", "KEEP_PAUSED"]
    service.resolve(
        issue_id=issue.issue_id, decision="RESTORE_SOURCE_FIDELITY_BALLOT",
        rationale="Only source fidelity is at issue; unsupported external additions remain forbidden.",
        scope="THIS_CONSULTATION_ONLY",
    )
    assert runner._recover_empty_science_panel(assignment, cycle=1) == {"R-A", "R-B"}


def test_rendering_length_is_advisory_and_never_blocks_publication(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    repo.docs.write_once("public/scholarly_rendering/body_length_budget.json", json.dumps({
        "target_characters": 1000,
        "lower_bound_characters": 800,
        "upper_bound_characters": 1200,
        "measurement": "non-whitespace Unicode characters",
        "sections": [{"section_id": "SR-001", "counts_toward_body_target": True}],
    }))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    assignment = RenderingAssignment(section_id="SR-001", title="Body", source_block_ids=["SB-001"])
    completed = [(assignment, "甲" * 1300)]
    runner._check_final_length_budget(completed)
    outcome = json.loads((tmp_path / "public/scholarly_rendering/body_length_outcome.json").read_text())
    assert outcome["actual_body_characters"] == 1300
    assert outcome["difference_from_target_characters"] == 300
    assert outcome["policy"] == "ADVISORY_ONLY; NO_HARD_LIMIT; FINAL_LENGTH_NOT_GUARANTEED"
    assert not (tmp_path / "human_private/consultations").exists()


def test_intermediate_science_vote_accepts_advisory_style_note_but_final_does_not():
    review = ScienceReview(vote="YES", objections=[], style_note="缩短引言。")
    assert review.style_note == "缩短引言。"
    with pytest.raises(ValidationError):
        FinalScienceBallot(vote="YES", objections=[], style_note="终局不附言。")


def test_long_scholarly_excerpts_remain_valid_archival_content():
    # An earlier meeting was blocked because a 1,619-character source excerpt
    # exceeded an implementation-only 1,600-character field limit. The full
    # provider response is immutable and must validate on resume.
    raw = "```json\n" + json.dumps({
        "source_excerpt": "甲" * 1619,
        "redraw_excerpt": "乙" * 1700,
        "suggestion": "丙" * 1750,
    }, ensure_ascii=False) + "\n```"
    advice = ChairScienceIssueAdvice.model_validate(parse_json_object(raw))
    assert len(advice.source_excerpt) == 1619
    assert len(advice.redraw_excerpt) == 1700
    assert len(advice.suggestion) == 1750
    ballot = FinalScienceBallot.model_validate({
        "vote": "NO", "objections": [{
            "issue": "The current rendering omits a supported limitation.",
            "current_excerpt": "丁" * 1700,
        }],
    })
    assert len(ballot.objections[0].current_excerpt) == 1700
    citation = CitationIssue.model_validate({
        "operation": "INSERT", "target_text": "anchor",
        "citation_text": "戊" * 1700, "rationale": "full bibliographic entry",
    })
    assert len(citation.citation_text or "") == 1700


def test_pre_limit_chair_advice_reuses_frozen_response_after_schema_change(tmp_path):
    repo = MeetingRepository(tmp_path)
    stage = "chair_science_human_advice_SR-059_c001_o004"
    system = "Explain this one objection without changing its scientific meaning."
    user = {"source_markdown": "A frozen source.", "current_redraw": "A redraw."}
    response = {
        "source_excerpt": "甲" * 1619,
        "redraw_excerpt": "A redraw.",
        "suggestion": "Keep the scope of the source.",
    }
    repo.docs.write_once(
        "governance_private/provider_exchanges/X-OLDADVICE.json",
        json.dumps({
            "participant_id": "CHAIR", "stage": stage,
            "request": {
                "system_text": system,
                "user_text": json.dumps(user, indent=2, ensure_ascii=False)
                + "\n\n只返回一个符合以下结构的 JSON 对象：\n"
                + '{"properties":{"source_excerpt":{"maxLength":1600}}}',
            },
            "response": {"text": json.dumps(response, ensure_ascii=False)},
        }, ensure_ascii=False),
    )
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    restored = runner._restore_pre_limit_chair_advice(
        stage=stage, system=system, user=user,
    )
    assert restored is not None
    assert restored.source_excerpt == response["source_excerpt"]
    assert runner._restore_pre_limit_chair_advice(
        stage=stage, system=system, user={**user, "current_redraw": "Changed."},
    ) is None


def test_restored_science_review_keeps_legacy_payload_without_new_null_fields(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    relative = "governance_private/scholarly_rendering/science_reviews/SR-001/round_1/R-A.json"
    repo.docs.write_once(relative, json.dumps({"vote": "YES", "objections": []}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None),
        progress=SimpleNamespace(status=lambda *_args: None),
    )
    runner._ordered_science = lambda: [{"representative_id": "R-A"}]
    runner._invoke = lambda *_args, **_kwargs: pytest.fail("frozen review must not call a model")
    assignment = RenderingAssignment(section_id="SR-001", title="Section", source_block_ids=["SB-001"])
    reviews = runner._science_round(assignment, 1, "Source.", "Current.", tmp_path / "source.md")
    assert reviews[0]["review"] == {"vote": "YES", "objections": []}
    assert "style_note" not in reviews[0]["review"]


def test_source_blocks_are_ordered_and_bounded():
    source = "# Title\n\nOpening.\n\n## A\n\n" + ("alpha " * 40) + "\n\n## B\n\nEnd."
    blocks = ScholarlyRenderingRunner._source_blocks(source, max_chars=80)
    assert [block.block_id for block in blocks] == [
        f"SB-{index:03d}" for index in range(1, len(blocks) + 1)
    ]
    assert "Title" in blocks[0].heading
    assert "End." in blocks[-1].body_markdown


def test_partial_scope_requires_human_approval_and_resumes_after_feedback(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-PARTIAL"}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.manifest = {"rendering_scope_description": "只重绘方法，结论保持原文"}
    blocks = [
        SourceBlock(block_id="SB-001", heading="方法", body_markdown="方法原文"),
        SourceBlock(block_id="SB-002", heading="结论", body_markdown="结论原文"),
    ]
    feedback = []
    def propose(*_args, **kwargs):
        feedback.append(kwargs["user"]["previous_human_feedback"])
        return RenderingScopeProposal(
            selected_block_ids=["SB-001"], rationale="只选择方法。"
        )
    runner._invoke = propose
    service = HumanConsultationService(repo)
    with pytest.raises(ScholarlyRenderingPaused, match="SCOPE_APPROVAL_REQUIRED"):
        runner._approved_scope(blocks)
    issue = service.open_issues()[0]
    assert issue.context["selected_blocks"] == [{"id": "SB-001", "heading": "方法"}]
    service.resolve(issue_id=issue.issue_id, decision="REVISE_RENDERING_SCOPE",
                    rationale="请再次确认结论不会改变", scope="THIS_CONSULTATION_ONLY")
    with pytest.raises(ScholarlyRenderingPaused, match="SCOPE_APPROVAL_REQUIRED"):
        runner._approved_scope(blocks)
    assert feedback == [None, "请再次确认结论不会改变"]
    issue = service.open_issues()[0]
    service.resolve(issue_id=issue.issue_id, decision="APPROVE_RENDERING_SCOPE",
                    rationale="同意仅重绘方法", scope="THIS_CONSULTATION_ONLY")
    assert runner._approved_scope(blocks).selected_block_ids == ["SB-001"]
    assert runner._approved_scope(blocks).selected_block_ids == ["SB-001"]


def test_partial_plan_preserves_unselected_source_without_review(tmp_path, monkeypatch):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-PARTIAL"}))
    source = tmp_path / "public/continuation/source_artifacts/final/scholarly_rendering/scholarly_review.md"
    repo.docs.write_once(source.relative_to(tmp_path), "# 标题\n\n## 方法\n\n方法原文。\n\n## 结论\n\n结论原文。")
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.manifest = {"deliverable_type": "scholarly_rendering", "rendering_scope_description": "只重绘方法"}
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None, paused_reason=None),
        progress=SimpleNamespace(status=lambda *_: None, info=lambda *_: None,
                                 paused=lambda *_: None, set_workload=lambda *_: None),
    )
    blocks = runner._source_blocks(source.read_text(encoding="utf-8"))
    selected = next(block.block_id for block in blocks if block.heading == "方法")
    runner._invoke = lambda *_args, **_kwargs: RenderingScopeProposal(
        selected_block_ids=[selected], rationale="只重绘方法。"
    )
    assert runner.run().paused_reason == "SCHOLARLY_RENDERING_SCOPE_APPROVAL_REQUIRED"
    service = HumanConsultationService(repo)
    issue = service.open_issues()[0]
    service.resolve(issue_id=issue.issue_id, decision="APPROVE_RENDERING_SCOPE",
                    rationale="范围准确", scope="THIS_CONSULTATION_ONLY")
    reviewed = []
    monkeypatch.setattr(runner, "_run_section", lambda item, *_: reviewed.append(item.section_id) or "重绘方法。")
    monkeypatch.setattr(runner, "_publish", lambda _source, _plan, completed: completed)
    completed = runner.run()
    assert len(reviewed) == 1
    assert any("结论原文" in text for item, text in completed if item.section_id not in reviewed)
    assert any((tmp_path / "public/scholarly_rendering/sections" / item.section_id /
                "preserved_source.json").is_file() for item, _ in completed if item.section_id not in reviewed)


def test_rendering_run_reports_source_block_and_section_workload(tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    source.write_text("# A\n\nFirst.\n\n## B\n\nSecond.", encoding="utf-8")
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    reported = []
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.manifest = {"deliverable_type": "scholarly_rendering"}
    runner.engine = SimpleNamespace(progress=SimpleNamespace(set_workload=reported.append))
    monkeypatch.setattr(runner, "_source_path", lambda: source)
    monkeypatch.setattr(
        runner,
        "_plan",
        lambda _blocks: RenderingPlan(
            rationale="Two ordered sections cover the frozen source.",
            assignments=[
                RenderingAssignment(section_id="SR-001", title="A", source_block_ids=["SB-001"]),
                RenderingAssignment(section_id="SR-002", title="B", source_block_ids=["SB-002"]),
            ]
        ),
    )
    monkeypatch.setattr(runner, "_run_section", lambda *_args: "Rendered.")
    monkeypatch.setattr(runner, "_publish", lambda *_args: "published")

    assert runner.run() == "published"
    assert reported == [
        "待重绘原文块共 2 块；正在规划重绘章节",
        "原文块 1–1/2；重绘章节 1/2",
        "已完成原文块 1/2；已完成重绘章节 1/2",
        "原文块 2–2/2；重绘章节 2/2",
        "已完成原文块 2/2；已完成重绘章节 2/2",
    ]


def test_resume_skips_frozen_sections_before_any_model_call(tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    source.write_text("# A\n\nFirst.\n\n## B\n\nSecond.", encoding="utf-8")
    frozen = tmp_path / "public/scholarly_rendering/sections/SR-001/final.md"
    frozen.parent.mkdir(parents=True)
    frozen.write_text("Frozen first section.\n", encoding="utf-8")
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.manifest = {"deliverable_type": "scholarly_rendering"}
    restored = []
    runner.engine = SimpleNamespace(progress=SimpleNamespace(
        set_workload=lambda _message: None,
        info=restored.append,
    ))
    monkeypatch.setattr(runner, "_source_path", lambda: source)
    monkeypatch.setattr(runner, "_plan", lambda _blocks: RenderingPlan(
        rationale="Two sections.", assignments=[
            RenderingAssignment(section_id="SR-001", title="A", source_block_ids=["SB-001"]),
            RenderingAssignment(section_id="SR-002", title="B", source_block_ids=["SB-002"]),
        ],
    ))
    calls = []
    monkeypatch.setattr(runner, "_run_section", lambda assignment, *_args: calls.append(assignment.section_id) or "New second section.\n")
    monkeypatch.setattr(runner, "_publish", lambda _source, _plan, completed: completed)
    completed = runner.run()
    assert calls == ["SR-002"]
    assert completed[0][1] == "Frozen first section.\n"
    assert "已恢复 1/2" in restored[0]


def test_initial_redraw_cannot_declare_a_conclusion_change():
    with pytest.raises(ValidationError):
        RenderedSection(
            title="Section",
            body_markdown="Text",
            justification="Readable prose",
            conclusion_changes=["Changed the conclusion"],
        )


def test_rendering_prompts_do_not_restore_internal_ids_as_reader_content(tmp_path, monkeypatch):
    repo = MeetingRepository(tmp_path)
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.science = [{"representative_id": "R-A"}]
    runner.citations = [{"representative_id": "R-B"}]
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None),
        progress=SimpleNamespace(status=lambda *_args: None),
        model_concurrency_limit=lambda *_args: 1,
    )
    runner._ordered_science = lambda: runner.science
    runner._resolved_runtime_key = lambda *_args: "fake:m"
    runner._record_read = lambda *_args: None
    runner._evidence_index = lambda *_args, **_kwargs: []
    monkeypatch.setattr(
        "project_ensemble.orchestration.scholarly_rendering.run_bounded_representative_lanes",
        lambda records, worker, _limit, *, on_result, **_kwargs: [
            on_result(worker(record)) for record in records
        ],
    )
    systems = {}

    def invoke(_participant, *, stage, schema, system, **_kwargs):
        systems[stage] = system
        if schema is ScienceReview:
            return ScienceReview(vote="YES", objections=[])
        if schema is FinalScienceBallot:
            return FinalScienceBallot(vote="YES", objections=[])
        if schema is ChairScienceRevision:
            return ChairScienceRevision(body_markdown="Natural prose.")
        if schema is CitationReview:
            return CitationReview(issues=[], structure_issues=["### 4.1 背景：手写节号"])
        assert schema is CitationDocket
        return CitationDocket(amendments=[])

    runner._invoke = invoke
    assignment = RenderingAssignment(section_id="SR-001", title="Topic", source_block_ids=["SB-001"])
    assert runner._science_round(
        assignment, 1, "RM-01 source.", "Natural prose.", tmp_path / "source.md"
    )[0]["review"]["vote"] == "YES"
    runner._chair_science_revision(
        assignment, 1, "RM-01 source.", "Natural prose.", [], [],
    )
    runner._invoke_current_science_ballot(
        "R-A", stage="final_science_vote", original="RM-01 source.",
        current="Natural prose.", legacy=False,
    )
    assert runner._citation_round(
        assignment, 1, "RM-01 source.", "Natural prose.", tmp_path / "source.md"
    ) == "Natural prose."
    assert "record_id" in runner._chair_render_system()
    assert "不得为了行文流畅把数学定义改成笼统形容词" in runner._chair_render_system()
    assert "不得以保持完整性为理由要求恢复" in systems["scholarly_science_review_SR-001_r1"]
    assert "使结论无法解释或研究间失去可比性" in systems["scholarly_science_review_SR-001_r1"]
    assert "结构或措辞变化若改变科学含义" in systems["scholarly_science_review_SR-001_r1"]
    assert "不得保留或重新引入" in systems["chair_science_revision_SR-001_r1"]
    assert "默认保留原文的科学结论" in systems["chair_science_revision_SR-001_r1"]
    assert "只有审阅者明确提出、列入 verified_corrections" in systems["chair_science_revision_SR-001_r1"]
    assert "不改变冻结原文的任何结论" not in systems["chair_science_revision_SR-001_r1"]
    assert "不得以保持完整性为理由要求恢复" in systems["final_science_vote"]
    assert "关键物理量的公式" in systems["final_science_vote"]
    assert "若异议是原文信息被遗漏" in systems["final_science_vote"]
    assert "不得把内部模块号" in systems["scholarly_citation_review_SR-001_r1"]
    assert "章节编号纪律" in systems["scholarly_citation_review_SR-001_r1"]
    heading_review = json.loads(
        (tmp_path / "public/scholarly_rendering/sections/SR-001/heading_review_1.json").read_text()
    )
    assert heading_review["findings"] == ["### 4.1 背景：手写节号"]
    assert "不得以完整性为由新增" in systems["chair_citation_docket_SR-001_r1"]


def test_failed_science_vote_opens_all_objections_before_chair_revises(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None, paused_reason=None),
        progress=SimpleNamespace(paused=lambda *_args: None),
    )
    calls = []
    systems = {}

    def invoke(_participant, *, stage, schema, **_kwargs):
        calls.append(stage)
        systems[stage] = _kwargs["system"]
        if schema is ChairScienceIssueAdvice:
            return ChairScienceIssueAdvice(
                source_excerpt="Source wording.",
                redraw_excerpt="Redrawn wording.",
                suggestion="Clarify the scope without changing the conclusion.",
            )
        assert schema is ChairScienceRevision
        return ChairScienceRevision(
            body_markdown="Revised wording.", dispositions=["Followed both Human rulings."]
        )

    runner._invoke = invoke
    assignment = RenderingAssignment(
        section_id="SR-001", title="Section", source_block_ids=["SB-001"]
    )
    reviews = [
        {"reviewer_id": "R-A", "review": {"vote": "NO", "objections": [{"issue": "Scope unclear.", "proposed_wording": None, "verification_claim": None}]}},
        {"reviewer_id": "R-B", "review": {"vote": "NO", "objections": [{"issue": "Uncertainty missing.", "proposed_wording": None, "verification_claim": None}]}},
    ]
    args = (assignment, "Source wording.", "Redrawn wording.", reviews, [
        {"vote": "NO", "objections": [{
            "issue": "Scope unclear.", "current_excerpt": "Redrawn wording.",
            "proposed_wording": None, "verification_claim": None,
        }]},
        {"vote": "NO", "objections": [{
            "issue": "Uncertainty missing.", "current_excerpt": "Redrawn wording.",
            "proposed_wording": None, "verification_claim": None,
        }]},
    ])
    kwargs = {"cycle": 1, "verified_corrections": []}
    service = HumanConsultationService(repo)
    service.open_issue(HumanConsultationIssue(
        issue_id="HC-SR-SR-001", meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW", question="旧版整章选择",
        options=["USE_SOURCE_TEXT", "USE_CURRENT_REDRAW", "USE_HUMAN_WORDING"],
    ))
    assert runner._human_science_resolution(*args, **kwargs) is None
    assert (tmp_path / "human_private/consultations/HC-SR-SR-001.superseded.json").is_file()
    open_issues = service.open_issues()
    assert len(open_issues) == 2
    first, second = open_issues
    assert "Scope unclear" in first.question
    assert "Uncertainty missing" in second.question
    assert "USE_HUMAN_WORDING" not in first.options
    assert not any("chair_science_human_revision" in stage for stage in calls)
    service.resolve(issue_id=first.issue_id, decision="ACCEPT_SCIENCE_OBJECTION", rationale="采纳", scope="THIS_CONSULTATION_ONLY")
    assert runner._human_science_resolution(*args, **kwargs) is None
    assert [issue.issue_id for issue in service.open_issues()] == [second.issue_id]
    assert not any("chair_science_human_revision" in stage for stage in calls)
    service.resolve(issue_id=second.issue_id, decision="DIRECT_CHAIR_SCIENCE_REVISION", rationale="明确不确定性范围", scope="THIS_CONSULTATION_ONLY")
    assert runner._human_science_resolution(*args, **kwargs) == "Revised wording."
    assert runner._human_science_resolution(*args, **kwargs) == "Revised wording."
    assert sum("chair_science_human_revision" in stage for stage in calls) == 1
    assert "默认保持冻结原文的结论" in systems["chair_science_human_revision_SR-001_c001"]
    assert "列入 verified_corrections" in systems["chair_science_human_revision_SR-001_c001"]
    context = json.loads((tmp_path / "public/scholarly_rendering/sections/SR-001/science_human_context_c001_o001.json").read_text())
    assert "reviewer_id" not in json.dumps(context)


def test_human_docket_uses_final_ballot_objections_not_repaired_review_issues(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None, paused_reason=None),
        progress=SimpleNamespace(paused=lambda *_args: None),
    )
    runner._invoke = lambda *_args, **_kwargs: ChairScienceIssueAdvice(
        source_excerpt="Original text.",
        redraw_excerpt="Current text.",
        suggestion="Check the remaining scope issue.",
    )
    assignment = RenderingAssignment(section_id="SR-001", title="Section", source_block_ids=["SB-001"])
    old_reviews = [{"reviewer_id": "R-A", "review": {
        "vote": "NO", "objections": [{
            "issue": "Old claim was too strong.", "proposed_wording": "Current text.",
            "requires_external_verification": False, "verification_claim": None,
        }],
    }}]
    ballots = [{"reviewer_id": "R-A", "vote": "NO", "objections": [{
        "issue": "Current text still lacks a boundary.",
        "current_excerpt": "Current text.",
        "proposed_wording": None,
        "requires_external_verification": False,
        "verification_claim": None,
    }]}]
    assert runner._human_science_resolution(
        assignment, "Original text.", "Current text.", old_reviews, ballots,
        cycle=1, verified_corrections=[],
    ) is None
    issues = HumanConsultationService(repo).open_issues()
    assert len(issues) == 1
    assert "Current text still lacks a boundary" in issues[0].question
    assert "Old claim was too strong" not in issues[0].question


def test_unanswered_old_ballot_cannot_reopen_repaired_review_objection(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    assignment = RenderingAssignment(section_id="SR-017", title="Section", source_block_ids=["SB-001"])
    HumanConsultationService(repo).open_issue(HumanConsultationIssue(
        issue_id="HC-SR-SR-017-C001-O001", meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW", question="Old repaired complaint.",
        options=["ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION"],
    ))
    with pytest.raises(ValueError, match="clarify old-format NO votes"):
        runner._human_science_resolution(
            assignment, "Original.", "Corrected.", [{
                "reviewer_id": "R-A", "review": {"vote": "NO", "objections": [{
                    "issue": "Old repaired complaint.", "proposed_wording": "Corrected.",
                    "verification_claim": None,
                }]},
            }], [{"reviewer_id": "R-A", "vote": "NO"}],
            cycle=1, verified_corrections=[],
        )


def test_resume_reuses_frozen_science_issue_when_only_display_wording_changed(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None, paused_reason=None),
        progress=SimpleNamespace(paused=lambda *_args: None),
    )
    runner._invoke = lambda *_args, **_kwargs: pytest.fail("frozen advice must be reused")
    assignment = RenderingAssignment(section_id="SR-017", title="Section", source_block_ids=["SB-001"])
    objection = {
        "issue": "Old wording was too strong.", "proposed_wording": "Corrected wording.",
        "requires_external_verification": False, "verification_claim": None,
    }
    reviews = [{"reviewer_id": "R-A", "review": {"vote": "NO", "objections": [objection]}}]
    ballots = [{"reviewer_id": "R-A", "vote": "NO", "objections": [{
        **objection, "current_excerpt": "Corrected wording.",
    }]}, {"reviewer_id": "R-B", "vote": "YES"}]
    advice = ChairScienceIssueAdvice(
        source_excerpt="Original wording.", redraw_excerpt="Corrected wording.",
        suggestion="Keep the already corrected sentence.",
    )
    root = "public/scholarly_rendering/sections/SR-017"
    repo.docs.write_once(f"{root}/science_human_advice_c001_o001.json", advice.model_dump_json())
    repo.docs.write_once(f"{root}/science_human_context_c001_o001.json", json.dumps({
        "section_id": "SR-017", "cycle": 1, "objection_number": 1,
        "objection_count": 1, "source_markdown": "Original wording.",
        "current_redraw": "Corrected wording.", "objection": {
            **objection, "current_excerpt": "Corrected wording.",
        },
        "chair_advice": advice.model_dump(mode="json"), "yes_votes": 1,
        "eligible_votes": 2,
    }))
    issue_id = "HC-SR-SR-017-C001-O001"
    service = HumanConsultationService(repo)
    service.open_issue(HumanConsultationIssue(
        issue_id=issue_id, meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW",
        question="第 1 次科学审核未获过半；旧版展示文字。",
        options=[
            "ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION",
            "DIRECT_CHAIR_SCIENCE_REVISION",
        ],
        affected_items=["SR-017"],
    ))
    assert runner._human_science_resolution(
        assignment, "Original wording.", "Corrected wording.", reviews, ballots,
        cycle=1, verified_corrections=[],
    ) is None
    assert service.open_issues()[0].question == "第 1 次科学审核未获过半；旧版展示文字。"
    assert len(service.open_issues()) == 1


def test_resume_refuses_changed_frozen_science_objection(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.engine = SimpleNamespace(status=SimpleNamespace(phase=None), progress=SimpleNamespace())
    runner._invoke = lambda *_args, **_kwargs: ChairScienceIssueAdvice(
        source_excerpt="Original.", redraw_excerpt="Current.", suggestion="Review."
    )
    assignment = RenderingAssignment(section_id="SR-001", title="Section", source_block_ids=["SB-001"])
    objection = {"issue": "A current issue.", "proposed_wording": None,
                 "requires_external_verification": False, "verification_claim": None}
    repo.docs.write_once(
        "public/scholarly_rendering/sections/SR-001/science_human_context_c001_o001.json",
        json.dumps({"objection": {"issue": "A different frozen issue."}}),
    )
    with pytest.raises(ValueError, match="frozen science consultation context conflicts"):
        runner._human_science_resolution(
            assignment, "Original.", "Current.", [],
            [{"vote": "NO", "objections": [objection]}],
            cycle=1, verified_corrections=[],
        )


def test_legacy_no_ballot_is_clarified_without_overwriting_frozen_vote(tmp_path, monkeypatch):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    old_ballot = {"reviewer_id": "R-A", "vote": "NO"}
    original_relative = "governance_private/scholarly_rendering/science_ballots/SR-001/R-A.json"
    repo.docs.write_once(original_relative, json.dumps(old_ballot))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.science = [{"representative_id": "R-A"}]
    runner.engine = SimpleNamespace(
        progress=SimpleNamespace(), model_concurrency_limit=lambda *_args: 1,
    )
    runner._resolved_runtime_key = lambda *_args: "fake:m"
    runner._record_read = lambda *_args: None
    monkeypatch.setattr(
        "project_ensemble.orchestration.scholarly_rendering.run_bounded_representative_lanes",
        lambda *_args, **_kwargs: [],
    )
    calls = []

    def invoke(_rid, *, stage, schema, **_kwargs):
        calls.append(stage)
        assert schema is FinalScienceBallot
        return FinalScienceBallot(vote="YES", objections=[])

    runner._invoke = invoke
    assignment = RenderingAssignment(section_id="SR-001", title="Section", source_block_ids=["SB-001"])
    ballots = runner._science_ballot(
        assignment, "Original text.", "Current text.", {"R-A"}, tmp_path / "source.md",
    )
    assert ballots[0]["vote"] == "YES"
    assert json.loads((tmp_path / original_relative).read_text()) == old_ballot
    assert "clarification" in ballots[0]["legacy_ballot_clarification_path"]
    assert len(calls) == 1
    assert runner._science_ballot(
        assignment, "Original text.", "Current text.", {"R-A"}, tmp_path / "source.md",
    ) == ballots
    assert len(calls) == 1


@pytest.mark.parametrize("clarified_vote", ["YES", "NO"])
def test_pending_stale_science_consultation_is_withdrawn_after_current_ballot_clarification(
    tmp_path, monkeypatch, clarified_vote
):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    original_ballot = {"reviewer_id": "R-A", "vote": "NO"}
    ballot_path = "governance_private/scholarly_rendering/science_ballots/SR-017/R-A.json"
    repo.docs.write_once(ballot_path, json.dumps(original_ballot))
    assignment = RenderingAssignment(section_id="SR-017", title="Section", source_block_ids=["SB-001"])
    service = HumanConsultationService(repo)
    old_issue_id = "HC-SR-SR-017-C001-O001"
    service.open_issue(HumanConsultationIssue(
        issue_id=old_issue_id, meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW", question="First-round issue that was fixed.",
        options=["ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION"],
        affected_items=["SR-017"],
    ))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.science = [{"representative_id": "R-A"}]
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None, paused_reason=None),
        progress=SimpleNamespace(paused=lambda *_args: None),
        model_concurrency_limit=lambda *_args: 1,
    )
    runner._resolved_runtime_key = lambda *_args: "fake:m"
    runner._record_read = lambda *_args: None
    monkeypatch.setattr(
        "project_ensemble.orchestration.scholarly_rendering.run_bounded_representative_lanes",
        lambda *_args, **_kwargs: [],
    )
    calls = []

    def invoke(_participant, *, schema, **_kwargs):
        calls.append(schema)
        if schema is FinalScienceBallot:
            return FinalScienceBallot(
                vote=clarified_vote,
                objections=[] if clarified_vote == "YES" else [{
                    "issue": "A new issue still present in the current text.",
                    "current_excerpt": "Current sentence.",
                }],
            )
        assert schema is ChairScienceIssueAdvice
        return ChairScienceIssueAdvice(
            source_excerpt="Source sentence.", redraw_excerpt="Current sentence.",
            suggestion="Address the new current-text issue.",
        )

    runner._invoke = invoke
    ballots = runner._science_ballot(
        assignment, "Source sentence.", "Current sentence.", {"R-A"}, tmp_path / "source.md"
    )
    assert ballots[0]["vote"] == clarified_vote
    assert json.loads((tmp_path / ballot_path).read_text()) == original_ballot
    assert (tmp_path / f"human_private/consultations/{old_issue_id}.withdrawn.json").is_file()
    assert service.open_issues() == []
    with pytest.raises(ValueError, match="withdrawn"):
        service.resolve(
            issue_id=old_issue_id, decision="REJECT_SCIENCE_OBJECTION",
            rationale="No longer pending.", scope="THIS_CONSULTATION_ONLY",
        )
    assert calls.count(FinalScienceBallot) == 1
    assert runner._science_ballot(
        assignment, "Source sentence.", "Current sentence.", {"R-A"}, tmp_path / "source.md"
    ) == ballots
    assert calls.count(FinalScienceBallot) == 1
    if clarified_vote == "NO":
        assert runner._human_science_resolution(
            assignment, "Source sentence.", "Current sentence.", [{
                "reviewer_id": "R-A", "review": {"vote": "NO", "objections": [{
                    "issue": "First-round issue that was fixed.",
                    "proposed_wording": None, "verification_claim": None,
                }]},
            }], ballots, cycle=1, verified_corrections=[],
        ) is None
        open_issues = service.open_issues()
        assert len(open_issues) == 1
        assert open_issues[0].issue_id == f"{old_issue_id}-CLARIFIED"
        assert "A new issue still present" in open_issues[0].question
        assert "First-round issue" not in open_issues[0].question
    assert repo.events.verify()


def test_resolved_old_format_science_consultation_keeps_human_ruling(tmp_path, monkeypatch):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    assignment = RenderingAssignment(section_id="SR-017", title="Section", source_block_ids=["SB-001"])
    old_ballot = {"reviewer_id": "R-A", "vote": "NO"}
    repo.docs.write_once(
        "governance_private/scholarly_rendering/science_ballots/SR-017/R-A.json",
        json.dumps(old_ballot),
    )
    objection = {
        "issue": "An already repaired first-round complaint.", "proposed_wording": None,
        "requires_external_verification": False, "verification_claim": None,
    }
    advice = ChairScienceIssueAdvice(
        source_excerpt="Original.", redraw_excerpt="Current.", suggestion="Keep the current text.",
    )
    root = "public/scholarly_rendering/sections/SR-017"
    repo.docs.write_once(f"{root}/science_human_advice_c001_o001.json", advice.model_dump_json())
    repo.docs.write_once(f"{root}/science_human_context_c001_o001.json", json.dumps({
        "section_id": "SR-017", "cycle": 1, "objection_number": 1, "objection_count": 1,
        "source_markdown": "Original.", "current_redraw": "Current.",
        "objection": objection, "chair_advice": advice.model_dump(mode="json"),
        "yes_votes": 0, "eligible_votes": 1,
    }))
    repo.docs.write_once(f"{root}/science_human_revision_c001.json", ChairScienceRevision(
        body_markdown="Current.", dispositions=["Human rejected the old complaint."],
    ).model_dump_json())
    service = HumanConsultationService(repo)
    issue_id = "HC-SR-SR-017-C001-O001"
    service.open_issue(HumanConsultationIssue(
        issue_id=issue_id, meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW", question="Frozen historical wording.",
        options=["ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION",
                 "DIRECT_CHAIR_SCIENCE_REVISION"],
        affected_items=["SR-017"],
    ))
    service.resolve(
        issue_id=issue_id, decision="REJECT_SCIENCE_OBJECTION",
        rationale="Already repaired.", scope="THIS_CONSULTATION_ONLY",
    )
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.science = [{"representative_id": "R-A"}]
    runner.engine = SimpleNamespace(
        progress=SimpleNamespace(), model_concurrency_limit=lambda *_args: 1,
    )
    runner._resolved_runtime_key = lambda *_args: "fake:m"
    runner._record_read = lambda *_args: None
    runner._invoke = lambda *_args, **_kwargs: pytest.fail("resolved ruling must be replayed without a model call")
    monkeypatch.setattr(
        "project_ensemble.orchestration.scholarly_rendering.run_bounded_representative_lanes",
        lambda *_args, **_kwargs: [],
    )
    ballots = runner._science_ballot(
        assignment, "Original.", "Current.", {"R-A"}, tmp_path / "source.md",
    )
    assert ballots == [old_ballot]
    assert runner._human_science_resolution(
        assignment, "Original.", "Current.", [{
            "reviewer_id": "R-A", "review": {"vote": "NO", "objections": [objection, {
                "issue": "Another historical issue already repaired before the Human ruling.",
                "proposed_wording": None, "verification_claim": None,
            }]},
        }], ballots, cycle=1, verified_corrections=[],
    ) == "Current."
    assert not (tmp_path / "human_private/consultations/HC-SR-SR-017-C001-O002.issue.json").exists()
    assert not (tmp_path / f"human_private/consultations/{issue_id}.withdrawn.json").exists()
    assert service.resolution(issue_id).decision == "REJECT_SCIENCE_OBJECTION"


def test_partial_legacy_science_docket_replays_all_frozen_objections(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None, paused_reason=None),
        progress=SimpleNamespace(paused=lambda *_args: None),
    )
    runner._invoke = lambda *_args, **_kwargs: ChairScienceIssueAdvice(
        source_excerpt="Source.", redraw_excerpt="Current.", suggestion="Review the issue."
    )
    assignment = RenderingAssignment(section_id="SR-013", title="Section", source_block_ids=["SB-001"])
    first = {"issue": "First issue.", "proposed_wording": None, "verification_claim": None}
    second = {"issue": "Second issue.", "proposed_wording": None, "verification_claim": None}
    advice = ChairScienceIssueAdvice(
        source_excerpt="Source.", redraw_excerpt="Current.", suggestion="Review the issue."
    )
    root = "public/scholarly_rendering/sections/SR-013"
    repo.docs.write_once(f"{root}/science_human_advice_c001_o001.json", advice.model_dump_json())
    repo.docs.write_once(f"{root}/science_human_context_c001_o001.json", json.dumps({
        "section_id": "SR-013", "cycle": 1, "objection_number": 1, "objection_count": 2,
        "source_markdown": "Source.", "current_redraw": "Current.",
        "objection": first, "chair_advice": advice.model_dump(mode="json"),
        "yes_votes": 1, "eligible_votes": 2,
    }))
    service = HumanConsultationService(repo)
    issue_id = "HC-SR-SR-013-C001-O001"
    service.open_issue(HumanConsultationIssue(
        issue_id=issue_id, meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW", question="Frozen first question.",
        options=["ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION", "DIRECT_CHAIR_SCIENCE_REVISION"],
        affected_items=["SR-013"],
    ))
    service.resolve(
        issue_id=issue_id, decision="REJECT_SCIENCE_OBJECTION",
        rationale="Resolved first only.", scope="THIS_CONSULTATION_ONLY",
    )
    reviews = [{"reviewer_id": "R-A", "review": {"vote": "NO", "objections": [first, second]}}]
    ballots = [{"reviewer_id": "R-A", "vote": "NO"}, {"reviewer_id": "R-B", "vote": "YES"}]
    assert runner._human_science_resolution(
        assignment, "Source.", "Current.", reviews, ballots,
        cycle=1, verified_corrections=[],
    ) is None
    assert service.resolution(issue_id).decision == "REJECT_SCIENCE_OBJECTION"
    assert [issue.issue_id for issue in service.open_issues()] == ["HC-SR-SR-013-C001-O002"]
    second_context = json.loads((tmp_path / root / "science_human_context_c001_o002.json").read_text())
    assert second_context["objection"] == second
    assert second_context["objection_count"] == 2
    docket_path = tmp_path / "governance_private/scholarly_rendering/science_human_dockets/SR-013/cycle_001.json"
    docket = json.loads(docket_path.read_text())
    assert docket["objections"] == [first, second]
    # Further resumes use the immutable full docket even if a newer schema no
    # longer reconstructs the legacy review list in exactly the same way.
    assert runner._human_science_resolution(
        assignment, "Source.", "Current.", [], ballots,
        cycle=1, verified_corrections=[],
    ) is None
    assert [issue.issue_id for issue in service.open_issues()] == ["HC-SR-SR-013-C001-O002"]
    with pytest.raises(ScholarlyRenderingPaused, match="SCHOLARLY_RENDERING_CONSULTATION_INPUT_CONFLICT"):
        runner._human_science_resolution(
            assignment, "Source.", "Current.", [], [{"reviewer_id": "R-A", "vote": "YES"}],
            cycle=1, verified_corrections=[],
        )
    assert json.loads(docket_path.read_text()) == docket


def test_final_science_ballot_retries_stale_quote_before_freezing(tmp_path):
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    calls = []

    def invoke(_rid, *, stage, **_kwargs):
        calls.append(stage)
        excerpt = "Repaired old wording." if len(calls) == 1 else "Current text."
        return FinalScienceBallot(vote="NO", objections=[{
            "issue": "A remaining issue.", "current_excerpt": excerpt,
        }])

    runner._invoke = invoke
    ballot = runner._invoke_current_science_ballot(
        "R-A", stage="final_ballot", original="Original text.",
        current="Current text.", legacy=False,
    )
    assert ballot.objections[0].current_excerpt == "Current text."
    assert calls == ["final_ballot", "final_ballot_excerpt_repair_1"]


def test_final_science_research_deduplicates_and_shares_four_query_limit_across_cycles(tmp_path):
    repo = MeetingRepository(tmp_path)
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.manifest = {"rendering_final_science_query_limit": 4}
    assignment = RenderingAssignment(section_id="SR-001", title="Section", source_block_ids=["SB-001"])
    decisions = iter([
        FinalScienceResearchDecision(finished=False, claim="Claim A"),
        FinalScienceResearchDecision(finished=False, claim="  claim   a  "),
        FinalScienceResearchDecision(finished=False, claim="Claim B"),
        FinalScienceResearchDecision(finished=True),
        FinalScienceResearchDecision(finished=False, claim="Claim C"),
        FinalScienceResearchDecision(finished=False, claim="Claim D"),
    ])
    requested = []
    runner._invoke = lambda *_args, **_kwargs: next(decisions)

    def research(*, request_id, requester_id, claim):
        requested.append(claim)
        finding = SimpleNamespace(model_dump=lambda **_kwargs: {"source": "paper"})
        return SimpleNamespace(
            packet_id=f"RP-{len(requested)}", normalized_claim=claim,
            knowledge_status=SimpleNamespace(value="SOURCE_BACKED"),
            supporting_evidence=[finding], contradictory_evidence=[], scope_limitations=[],
        )

    runner._research_or_restore_science_correction = research
    first = runner._final_science_research(assignment, "R-A", "Source.", "Current.", cycle=1)
    assert requested == ["Claim A", "Claim B"]
    assert len(first) == 3  # duplicate reuses the first packet, without a query
    assert runner._final_science_research(assignment, "R-A", "Source.", "Current.", cycle=1) == first
    second = runner._final_science_research(assignment, "R-A", "Source.", "Current.", cycle=2)
    assert requested == ["Claim A", "Claim B", "Claim C", "Claim D"]
    assert len(second) == 5
    assert runner._final_science_research(assignment, "R-A", "Source.", "Current.", cycle=3) == second


def test_final_science_correction_requires_partial_support_before_vote():
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.manifest = {"rendering_final_science_query_limit": 4}
    objection = {
        "issue": "The redraw changes a conclusion.", "current_excerpt": "Current text.",
        "requires_external_verification": True, "verification_claim": "Claim A",
    }
    runner._invoke = lambda *_args, **_kwargs: FinalScienceBallot(
        vote="NO", objections=[objection]
    )
    with pytest.raises(DisqualifiedScienceBallot):
        runner._invoke_current_science_ballot(
            "R-A", stage="final_vote", original="Source text.",
            current="Current text.", legacy=False, verified_corrections=[],
        )
    accepted = runner._invoke_current_science_ballot(
        "R-A", stage="final_vote", original="Source text.",
        current="Current text.", legacy=False,
        verified_corrections=[{"claim": "Claim A", "packet_id": "RP-1"}],
    )
    assert accepted.vote == "NO"


def test_existing_rendering_meeting_keeps_final_science_query_window_disabled():
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.manifest = {}  # Existing meetings predate the frozen query-limit field.
    runner._invoke = lambda *_args, **_kwargs: FinalScienceBallot(
        vote="NO", objections=[{
            "issue": "An external scientific correction.",
            "current_excerpt": "Current text.",
            "requires_external_verification": True,
            "verification_claim": "Claim A",
        }]
    )
    with pytest.raises(ValueError, match="unverified external correction"):
        runner._invoke_current_science_ballot(
            "R-A", stage="final_vote", original="Source text.",
            current="Current text.", legacy=False,
        )


def test_human_science_revision_is_followed_by_new_science_review_and_vote(tmp_path):
    repo = MeetingRepository(tmp_path)
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.science = []
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None),
        progress=SimpleNamespace(status=lambda *_args: None),
    )
    draft = RenderedSection(title="Section", body_markdown="First redraw.", justification="Readability.")
    runner._invoke = lambda *_args, **_kwargs: draft
    runner._public_config = lambda: {}
    cycles = []
    runner._science_round = lambda *_args, cycle, **_kwargs: cycles.append(cycle) or []
    runner._verify_science_corrections = lambda *_args, **_kwargs: ([], set())
    runner._science_ballot = lambda *_args, cycle, **_kwargs: [{"vote": "NO" if cycle == 1 else "YES"}]
    runner._human_science_resolution = lambda *_args, **_kwargs: "Human-directed revision."
    runner._citation_round = lambda _assignment, _round, _original, current, _path: current
    assignment = RenderingAssignment(section_id="SR-001", title="Section", source_block_ids=["SB-001"])
    assert runner._run_section(assignment, "Original.", tmp_path / "source.md") == "Human-directed revision.\n"
    assert cycles == [1, 1, 2, 2]


def test_resolved_legacy_science_choice_does_not_start_second_review_cycle(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    service = HumanConsultationService(repo)
    service.open_issue(HumanConsultationIssue(
        issue_id="HC-SR-SR-001", meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW", question="旧版整章选择",
        options=["USE_SOURCE_TEXT", "USE_CURRENT_REDRAW", "USE_HUMAN_WORDING"],
    ))
    service.resolve(
        issue_id="HC-SR-SR-001", decision="USE_CURRENT_REDRAW",
        rationale="采用现有重绘稿", scope="THIS_CONSULTATION_ONLY",
    )
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.science = []
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None),
        progress=SimpleNamespace(status=lambda *_args: None),
    )
    runner._invoke = lambda *_args, **_kwargs: RenderedSection(
        title="Section", body_markdown="First redraw.", justification="Readability."
    )
    runner._public_config = lambda: {}
    cycles = []
    runner._science_round = lambda *_args, cycle, **_kwargs: cycles.append(cycle) or []
    runner._verify_science_corrections = lambda *_args, **_kwargs: ([], set())
    runner._science_ballot = lambda *_args, **_kwargs: [{"vote": "NO"}]
    runner._citation_round = lambda _assignment, _round, _original, current, _path: current
    assignment = RenderingAssignment(section_id="SR-001", title="Section", source_block_ids=["SB-001"])
    assert runner._run_section(assignment, "Original.", tmp_path / "source.md") == "First redraw.\n"
    assert cycles == [1, 1]


def test_citation_move_is_not_an_available_operation():
    with pytest.raises(ValidationError):
        CitationIssue(
            operation="MOVE",
            target_text="[1]",
            citation_text="[2]",
            rationale="Relocate citation",
        )


def test_citation_application_is_mechanical_and_complete():
    docket = CitationDocket(
        amendments=[
            CitationAmendment(
                amendment_id="CA-001",
                operation="INSERT",
                target_text="supported claim",
                citation_text=" [2]",
                rationale="Add its source.",
            ),
            CitationAmendment(
                amendment_id="CA-002",
                operation="REMOVE",
                target_text=" [9]",
                rationale="The source does not support this location.",
            ),
        ]
    )
    application = ChairCitationApplication(
        decisions=[
            CitationDisposition(amendment_id="CA-001", apply=True, rationale="Supported."),
            CitationDisposition(amendment_id="CA-002", apply=True, rationale="Misplaced."),
        ]
    )
    assert ScholarlyRenderingRunner._apply_citation_decisions(
        "A supported claim [9].", docket, application
    ) == "A supported claim [2]."

    incomplete = ChairCitationApplication(
        decisions=[
            CitationDisposition(amendment_id="CA-001", apply=True, rationale="Supported.")
        ]
    )
    with pytest.raises(ValueError, match="every amendment"):
        ScholarlyRenderingRunner._apply_citation_decisions(
            "A supported claim [9].", docket, incomplete
        )


def test_latex_parses_structure_before_escaping_text():
    latex = ScholarlyRenderingRunner._latex(
        "# 标题\n\n## Method & scope\n\n- x_1\n- 50%\n\n```text\n# literal\n```\n"
    )
    assert r"\documentclass[11pt,a4paper]{ctexart}" in latex
    assert r"\title{标题}" in latex
    assert r"\tableofcontents" in latex
    assert r"\section{Method \& scope}" in latex
    assert r"\item x\_1" in latex
    assert r"\item 50\%" in latex
    assert "# literal" in latex


def test_scholarly_publication_assembles_title_contents_and_chapter_hierarchy(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("# 原始综述总标题\n\n## 原始章节\n\n冻结原文。\n", encoding="utf-8")
    draft_path = tmp_path / "public/scholarly_rendering/sections/SR-001/chair_redraw.json"
    draft_path.parent.mkdir(parents=True)
    draft_path.write_text(RenderedSection(
        title="RM-01 第一章：证据基础",
        body_markdown="### 小节\n\n正文。",
        justification="调整表达。",
    ).model_dump_json(), encoding="utf-8")
    assembled = ScholarlyRenderingRunner._assemble_publication_body(
        SimpleNamespace(root=tmp_path), {"rendering_language": "zh"}, source, [
        (RenderingAssignment(
            section_id="SR-001", title="规划标题", source_block_ids=["SB-001"]
        ), "### 本节摘要\n\n正文。\n\n## 结论\n\n结论文字。\n\n```\n# 示例，不是标题\n```"),
        (RenderingAssignment(
            section_id="SR-002", title="第二章：后续研究", source_block_ids=["SB-002"]
        ), "开篇。\n\n# 方法\n\n方法文字。"),
    ])

    assert assembled.startswith("# 原始综述总标题\n\n## 目录\n\n")
    assert "1. 第一章：证据基础\n2. 第二章：后续研究" in assembled
    assert "## 1. 第一章：证据基础\n\n#### 本节摘要" in assembled
    assert "### 结论\n\n结论文字。" in assembled
    assert "## 2. 第二章：后续研究\n\n开篇。\n\n### 方法" in assembled
    assert "```\n# 示例，不是标题\n```" in assembled
    assert "RM-01" not in assembled


def test_scholarly_contents_group_adjacent_rendering_sections_by_source_module(tmp_path):
    source = tmp_path / "source.md"
    source.write_text(
        "# 总标题\n\n### 1. RM-01 原报告模块标题\n\n正文。\n",
        encoding="utf-8",
    )
    completed = [
        (RenderingAssignment(
            section_id="SR-001", title="RM-01 范围", source_block_ids=["SB-001"]
        ), "### 范围定义\n\n甲。"),
        (RenderingAssignment(
            section_id="SR-002", title="RM-01 证据", source_block_ids=["SB-002"]
        ), "### 证据分级\n\n乙；详见 RM-01。"),
    ]
    assembled = ScholarlyRenderingRunner._assemble_publication_body(
        SimpleNamespace(root=tmp_path), {"rendering_language": "zh"}, source, completed
    )
    contents = assembled.split("## 目录\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert contents == "1. 原报告模块标题"
    assert "## 1. 原报告模块标题" in assembled
    assert "### 1.1. 范围" in assembled
    assert "### 1.2. 证据" in assembled
    assert "#### 范围定义" in assembled
    assert "乙；详见 第1章。" in assembled
    assert "RM-01" not in assembled


def test_completed_rendering_can_add_structured_edition_without_overwriting_original(tmp_path):
    repo = MeetingRepository(tmp_path / "M-TEST")
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    repo.docs.write_once("identity_private/meeting_manifest.json", json.dumps({
        "rendering_language": "zh", "rendering_output_formats": ["md"],
    }))
    repo.docs.write_once(
        "public/continuation/source_artifacts/final/literature_review_report.md",
        "# 综述总标题\n\n冻结原文。\n",
    )
    assignment = RenderingAssignment(
        section_id="SR-001", title="RM-01 证据基础", source_block_ids=["SB-001"]
    )
    repo.docs.write_once("public/scholarly_rendering/rendering_plan.json", RenderingPlan(
        assignments=[assignment], rationale="保留原始顺序。"
    ).model_dump_json())
    repo.docs.write_once("public/scholarly_rendering/sections/SR-001/final.md", "### 研究方法\n\n已审核正文。\n")
    repo.docs.write_once(
        "public/final/scholarly_rendering/scholarly_review.md", "已冻结的旧版排版。\n"
    )
    repo.docs.write_once("public/scholarly_rendering/execution_result.json", json.dumps({
        "meeting_id": repo.meeting_id, "status": "HANDOFF_READY",
        "section_count": 1, "completed_section_count": 1,
        "source_markdown_path": "public/continuation/source_artifacts/final/literature_review_report.md",
        "final_markdown_path": "public/final/scholarly_rendering/scholarly_review.md",
        "next_phase": MeetingPhase.HANDOFF_READY.value,
    }))

    edition = reissue_structured_scholarly_publication(repo)
    structured = (repo.root / edition["markdown_path"]).read_text(encoding="utf-8")
    assert structured.startswith("# 综述总标题\n\n## 目录\n\n1. 证据基础")
    assert "## 1. 证据基础\n\n### 1.1 研究方法" in structured
    assert (repo.root / "public/final/scholarly_rendering/scholarly_review.md").read_text() == "已冻结的旧版排版。\n"
    assert (repo.root / "SCHOLARLY_REVIEW_STRUCTURED.md").resolve() == repo.root / edition["markdown_path"]
    assert (repo.root / "FINAL_REPORT.md").resolve() == repo.root / edition["markdown_path"]
    assert (repo.root / "FINAL_REPORT.tex").resolve() == repo.root / edition["latex_path"]
    assert reissue_structured_scholarly_publication(repo) == edition


def test_numeric_citations_must_resolve_to_reference_entries():
    ScholarlyRenderingRunner._validate_numeric_citation_mapping(
        "# Review\n\nClaim [1, 2].\n\n## References\n\n[1] A.\n[2] B.\n"
    )
    with pytest.raises(ValueError, match="no reference entry"):
        ScholarlyRenderingRunner._validate_numeric_citation_mapping(
            "# Review\n\nClaim [2].\n\n## References\n\n[1] A.\n"
        )
    # Without an explicit reference section this could be a year or a metric,
    # so the deterministic citation parser must not classify it as an error.
    ScholarlyRenderingRunner._validate_numeric_citation_mapping(
        "# Review\n\nThe cohort was assembled in [2023].\n"
    )
    with pytest.raises(ValueError, match="continuous"):
        ScholarlyRenderingRunner._validate_numeric_citation_mapping(
            "# Review\n\nClaim [1].\n\n## References\n\n[1] A.\n[3] C.\n"
        )


def test_source_headings_inside_code_fences_do_not_split_blocks():
    source = "# Title\n\n```md\n## not a heading\n```\n\n### Real section\n\nText."
    blocks = ScholarlyRenderingRunner._source_blocks(source)
    assert len(blocks) == 2
    assert "## not a heading" in blocks[0].body_markdown
    assert blocks[1].heading == "Real section"


def test_science_research_restores_rejected_immutable_request(tmp_path):
    repo = MeetingRepository(tmp_path)
    request_id = "SR-001-R1-R-ABC123-01"
    request_root = tmp_path / "audit_private/research/requests"
    request_root.mkdir(parents=True)
    (request_root / f"{request_id}.json").write_text(
        json.dumps(
            {
                "request_id": request_id,
                "request": {
                    "requester_id": "R-ABC123",
                    "stage": "SCHOLARLY_RENDERING",
                    "claim": "Internal procedure is preferable.",
                    "force_refresh": False,
                },
                "normalized_claim": {"rejection_reason": "not an external claim"},
                "status": "REJECTED_NOT_CLAIM_SCOPED",
            }
        ),
        encoding="utf-8",
    )

    class DeskMustNotRun:
        def research(self, *args, **kwargs):
            raise AssertionError("persisted request must be restored")

    runner = object.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.research_desk = DeskMustNotRun()
    with pytest.raises(ResearchRequestRejectedError, match="not an external claim"):
        runner._research_or_restore_science_correction(
            request_id=request_id,
            requester_id="R-ABC123",
            claim="Internal procedure is preferable.",
        )


def test_research_desk_unavailability_disqualifies_only_the_correction(tmp_path):
    repo = MeetingRepository(tmp_path)

    class UnavailableDesk:
        def research(self, *args, **kwargs):
            raise RepresentativeUnavailableError("temporary retrieval outage")

    runner = object.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    runner.research_desk = UnavailableDesk()
    assignment = RenderingAssignment(
        section_id="SR-001", title="Section", source_block_ids=["SB-001"]
    )
    reviews = [
        {
            "reviewer_id": "R-ABC123",
            "review_number": 1,
            "review": {
                "vote": "NO",
                "objections": [
                    {
                        "issue": "A correction is proposed.",
                        "proposed_wording": "Corrected wording.",
                        "requires_external_verification": True,
                        "verification_claim": "The bounded empirical claim is true.",
                    }
                ],
            },
        }
    ]
    supported, disqualified = runner._verify_science_corrections(assignment, 1, reviews)
    assert supported == []
    assert disqualified == {"R-ABC123"}
    frozen = tmp_path / (
        "governance_private/scholarly_rendering/research_verifications/SR-001/round_1.json"
    )
    assert json.loads(frozen.read_text(encoding="utf-8"))["checks"][0]["status"] == (
        "UNSUPPORTED_CORRECTION"
    )


def test_unresolved_citation_anchor_invalidates_only_that_amendment(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once(
        "public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"})
    )
    runner = object.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    assignment = RenderingAssignment(
        section_id="SR-001", title="Section", source_block_ids=["SB-001"]
    )
    amendment = CitationAmendment(
        amendment_id="CA-001",
        operation="INSERT",
        target_text="same claim",
        citation_text=" [1]",
        rationale="Attach the citation.",
    )
    docket = CitationDocket(amendments=[amendment])
    application = ChairCitationApplication(
        decisions=[
            CitationDisposition(amendment_id="CA-001", apply=True, rationale="Apply.")
        ]
    )
    repo.docs.write_once(
        "public/scholarly_rendering/sections/SR-001/citation_anchor_repair_1.json",
        CitationAnchorRepair(amendments=[amendment]).model_dump_json(indent=2),
    )
    assert runner._apply_citation_with_repair(
        assignment, 1, "same claim; same claim", docket, application
    ) == "same claim; same claim"
    record = json.loads((repo.root /
        "public/scholarly_rendering/sections/SR-001/citation_invalid_amendments_1.json"
    ).read_text(encoding="utf-8"))
    assert record["reason_code"] == "CITATION_ANCHOR_NOT_UNIQUE_AFTER_REPAIR"
    assert [(item["amendment_id"], item["occurrences"]) for item in record["invalid_amendments"]] == [
        ("CA-001", 2)
    ]
    assert HumanConsultationService(repo).open_issues() == []


def test_missing_citation_anchor_is_invalid_and_other_amendments_continue(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = object.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    assignment = RenderingAssignment(section_id="SR-004", title="Section", source_block_ids=["SB-001"])
    amendments = [
        CitationAmendment(
            amendment_id="CA-001", operation="INSERT", target_text="not in the draft",
            citation_text=" [1]", rationale="No valid anchor.",
        ),
        CitationAmendment(
            amendment_id="CA-002", operation="INSERT", target_text="Existing claim",
            citation_text=" [2]", rationale="A valid anchor.",
        ),
    ]
    docket = CitationDocket(amendments=amendments)
    application = ChairCitationApplication(decisions=[
        CitationDisposition(amendment_id=item.amendment_id, apply=True, rationale="Apply.")
        for item in amendments
    ])
    repo.docs.write_once(
        "public/scholarly_rendering/sections/SR-004/citation_anchor_repair_1.json",
        CitationAnchorRepair(amendments=amendments).model_dump_json(indent=2),
    )
    issue_id = "HC-SR-SR-004-CITATION-R1"
    service = HumanConsultationService(repo)
    service.open_issue(HumanConsultationIssue(
        issue_id=issue_id, meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_CITATION_TARGET_AMBIGUOUS",
        stage="SCHOLARLY_CITATION_REVIEW", question="Old ambiguity question.",
        options=["SKIP_AMBIGUOUS_CITATION_AMENDMENTS", "KEEP_PRE_CITATION_TEXT",
                 "USE_HUMAN_WORDING"],
    ))
    assert runner._apply_citation_with_repair(
        assignment, 1, "Existing claim", docket, application,
    ) == "Existing claim [2]"
    assert service.open_issues() == []
    assert (repo.root / f"human_private/consultations/{issue_id}.withdrawn.json").is_file()
    record = json.loads((repo.root /
        "public/scholarly_rendering/sections/SR-004/citation_invalid_amendments_1.json"
    ).read_text(encoding="utf-8"))
    assert [item["amendment_id"] for item in record["invalid_amendments"]] == ["CA-001"]
    assert record["invalid_amendments"][0]["occurrences"] == 0
    assert runner._apply_citation_with_repair(
        assignment, 1, "Existing claim", docket, application,
    ) == "Existing claim [2]"
    assert repo.events.verify()


def test_resolved_citation_consultation_retains_human_choice(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once("public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"}))
    runner = object.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    assignment = RenderingAssignment(section_id="SR-001", title="Section", source_block_ids=["SB-001"])
    amendment = CitationAmendment(
        amendment_id="CA-001", operation="INSERT", target_text="absent",
        citation_text=" [1]", rationale="Attach citation.",
    )
    docket = CitationDocket(amendments=[amendment])
    application = ChairCitationApplication(decisions=[
        CitationDisposition(amendment_id="CA-001", apply=True, rationale="Apply.")
    ])
    repo.docs.write_once(
        "public/scholarly_rendering/sections/SR-001/citation_anchor_repair_1.json",
        CitationAnchorRepair(amendments=[amendment]).model_dump_json(indent=2),
    )
    service = HumanConsultationService(repo)
    issue_id = "HC-SR-SR-001-CITATION-R1"
    service.open_issue(HumanConsultationIssue(
        issue_id=issue_id, meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_CITATION_TARGET_AMBIGUOUS",
        stage="SCHOLARLY_CITATION_REVIEW", question="Frozen choice.",
        options=["SKIP_AMBIGUOUS_CITATION_AMENDMENTS", "KEEP_PRE_CITATION_TEXT",
                 "USE_HUMAN_WORDING"],
    ))
    service.resolve(
        issue_id=issue_id, decision="KEEP_PRE_CITATION_TEXT",
        rationale="Preserve the text.", scope="THIS_CONSULTATION_ONLY",
    )
    assert runner._apply_citation_with_repair(
        assignment, 1, "Current text", docket, application,
    ) == "Current text"
    assert not (repo.root / f"human_private/consultations/{issue_id}.withdrawn.json").exists()


def test_changed_science_verification_input_opens_integrity_consultation(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once(
        "public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"})
    )
    runner = object.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    assignment = RenderingAssignment(
        section_id="SR-001", title="Section", source_block_ids=["SB-001"]
    )
    repo.docs.write_once(
        "governance_private/scholarly_rendering/research_verifications/SR-001/round_1.json",
        json.dumps(
            {
                "section_id": "SR-001",
                "round_number": 1,
                "input_sha256": "old-digest",
                "supported": [],
                "disqualified_reviewer_ids": [],
                "checks": [],
            }
        ),
    )
    reviews = [
        {
            "reviewer_id": "R-ABC123",
            "review_number": 1,
            "review": {"vote": "YES", "objections": []},
        }
    ]
    with pytest.raises(ScholarlyRenderingPaused, match="SCHOLARLY_RENDERING_INTEGRITY_CONFLICT"):
        runner._verify_science_corrections(assignment, 1, reviews)
    assert HumanConsultationService(repo).open_issues()[0].issue_id == (
        "HC-SR-SR-001-VERIFY-R1"
    )


def test_invalid_citation_vote_contract_pauses_then_can_be_recorded_missing(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once(
        "public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"})
    )
    runner = object.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    invalid = CitationVoteSet(
        votes=[CitationVoteItem(amendment_id="CA-001", vote="YES")]
    )
    runner._invoke = lambda *args, **kwargs: invalid
    assignment = RenderingAssignment(
        section_id="SR-001", title="Section", source_block_ids=["SB-001"]
    )
    docket = CitationDocket(
        amendments=[
            CitationAmendment(
                amendment_id=f"CA-{index:03d}",
                operation="REMOVE",
                target_text=f" [{index}]",
                rationale="Remove misplaced citation.",
            )
            for index in (1, 2)
        ]
    )
    with pytest.raises(
        ScholarlyRenderingPaused, match="SCHOLARLY_CITATION_VOTE_CONTRACT_INVALID"
    ):
        runner._citation_vote_with_contract_repair(
            rid="R-ABC123",
            assignment=assignment,
            round_number=1,
            docket=docket,
            current="Text [1] [2].",
        )
    HumanConsultationService(repo).resolve(
        issue_id="HC-SR-SR-001-VOTE-R1-R-ABC123",
        decision="EXCLUDE_INVALID_CITATION_VOTE",
        rationale="Two bounded attempts remained incomplete.",
        scope="THIS_CONSULTATION_ONLY",
    )
    assert runner._citation_vote_with_contract_repair(
        rid="R-ABC123",
        assignment=assignment,
        round_number=1,
        docket=docket,
        current="Text [1] [2].",
    ) == [
        {"amendment_id": "CA-001", "vote": "MISSING"},
        {"amendment_id": "CA-002", "vote": "MISSING"},
    ]


def test_publication_citation_error_opens_recoverable_consultation(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once(
        "public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"})
    )
    runner = object.__new__(ScholarlyRenderingRunner)
    runner.repo = repo
    body = "# Review\n\nClaim [2].\n\n## References\n\n[1] A.\n"
    with pytest.raises(
        ScholarlyRenderingPaused,
        match="SCHOLARLY_PUBLICATION_CITATION_INTEGRITY_WARNING",
    ):
        runner._resolve_publication_citation_validation(body)
    issue = HumanConsultationService(repo).open_issues()[0]
    assert issue.issue_id == "HC-SR-PUBLICATION-CITATIONS"
    HumanConsultationService(repo).resolve(
        issue_id=issue.issue_id,
        decision="PUBLISH_WITH_CITATION_WARNING",
        rationale="The original report remains available for comparison.",
        scope="THIS_CONSULTATION_ONLY",
    )
    published = runner._resolve_publication_citation_validation(body)
    assert published.startswith("> **Citation integrity warning:**")
