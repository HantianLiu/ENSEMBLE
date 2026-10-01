import io
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from project_ensemble.domain import DeliverableType, MeetingType, ModelDescriptor, Persona, ReasoningEffort
from project_ensemble.errors import (
    ModelReplacementRequested, OpenAlexDailyQuotaExhausted,
    ProviderContentRejectedError, RepresentativeUnavailableError, TransientProviderError,
)
from project_ensemble.orchestration.consultations import HumanConsultationIssue, HumanConsultationService
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.literature_fast import (
    FastBreadthSearchPlan, FastLiteratureRunner, FastLocalScienceRepair, FastModulePlan, FastPlanningTurn, FastQueryBatch,
    FastResearchDeskPause, FastResearchDeskRetry, FastSearchQueries, FastSplitProposal, FastTaskbook,
    FastWholeSynthesis, request_fast_research_rollback,
)
from project_ensemble.runtime.fast_scope_consultation import (
    prompt_fast_scope_consultation, read_scope_decisions, unique_scope_changes,
)
from project_ensemble.runtime.progress import NullProgressReporter
from project_ensemble.orchestration.literature_writing_v071 import (
    LiteratureWritingPaused, ScienceChecklist, WriterChapter,
)
from project_ensemble.orchestration.literature_report_execution import ModuleDraft
from project_ensemble.orchestration.literature_report import OutlineModule
from project_ensemble.startup import StartupSelection, TerminalWizard, validate_noninteractive_selection
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.runtime.model_replacements import current_runtime_for, meeting_participant_ids
from project_ensemble.research.retrievers import PolicyResearchRetriever, ResearchRetrievalResult
from project_ensemble.research.desk import AdjustableCallGate
from project_ensemble.research.models import NormalizedClaim
from test_startup import config


def test_fast_split_proposals_are_parallel_anonymous_and_replayable(tmp_path):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    runner.manifest = {"fast_planner_models": [("fake", "model-a"), ("fake", "model-b")]}
    runner.engine = SimpleNamespace(progress=NullProgressReporter())
    runner._planning_search = lambda **kwargs: (
        runner.repo.docs.write_once(
            Path("public/literature_report/fast/planning_search") / f"{kwargs['key']}.json",
            json.dumps({"question": kwargs["question"], "answer_text": "检索线索", "sources": [],
                        "evidence_status": "DISCOVERY_ONLY", "requester_id": kwargs["requester_id"]}),
        ) or {"question": kwargs["question"], "answer_text": "检索线索", "sources": [],
              "evidence_status": "DISCOVERY_ONLY"}
    )
    barrier = threading.Barrier(2)
    calls = []

    def invoke(participant_id, *, stage, schema, system, user):
        calls.append((participant_id, stage, user))
        if schema is FastBreadthSearchPlan:
            barrier.wait(timeout=3)  # Both planning calls must be in flight together.
            return FastBreadthSearchPlan(question="如何拆分？", search_queries=[participant_id])
        assert schema is FastSplitProposal
        assert user["exploratory_search"]["evidence_status"] == "DISCOVERY_ONLY"
        return FastSplitProposal.model_validate({
            "core_problem": "研究问题", "hard_constraints_seen": ["保持用户范围"],
            "proposed_modules": [{"title": "模块", "central_problem": "核心问题", "evidence_needs": ["原始文献"]}],
            "cross_module_synthesis": "最后比较", "planning_caveat": "检索只是线索",
        })

    runner._invoke_service = invoke
    task = {"description": "研究一个问题"}
    result = FastLiteratureRunner._parallel_split_proposals(runner, task)
    assert len(result) == 2
    assert [entry["anonymous_proposal"] for entry in result] == [1, 2]
    assert all(entry["discovery_only"]["answer_text"] == "检索线索" for entry in result)
    assert "model-a" not in json.dumps(result) and "model-b" not in json.dumps(result)
    assert "requester_id" not in json.dumps(result)
    assert len(calls) == 4
    runner._invoke_service = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must replay"))
    assert FastLiteratureRunner._parallel_split_proposals(runner, task) == result


def test_fast_consultation_resume_reuses_frozen_resolution_before_reopening_issue(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "model")],
        chair_model=("fake", "model"),
        governance_docs=gov,
    )
    service = HumanConsultationService(repo)
    frozen = HumanConsultationIssue(
        issue_id="HC-FAST-SCIENCE-RM-07",
        meeting_id=repo.meeting_id,
        reason_code="FAST_SCIENCE_REVIEW_HUMAN_REQUIRED",
        stage="FAST_SCIENCE_REVIEW",
        question="旧版本的科学异议决定",
        options=["RETRY_WRITER_REVISION", "ACCEPT_WITH_DISCLOSED_LIMITATION"],
        context={"recheck_path": "science_evidence_appeal_v2.json"},
    )
    service.open_issue(frozen)
    recorded = service.resolve(
        issue_id=frozen.issue_id,
        decision="RETRY_WRITER_REVISION",
        rationale="Human selected RETRY_WRITER_REVISION; no additional note.",
        scope="THIS_CONSULTATION_ONLY",
    )

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = repo
    resumed = FastLiteratureRunner._consult(
        runner,
        frozen.issue_id,
        "FAST_SCIENCE_REVIEW",
        "更新后的上下文路径不应覆盖已经冻结的决定",
        ["RETRY_WRITER_REVISION", "ACCEPT_WITH_DISCLOSED_LIMITATION"],
        {"recheck_path": "science_evidence_appeal_v3.json"},
    )

    assert resumed == recorded
    persisted_issue = HumanConsultationIssue.model_validate_json(
        (repo.root / "human_private/consultations"
         / f"{frozen.issue_id}.issue.json").read_text(encoding="utf-8")
    )
    assert persisted_issue == frozen


def test_fast_writer_receives_anonymous_proposals_but_retains_own_taskbook_authority(
    tmp_path, monkeypatch,
):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    runner.engine = SimpleNamespace(progress=NullProgressReporter())
    runner.research_desk = None
    runner._parallel_split_proposals = lambda task: [
        {"anonymous_proposal": 1, "content": {"core_problem": "候选 A"}},
        {"anonymous_proposal": 2, "content": {"core_problem": "候选 B"}},
    ]
    runner._read_json = lambda relative: {"description": "用户硬约束必须保留"}
    runner._consult = lambda *args: SimpleNamespace(decision="APPROVE_TASKBOOK")
    taskbook = FastTaskbook.model_validate({
        "hard_constraints": ["用户硬约束必须保留"],
        "global_evidence_requirements": ["核验原始材料"],
        "completion_standard": "回答问题且注明证据边界",
        "outline": {"report_title": "主笔独立任务书", "scope_note": "保持用户范围",
                    "modules": [{"module_id": "RM-01", "title": "主笔的模块",
                                 "research_questions": ["如何判断？"],
                                 "required_evidence": ["原始材料"],
                                 "source_submission_refs": ["WRITER"]}]},
    })
    calls = []

    def frozen_or_call(_runner, _relative, schema, participant, stage, system, user):
        calls.append((schema, participant, stage, system, user))
        return FastPlanningTurn(action="PROPOSE", taskbook=taskbook)

    monkeypatch.setattr("project_ensemble.orchestration.literature_fast._frozen_or_call", frozen_or_call)
    approved = FastLiteratureRunner._taskbook(runner)
    assert approved.outline.report_title == "主笔独立任务书"
    assert calls[0][1] == "WRITER"
    assert calls[0][4]["anonymous_split_proposals"][1]["content"]["core_problem"] == "候选 B"
    assert "模型" not in json.dumps(calls[0][4], ensure_ascii=False)
    assert "用户硬约束绝不可动" in calls[0][3]


def test_fast_synthesis_c_source_metadata_uses_frozen_catalog_without_rerunning_writer(tmp_path):
    class Docs:
        def write_once(self, relative, text):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    source = Path("public/literature_report/fast/whole_report_synthesis.json")
    original = FastWholeSynthesis(
        title="研究综述", cross_module_synthesis="具体结论[C00004-00002]。",
        cited_packet_ids=["C00004-00002"],
    )
    (tmp_path / source).parent.mkdir(parents=True)
    (tmp_path / source).write_text(original.model_dump_json(), encoding="utf-8")
    runner = SimpleNamespace(
        repo=SimpleNamespace(root=tmp_path, docs=Docs()),
        _compact_evidence_index=lambda: [{"packet_id": "RP-FOUND"}],
        _validate_citations=lambda packet_ids: None,
    )
    normalized, effective = FastLiteratureRunner._normalize_synthesis_citation_metadata(
        runner, original, source,
        {"sources": [{"citation_id": "C00004-00002", "packet_ids": ["RP-FOUND"]}]},
    )
    assert normalized.cited_packet_ids == []
    assert normalized.cross_module_synthesis == original.cross_module_synthesis
    assert json.loads((tmp_path / source).read_text(encoding="utf-8"))["cited_packet_ids"] == ["C00004-00002"]
    assert json.loads((tmp_path / effective).read_text(encoding="utf-8"))["cited_packet_ids"] == []
    assert json.loads((tmp_path / effective.with_name(
        effective.stem + "_trace.json")).read_text(encoding="utf-8"))["source_to_packet_ids"] == {
        "C00004-00002": ["RP-FOUND"]
    }


def test_fast_synthesis_unknown_inline_source_opens_repair_consultation(tmp_path):
    source = Path("public/literature_report/fast/whole_report_synthesis.json")
    synthesis = FastWholeSynthesis(title="Review", abstract="Unsupported [C00004-99999].")
    calls = []
    runner = SimpleNamespace(
        repo=SimpleNamespace(root=tmp_path),
        _compact_evidence_index=lambda: [],
        _consult=lambda *args: calls.append(args) or SimpleNamespace(decision="KEEP_PAUSED"),
    )
    with pytest.raises(LiteratureWritingPaused):
        FastLiteratureRunner._normalize_synthesis_citation_metadata(
            runner, synthesis, source, {"sources": []},
        )
    assert calls[0][4]["unknown_ids"] == ["C00004-99999"]


def test_fast_synthesis_module_ids_are_not_evidence_packet_citations():
    synthesis = FastWholeSynthesis.model_validate({
        "title": "Report", "abstract": "A finding [RP-REAL].",
        "cited_packet_ids": ["RM-01", "RP-REAL", "RM-06"],
    })
    assert synthesis.cited_packet_ids == ["RP-REAL"]
    assert FastWholeSynthesis.model_validate_json(synthesis.model_dump_json()).cited_packet_ids == ["RP-REAL"]


def test_human_can_replan_only_an_unfinished_fast_research_item(tmp_path):
    from project_ensemble.storage.documents import ImmutableDocumentStore

    events = []
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *args, **kwargs: events.append((args, kwargs))),
    )
    module = SimpleNamespace(module_id="RM-03")
    original = NormalizedClaim.model_validate({
        "is_researchable": True, "normalized_claim": "A scoped claim",
        "verification_question": "Does the source support it?",
        "supporting_query": "old support", "contradictory_query": "old contrary",
        "limitations_query": "old limitations", "alternatives_query": "old alternatives",
        "source_domain": "ACADEMIC", "source_domain_rationale": "Research claim",
        "freshness_class": "STABLE", "freshness_rationale": "Stable literature",
    })
    stage = tmp_path / "audit_private/research/fast_stages"
    stage.mkdir(parents=True)
    original_file = stage / "FAST-RM-03-1-06-normalized.json"
    original_file.write_text(json.dumps({"claim": "A scoped claim", "normalized_claim": original.model_dump(mode="json")}))
    original_bytes = original_file.read_bytes()
    request_fast_research_rollback(runner.repo, "FAST-RM-03-1-06", "use short topical terms")
    runner._invoke_service = lambda *_args, **_kwargs: FastSearchQueries(
        supporting_query="new support", contradictory_query="new contrary",
        limitations_query="new limitations", alternatives_query="new alternatives",
    )
    revised = runner._fast_normalize_claim(module, 1, 6, "A scoped claim")
    assert revised.supporting_query == "new support"
    assert revised.normalized_claim == original.normalized_claim
    assert revised.verification_question == original.verification_question
    assert original_file.read_bytes() == original_bytes
    assert (stage / "FAST-RM-03-1-06-normalized-rollback-0001.json").is_file()
    assert runner._fast_normalize_claim(module, 1, 6, "A scoped claim") == revised
    completed = tmp_path / "public/literature_report/fast/RM-03/research_round_1_06.json"
    completed.parent.mkdir(parents=True)
    completed.write_text("{}")
    with pytest.raises(ValueError, match="completed research"):
        request_fast_research_rollback(runner.repo, "FAST-RM-03-1-06", "another attempt")


def test_fast_menu_uses_writer_and_two_reviewers_without_chair(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [
            ModelDescriptor(provider_id="fake", model_id="writer"),
            ModelDescriptor(provider_id="fake", model_id="reviewer"),
        ],
    )
    answers = iter([
            "1", "1", "1", "1,2", "1", "1", "1", "1", "1,2", "2", "1", "", "",
            "初步研究目标", "1", "", "3", "3", "4", "10000", "1", "", "1", "y",
    ])
    selection = TerminalWizard(
            input_fn=lambda prompt: (
                "" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次"))
                else next(answers)
            ), output=io.StringIO()
    ).collect(config(tmp_path))
    assert selection.literature_writing_policy == "fast"
    assert selection.chair_model is None
    assert len(selection.models) == 2
    assert selection.writer_model == ("fake", "writer")
    assert selection.fast_planner_models == (("fake", "writer"), ("fake", "reviewer"))


def test_fast_menu_can_skip_parallel_split_proposals(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [
            ModelDescriptor(provider_id="fake", model_id="writer"),
            ModelDescriptor(provider_id="fake", model_id="reviewer"),
        ],
    )
    answers = iter([
        "1", "1", "1", "1,2", "1", "1", "1", "2", "1", "1", "", "",
        "初步研究目标", "1", "", "3", "3", "4", "10000", "1", "", "1", "y",
    ])
    selection = TerminalWizard(
        input_fn=lambda prompt: (
            "" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次"))
            else next(answers)
        ), output=io.StringIO()
    ).collect(config(tmp_path))
    assert selection.literature_writing_policy == "fast"
    assert selection.fast_planner_models == ()


def test_fast_split_proposals_return_empty_when_skipped():
    runner = FastLiteratureRunner.__new__(FastLiteratureRunner)
    runner.manifest = {"fast_planner_models": []}
    assert runner._parallel_split_proposals({"task": "test"}) == []


def test_fast_selection_requires_distinct_reviewers_and_no_chair(tmp_path):
    base = dict(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "writer"), ("fake", "reviewer")),
        chair_model=None,
        escalation_email=None,
        task_description="研究任务",
        meeting_title="研究任务",
        research_enabled=True,
        research_model=("fake", "reviewer"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        writer_model=("fake", "writer"),
        writer_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_writing_policy="fast",
        fast_planner_models=(("fake", "writer"), ("fake", "reviewer")),
    )
    validate_noninteractive_selection(StartupSelection(**base), config(tmp_path))
    with pytest.raises(ValueError, match="at least two"):
        validate_noninteractive_selection(StartupSelection(**{**base, "models": (("fake", "writer"),)}), config(tmp_path))
    with pytest.raises(ValueError, match="do not appoint a Chair"):
        validate_noninteractive_selection(StartupSelection(**{**base, "chair_model": ("fake", "writer")}), config(tmp_path))


def test_fast_repository_has_only_librarian_reviewers(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rules.md").write_text("frozen rules", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "workspace",
        selected_models=[("fake", "writer"), ("fake", "reviewer")],
        chair_model=None,
        governance_docs=governance,
        personas=[Persona.LIBRARIAN],
        meeting_type=MeetingType.DELIBERATION,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        task_description="研究任务",
        meeting_title="研究任务",
        research_enabled=True,
        research_model=("fake", "reviewer"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        writer_model=("fake", "writer"),
        writer_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_writing_policy="fast",
        forced_meeting_id="LR-FAST01",
    )
    registry = json.loads((repo.root / "identity_private/representative_registry.json").read_text())
    assert len(registry) == 2
    assert {item["runtime"]["persona"] for item in registry} == {Persona.LIBRARIAN.value}
    manifest = json.loads((repo.root / "identity_private/meeting_manifest.json").read_text())
    assert manifest["chair_model"] is None
    assert manifest["literature_writing_policy"] == "fast"


def test_fast_planners_have_dedicated_runtime_without_becoming_science_reviewers(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rules.md").write_text("frozen rules", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "workspace", selected_models=[("fake", "reviewer-a"), ("fake", "reviewer-b")],
        chair_model=None, governance_docs=governance, personas=[Persona.LIBRARIAN],
        meeting_type=MeetingType.DELIBERATION, deliverable_type=DeliverableType.LITERATURE_REVIEW,
        task_description="研究任务", research_enabled=True, research_model=("fake", "reviewer-a"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        writer_model=("fake", "writer"), writer_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_writing_policy="fast", fast_planner_models=[("fake", "split-a"), ("fake", "split-b")],
        fast_planner_reasoning_effective={
            "FAST_PLANNER_1": ReasoningEffort.DEFAULT,
            "FAST_PLANNER_2": ReasoningEffort.HIGH,
        },
    )
    assert current_runtime_for(repo, "FAST_PLANNER_1") == ("fake", "split-a")
    assert current_runtime_for(repo, "FAST_PLANNER_2") == ("fake", "split-b")
    assert {"FAST_PLANNER_1", "FAST_PLANNER_2"} <= set(meeting_participant_ids(repo))
    engine = object.__new__(MeetingEngine)
    engine.repo = repo
    assert engine._reasoning_effort_for("FAST_PLANNER_2") == ReasoningEffort.HIGH
    reviewers = json.loads((repo.root / "identity_private/representative_registry.json").read_text())
    assert len(reviewers) == 2
    assert {tuple((item["runtime"]["provider_id"], item["runtime"]["model_id"]))
            for item in reviewers} == {("fake", "reviewer-a"), ("fake", "reviewer-b")}


def test_fast_structured_turns_keep_question_and_taskbook_separate():
    assert FastPlanningTurn(action="ASK", question="研究对象是什么？").action == "ASK"
    with pytest.raises(ValidationError):
        FastPlanningTurn(action="ASK", question="研究对象是什么？", taskbook={})
    with pytest.raises(ValidationError):
        FastQueryBatch(finished=False, claims=[], reason_to_continue_or_stop="缺证据")
    assert FastQueryBatch(finished=True, reason_to_continue_or_stop="证据足够").claims == []
    assert FastQueryBatch(
        finished=True, claims=["核查最后一个证据缺口"],
        reason_to_continue_or_stop="查完这一批即结束",
    ).claims == ["核查最后一个证据缺口"]
    assert FastQueryBatch(
        finished=False, glossary_claims=["关键术语的定义是否有原始来源？"],
        reason_to_continue_or_stop="需查证定义",
    ).glossary_claims
    with pytest.raises(ValidationError):
        FastQueryBatch(
            finished=False, claims=[f"问题 {index}" for index in range(6)],
            glossary_claims=["额外术语问题"], reason_to_continue_or_stop="问题过多",
        )


def test_fast_taskbook_dialogue_and_approval_resume_without_chair(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rules.md").write_text("frozen rules", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "workspace", selected_models=[("fake", "writer"), ("fake", "reviewer")],
        chair_model=None, governance_docs=governance, personas=[Persona.LIBRARIAN],
        meeting_type=MeetingType.DELIBERATION,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        task_description="研究目标", meeting_title="研究目标", research_enabled=True,
        research_model=("fake", "reviewer"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        writer_model=("fake", "writer"), writer_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_writing_policy="fast", forced_meeting_id="LR-FAST02",
    )
    runner = FastLiteratureRunner(repo=repo, engine=SimpleNamespace(progress=NullProgressReporter()), governance_docs=governance,
                                  research_desk=None)
    taskbook = FastTaskbook.model_validate({
        "global_evidence_requirements": ["核对原始论文"],
        "completion_standard": "回答范围内的核心问题；其余标为未解决",
        "outline": {
            "report_title": "研究目标", "scope_note": "只研究目标体系",
            "modules": [{"module_id": "RM-01", "title": "核心问题",
                         "research_questions": ["该体系有什么证据？"],
                         "required_evidence": ["原始论文"],
                         "source_submission_refs": ["WRITER"]}],
        },
    })
    calls = []

    def writer_only(participant_id, *, stage, schema, system, user):
        assert participant_id == "WRITER"
        calls.append(stage)
        if stage.endswith("01"):
            return FastPlanningTurn(action="ASK", question="研究对象限定为哪个体系？")
        assert user["dialogue"][-1]["human_answer"] == "目标体系 A"
        return FastPlanningTurn(action="PROPOSE", taskbook=taskbook)

    runner._invoke_service = writer_only
    with pytest.raises(LiteratureWritingPaused, match="FAST_TASKBOOK_QUESTION"):
        runner._taskbook()
    HumanConsultationService(repo).resolve(
        issue_id="HC-FAST-PLAN-01", decision="USE_HUMAN_WORDING",
        rationale="明确研究对象", human_wording="目标体系 A", scope="THIS_CONSULTATION_ONLY",
    )
    with pytest.raises(LiteratureWritingPaused, match="FAST_TASKBOOK_APPROVAL"):
        runner._taskbook()
    HumanConsultationService(repo).resolve(
        issue_id="HC-FAST-APPROVE-02", decision="APPROVE_TASKBOOK",
        rationale="同意", scope="THIS_CONSULTATION_ONLY",
    )
    assert runner._taskbook() == taskbook
    assert runner._taskbook() == taskbook
    assert calls == ["fast_taskbook_turn_01", "fast_taskbook_turn_02"]
    assert (repo.root / "public/literature_report/frozen_research_outline.json").is_file()


def test_fast_planning_search_limits_and_taskbook_refinement(tmp_path):
    assert FastBreadthSearchPlan(question="背景", search_queries=[f"路径 {n}" for n in range(8)])
    with pytest.raises(ValidationError):
        FastBreadthSearchPlan(question="背景", search_queries=[f"路径 {n}" for n in range(9)])
    with pytest.raises(ValidationError):
        FastPlanningTurn(action="SEARCH", question="背景", search_queries=["同一检索", "第二条", "第三条"])

    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rules.md").write_text("frozen rules", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "workspace", selected_models=[("fake", "writer"), ("fake", "reviewer")],
        chair_model=None, governance_docs=governance, personas=[Persona.LIBRARIAN],
        meeting_type=MeetingType.DELIBERATION,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        task_description="陌生主题", meeting_title="陌生主题", research_enabled=True,
        research_model=("fake", "reviewer"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        writer_model=("fake", "writer"), writer_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_writing_policy="fast", forced_meeting_id="LR-FAST03",
    )
    runner = FastLiteratureRunner(repo=repo, engine=SimpleNamespace(progress=NullProgressReporter()),
                                  governance_docs=governance, research_desk=SimpleNamespace())
    taskbook = FastTaskbook.model_validate({
        "global_evidence_requirements": ["核对原始论文"],
        "completion_standard": "回答核心问题",
        "outline": {"report_title": "陌生主题", "scope_note": "范围",
                    "modules": [{"module_id": "RM-01", "title": "核心问题",
                                 "research_questions": ["什么是已知的？"],
                                 "required_evidence": ["原始论文"],
                                 "source_submission_refs": ["WRITER"]}]},
    })
    calls = []
    searches = []
    def invoke(_participant_id, *, stage, schema, system, user):
        calls.append(stage)
        if stage == "fast_taskbook_turn_01":
            return FastPlanningTurn(action="SEARCH", question="陌生主题的背景是什么？",
                                    search_queries=["陌生主题 综述", "陌生主题 争议"])
        if stage == "fast_taskbook_turn_02":
            assert user["dialogue"][-1]["exploratory_result"]["evidence_status"].startswith("DISCOVERY_ONLY")
            return FastPlanningTurn(action="PROPOSE", taskbook=taskbook)
        if stage == "fast_planning_breadth_1":
            return FastBreadthSearchPlan(question="主要研究路径", search_queries=["路径一", "路径二"])
        if stage == "fast_taskbook_after_search_02":
            assert user["exploratory_breadth_search"]["question"] == "主要研究路径"
            return taskbook
        raise AssertionError(stage)
    runner._invoke_service = invoke
    def search(**kwargs):
        relative = runner._fast_root() / "planning_search" / f"{kwargs['key']}.json"
        if (repo.root / relative).is_file():
            return json.loads((repo.root / relative).read_text(encoding="utf-8"))
        searches.append(kwargs["key"])
        result = {"question": kwargs["question"], "searches_used": len(kwargs["queries"]),
                  "answer_text": "仅供规划", "evidence_status": "DISCOVERY_ONLY"}
        repo.docs.write_once(relative, json.dumps(result, ensure_ascii=False))
        return result
    runner._planning_search = search
    with pytest.raises(LiteratureWritingPaused, match="FAST_TASKBOOK_APPROVAL"):
        runner._taskbook()
    assert searches == ["dialogue_01", "step_1_breadth"]
    HumanConsultationService(repo).resolve(
        issue_id="HC-FAST-APPROVE-02", decision="APPROVE_TASKBOOK",
        rationale="同意", scope="THIS_CONSULTATION_ONLY",
    )
    assert runner._taskbook() == taskbook
    assert searches == ["dialogue_01", "step_1_breadth"]


def test_fast_scope_questions_are_answered_one_by_one_and_resume(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rules.md").write_text("frozen rules", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "workspace", selected_models=[("fake", "writer"), ("fake", "reviewer")],
        chair_model=None, governance_docs=governance, personas=[Persona.LIBRARIAN],
        meeting_type=MeetingType.DELIBERATION,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        task_description="研究目标", meeting_title="研究目标", research_enabled=True,
        research_model=("fake", "reviewer"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        writer_model=("fake", "writer"), writer_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_writing_policy="fast", forced_meeting_id="LR-SCOPE01",
    )
    taskbook = FastTaskbook.model_validate({
        "global_evidence_requirements": ["原文"], "completion_standard": "回答问题",
        "outline": {"report_title": "任务", "scope_note": "范围", "modules": [
            {"module_id": "RM-01", "title": "界面", "research_questions": ["界面如何定义？"],
             "included_scope": ["平直界面"], "excluded_scope": ["其他体系"],
             "required_evidence": ["原文"], "source_submission_refs": ["WRITER"]},
        ]},
    })
    repo.docs.write_once("public/literature_report/fast/approved_taskbook.json",
                         taskbook.model_dump_json(indent=2))
    questions = ["是否增加弯曲界面？", "其他体系是否纳入？"]
    issue_id = "HC-FAST-SCOPE-RM-01"
    issue = HumanConsultationIssue(
        issue_id=issue_id, meeting_id=repo.meeting_id,
        reason_code="FAST_SCOPE_QUESTION_HUMAN_REQUIRED", stage="FAST_SCOPE_QUESTION",
        question="旧版冻结咨询文本", options=["KEEP_APPROVED_SCOPE", "KEEP_PAUSED"],
        context={"module_id": "RM-01", "questions": questions,
                 "proposed_scope_change": None},
    )
    HumanConsultationService(repo).open_issue(issue)
    out = io.StringIO()
    answers = iter(["1", "这些问题都有被讨论的价值", "y", ""])
    assert not prompt_fast_scope_consultation(
        repo, issue, input_fn=lambda prompt: ("" if prompt.startswith("选择 1–2；回车默认等待") else next(answers)), output=out,
    )
    assert "已批准范围" in out.getvalue()
    assert "尚无具体修改稿" in out.getvalue()
    assert read_scope_decisions(repo, issue_id, 2) is None
    resumed = io.StringIO()
    revised_answers = iter([
        "u", "1", "纳入弯曲界面并单列适用条件", "n",
        "纳入弯曲界面并单列适用条件", "y",
        "99", "纳入弯曲界面并单列适用条件", "y", "99", "1",
    ])
    assert prompt_fast_scope_consultation(
        repo, issue, input_fn=lambda _: next(revised_answers), output=resumed,
    )
    assert "此前的决定已撤回" in resumed.getvalue()
    assert "未保存本次修改；请重新选择" in resumed.getvalue()
    assert "检测到直接输入的范围说明" in resumed.getvalue()
    assert "请输入 1 或 u" in resumed.getvalue()
    decisions = read_scope_decisions(repo, issue_id, 2)
    assert [item["decision"] for item in decisions] == ["CHANGE", "CHANGE"]
    assert unique_scope_changes(decisions) == ["纳入弯曲界面并单列适用条件"]
    assert "执行时只应用一次" in resumed.getvalue()
    assert json.loads((repo.root / "human_private/consultations"
                       / f"{issue_id}.item-01.decision.json").read_text())[
        "new_scope"
    ] == "这些问题都有被讨论的价值"
    assert (repo.root / "human_private/consultations"
            / f"{issue_id}.item-01.withdrawal-01.json").is_file()
    assert HumanConsultationService(repo).resolution(issue_id).decision == "KEEP_APPROVED_SCOPE"

    suggested_issue = HumanConsultationIssue(
        issue_id="HC-FAST-SCOPE-RM-03", meeting_id=repo.meeting_id,
        reason_code="FAST_SCOPE_QUESTION_HUMAN_REQUIRED", stage="FAST_SCOPE_QUESTION",
        question="逐条确认", options=["KEEP_APPROVED_SCOPE", "KEEP_PAUSED"],
        context={"module_id": "RM-03", "questions": ["是否需要扩大范围？"],
                 "proposed_scope_change": None},
    )
    HumanConsultationService(repo).open_issue(suggested_issue)
    repo.docs.write_once(
        "human_private/consultations/HC-FAST-SCOPE-RM-03.item-01.writer_advice.json",
        json.dumps({"decision": "KEEP", "rationale": "原范围足够", "new_scope": None}),
    )
    suggestion_out = io.StringIO()
    suggestion_answers = iter(["2", "1"])
    assert prompt_fast_scope_consultation(
        repo, suggested_issue, input_fn=lambda _: next(suggestion_answers),
        output=suggestion_out,
    )
    assert read_scope_decisions(repo, suggested_issue.issue_id, 1)[0]["decision"] == "KEEP"
    assert "主笔解释（建议维持；非裁定）" in suggestion_out.getvalue()
    assert "5. 直接采纳" not in suggestion_out.getvalue()

    plan = FastModulePlan.model_validate({
        "module_id": "RM-01", "core_problem": "如何定义界面", "boundary": "仅平直界面",
        "subquestions_and_priorities": ["定义"], "mandatory_evidence": ["原文"],
        "verification_and_conflict_handling": "比较定义", "completion_standard": "定义明确",
        "report_back_scope_questions": questions,
        "writing_outline": {"steps": [{"heading": "定义", "purpose": "解释定义",
                                       "evidence_boundary": "只用原文"}]},
    })
    runner = FastLiteratureRunner(repo=repo, engine=SimpleNamespace(),
                                  governance_docs=governance, research_desk=None)
    runner._invoke_service = lambda *_args, **_kwargs: plan
    effective = runner._module_plan(taskbook, taskbook.outline.modules[0])
    assert effective.approved_scope_changes == ["纳入弯曲界面并单列适用条件"]
    assert "纳入弯曲界面并单列适用条件" in effective.writing_outline.scope_notes[-1]
    assert runner._module_plan(taskbook, taskbook.outline.modules[0]) == effective

    delegated_issue = HumanConsultationIssue(
        issue_id="HC-FAST-SCOPE-RM-02", meeting_id=repo.meeting_id,
        reason_code="FAST_SCOPE_QUESTION_HUMAN_REQUIRED", stage="FAST_SCOPE_QUESTION",
        question="逐条确认", options=["KEEP_APPROVED_SCOPE", "KEEP_PAUSED"],
        context={"module_id": "RM-02", "questions": ["是否保留原范围？"],
                 "proposed_scope_change": None},
    )
    HumanConsultationService(repo).open_issue(delegated_issue)
    calls = []

    def invoke(participant_id, **kwargs):
        calls.append((participant_id, kwargs["stage"]))
        return SimpleNamespace(text='{"decision":"KEEP","rationale":"原范围内可回答","new_scope":null}')

    delegated_engine = SimpleNamespace(
        invoke_participant=invoke,
        validate_structured_response=lambda _id, *, response, schema_model, **_kw:
        schema_model.model_validate_json(response.text),
    )
    delegated_answers = iter(["3", "1"])
    delegated_out = io.StringIO()
    assert prompt_fast_scope_consultation(
        repo, delegated_issue, input_fn=lambda _: next(delegated_answers), output=delegated_out,
        engine=delegated_engine,
    )
    assert "正在取得学术主笔对本条的非约束建议" in delegated_out.getvalue()
    assert "右栏是主笔的非约束解释或提案" in delegated_out.getvalue()
    assert calls == [("WRITER", "fast_scope_advice_item_01")]
    assert read_scope_decisions(repo, delegated_issue.issue_id, 1)[0]["authority"] == (
        "HUMAN_DELEGATED_TO_WRITER"
    )


def test_fast_scope_standing_writer_delegation_persists_across_modules(tmp_path):
    from project_ensemble.runtime.fast_scope_consultation import prompt_fast_scope_consultation
    from project_ensemble.orchestration.consultations import HumanConsultationService, HumanConsultationIssue
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rules.md").write_text("frozen rules", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "workspace", selected_models=[("fake", "writer"), ("fake", "reviewer")],
        chair_model=None, governance_docs=governance, personas=[Persona.LIBRARIAN],
        meeting_type=MeetingType.DELIBERATION,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        task_description="研究", research_enabled=True,
        research_model=("fake", "reviewer"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        writer_model=("fake", "writer"), writer_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_writing_policy="fast", forced_meeting_id="LR-STANDING",
    )
    calls = []

    def invoke(participant_id, **kwargs):
        calls.append((participant_id, kwargs["stage"]))
        return SimpleNamespace(text='{"decision":"KEEP","rationale":"原范围足够","new_scope":null}')

    engine = SimpleNamespace(
        invoke_participant=invoke,
        validate_structured_response=lambda _id, *, response, schema_model, **_kw:
        schema_model.model_validate_json(response.text),
    )
    for module_id, answers in (
        ("RM-01", ["3", "2"]),
        ("RM-02", []),
    ):
        issue = HumanConsultationIssue(
            issue_id=f"HC-FAST-SCOPE-{module_id}", meeting_id=repo.meeting_id,
            reason_code="FAST_SCOPE_QUESTION_HUMAN_REQUIRED",
            stage="FAST_SCOPE_QUESTION", question="范围",
            options=["KEEP_APPROVED_SCOPE", "KEEP_PAUSED"],
            context={"module_id": module_id, "questions": ["是否改变范围？"]},
        )
        HumanConsultationService(repo).open_issue(issue)
        choices = iter(answers)
        assert prompt_fast_scope_consultation(
            repo, issue, input_fn=lambda _: next(choices), output=io.StringIO(),
            engine=engine,
        )
        assert read_scope_decisions(repo, issue.issue_id, 1)[0]["authority"] == (
            "HUMAN_DELEGATED_TO_WRITER"
        )
    assert len(calls) == 2
    assert (repo.root / "human_private/consultations/fast_scope_writer_standing_delegation.json").is_file()


def test_failed_fast_scope_writer_delegation_allows_human_recovery(tmp_path):
    from project_ensemble.runtime.fast_scope_consultation import prompt_fast_scope_consultation
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rules.md").write_text("rules", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "workspace", selected_models=[("fake", "writer")],
        chair_model=("fake", "chair"), governance_docs=governance,
        task_description="研究", forced_meeting_id="LR-A0B0C0D0",
    )
    issue = HumanConsultationIssue(
        issue_id="HC-FAST-SCOPE-RM-01", meeting_id=repo.meeting_id,
        reason_code="FAST_SCOPE_QUESTION_HUMAN_REQUIRED", stage="FAST_SCOPE_QUESTION",
        question="范围", options=["KEEP_APPROVED_SCOPE", "KEEP_PAUSED"],
        context={"module_id": "RM-01", "questions": ["是否改变范围？"]},
    )
    HumanConsultationService(repo).open_issue(issue)

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    engine = SimpleNamespace(invoke_participant=fail)
    answers = iter(["3", "1", "2", "1", "1"])
    output = io.StringIO()
    assert prompt_fast_scope_consultation(
        repo, issue, input_fn=lambda _: next(answers), output=output, engine=engine,
    )
    decision = read_scope_decisions(repo, issue.issue_id, 1)[0]
    assert decision["authority"] == "HUMAN"
    assert decision["decision"] == "KEEP"
    assert "provider unavailable" in output.getvalue()
    assert HumanConsultationService(repo).resolution(issue.issue_id) is not None


def test_fast_scope_duplicate_frozen_answers_get_one_execution_view(tmp_path):
    from project_ensemble.storage.documents import ImmutableDocumentStore

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=ImmutableDocumentStore(tmp_path))
    wording = "保留原问题，并新增条件适用性讨论"
    decisions = [
        {"item_number": index, "decision": "CHANGE", "new_scope": wording}
        for index in (1, 2)
    ]
    frozen = {"decision": "ITEMIZED_CHANGE", "proposed_scope_change": wording + "\n" + wording,
              "item_decisions": decisions}
    scope_path = tmp_path / "public/literature_report/fast/RM-01/scope_decision.json"
    scope_path.parent.mkdir(parents=True)
    scope_path.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
    outline_path = (tmp_path / "public/literature_report/modules/RM-01"
                    / "writing_v071/approved_outline.json")
    outline_path.parent.mkdir(parents=True)
    original = {"outline": {"steps": [{"heading": "定义", "purpose": "解释",
                                       "evidence_boundary": "原文"}],
                            "scope_notes": ["人类批准的局部范围变更：" + wording + "\n" + wording]},
                "decision": {"policy": "FAST_WRITER_PLAN", "scope_change": frozen}}
    outline_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    assert runner._effective_scope_view("RM-01")["effective_scope_changes"] == [wording]
    effective_path = runner._effective_fast_outline("RM-01")
    effective = json.loads(effective_path.read_text(encoding="utf-8"))
    assert effective_path != outline_path
    assert effective["outline"]["scope_notes"] == ["人类批准的局部范围变更：" + wording]
    assert json.loads(outline_path.read_text(encoding="utf-8")) == original


def test_fast_research_rounds_stop_at_three_and_order_results(tmp_path):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    from project_ensemble.runtime.progress import ConsoleProgressReporter
    progress_output = io.StringIO()
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None),
        progress=ConsoleProgressReporter(progress_output, color=False, live=True),
    )
    runner.research_max_concurrent_claim_groups = 2
    taskbook = FastTaskbook.model_validate({
        "global_evidence_requirements": ["原文"], "completion_standard": "回答问题",
        "outline": {"report_title": "任务", "scope_note": "范围",
                    "modules": [
                        {"module_id": mid, "title": mid, "research_questions": ["问题"],
                         "required_evidence": ["原文"], "source_submission_refs": ["WRITER"]}
                        for mid in ("RM-01", "RM-02")]},
    })
    calls = []
    attempts = {}
    runner._research_round = lambda _book, module, _plan, round_number, _prior: (
        FastQueryBatch(finished=False, claims=[f"{module.module_id} round {round_number}"],
                       reason_to_continue_or_stop="还有问题")
    )

    def research(module, round_number, index, claim):
        key = (module.module_id, round_number, index)
        relative = runner._fast_root() / module.module_id / f"research_round_{round_number}_{index:02d}.json"
        if (tmp_path / relative).exists():
            return json.loads((tmp_path / relative).read_text(encoding="utf-8"))
        calls.append((module.module_id, round_number, index))
        attempts[key] = attempts.get(key, 0) + 1
        if key == ("RM-01", 1, 1) and attempts[key] == 1:
            raise RepresentativeUnavailableError("temporary provider disconnect")
        if key == ("RM-01", 1, 1) and attempts[key] == 2:
            runner.engine.progress._control_requested.set()
            # A worker must not turn a pending Ctrl+R into a failed question.
            runner.engine.progress.raise_if_control_requested()
        result = {"claim": claim, "status": "UNRESOLVED", "packet_id": None}
        runner.repo.docs.write_once(relative, json.dumps(result))
        # The real Writer method freezes the batch before retrieval. Simulate
        # that durable checkpoint for the final ordering pass.
        batch = runner._fast_root() / module.module_id / f"query_round_{round_number}.json"
        if not (tmp_path / batch).exists():
            runner.repo.docs.write_once(batch, FastQueryBatch(
                finished=False, claims=[claim], reason_to_continue_or_stop="还有问题"
            ).model_dump_json())
        return result

    runner._research_claim = research
    runner._ensure_module_dossier = lambda module, _coverage, _followups: tmp_path / module.module_id
    with pytest.raises(ModelReplacementRequested):
        runner._research_all_modules(taskbook, {mid: None for mid in ("RM-01", "RM-02")})
    for mid in ("RM-01", "RM-02"):
        assert (tmp_path / f"public/literature_report/fast/{mid}/research_round_1_01.json").is_file()
    result = runner._research_all_modules(taskbook, {mid: None for mid in ("RM-01", "RM-02")})
    assert len(calls) == 7  # One transient provider failure is resubmitted once.
    assert {round_number for _mid, round_number, _index in calls} == {1, 2, 3}
    assert list(result) == ["RM-01", "RM-02"]
    coverage = json.loads((tmp_path / "public/literature_report/fast/RM-01/coverage.json").read_text())
    assert [item["claim"] for item in coverage["outcomes"]] == [
        "RM-01 round 1", "RM-01 round 2", "RM-01 round 3",
    ]
    displayed = progress_output.getvalue()
    assert "模块 1/2 · RM-01" in displayed
    assert "第 1/3 轮 · 提交待核查问题" in displayed
    assert "第 1/3 轮 · Research Desk 证据核查" in displayed
    assert "主笔确定问题" in displayed
    assert "Research Desk 核查证据" in displayed
    assert "已结束：完成 1/1" not in displayed
    assert "自动重新提交 1/1" in displayed


def test_fast_research_control_change_drains_inflight_and_restarts(tmp_path):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    events = []
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, docs=Docs(), meeting_id="LR-CONTROL",
        events=SimpleNamespace(append=lambda *args, **kwargs: events.append((args, kwargs))),
    )
    from project_ensemble.runtime.progress import ConsoleProgressReporter
    progress = ConsoleProgressReporter(io.StringIO(), color=False, live=True)
    runner.engine = SimpleNamespace(status=SimpleNamespace(phase=None), progress=progress)
    runner.research_max_concurrent_claim_groups = 1
    taskbook = FastTaskbook.model_validate({
        "global_evidence_requirements": ["原文"], "completion_standard": "回答问题",
        "outline": {"report_title": "任务", "scope_note": "范围", "modules": [
            {"module_id": mid, "title": mid, "research_questions": ["问题"],
             "required_evidence": ["原文"], "source_submission_refs": ["WRITER"]}
            for mid in ("RM-01", "RM-02")
        ]},
    })
    runner._research_round = lambda _book, module, _plan, _round, _prior: FastQueryBatch(
        finished=False, claims=[f"{module.module_id} claim"],
        reason_to_continue_or_stop="继续",
    )
    release = threading.Event()

    def research(module, round_number, index, claim):
        assert release.wait(2)
        result = {"claim": claim, "status": "UNRESOLVED", "packet_id": None}
        runner.repo.docs.write_once(
            runner._fast_root() / module.module_id / f"research_round_{round_number}_{index:02d}.json",
            json.dumps(result),
        )
        return result

    runner._research_claim = research
    asked = False

    def choose_control():
        nonlocal asked
        asked = True
        release.set()
        return [{"kind": "runtime_control", "control_kind": "model_concurrency",
                 "target": "deepseek:flash", "value": 4, "reason": "人类调整模型并行上限"}]

    runner.batch_control_callback = choose_control
    progress.control_request_pending = lambda: not asked
    with pytest.raises(FastResearchDeskRetry, match="changed future-call controls"):
        runner._research_all_modules(taskbook, {"RM-01": None, "RM-02": None})
    controls = list((tmp_path / "human_private/runtime_controls").glob("control-*.json"))
    assert len(controls) == 1
    assert json.loads(controls[0].read_text())["value"] == 4
    assert any(args[0] == "MEETING_RUNTIME_CONTROL_CHANGED" for args, _ in events)


def test_provider_content_rejection_does_not_freeze_a_false_research_result(tmp_path):
    from project_ensemble.orchestration.literature_report import OutlineModule

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)

    def rejected(**_kwargs):
        raise ProviderContentRejectedError(
            "Content Exists Risk", status_code=400,
            provider_request_id="abc-123",
        )

    runner._research_or_restore_model_prior = rejected
    module = OutlineModule(
        module_id="RM-03", title="质量控制", research_questions=["适用标准是什么？"],
        required_evidence=["药典原文"], source_submission_refs=["WRITER"],
    )
    with pytest.raises(ProviderContentRejectedError):
        runner._research_claim(module, 1, 1, "请核查适用版本")
    assert not (tmp_path / "public/literature_report/fast/RM-03/research_round_1_01.json").exists()


def test_fast_model_prior_correction_uses_writer_not_missing_chair(tmp_path):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    runner.manifest = {"literature_writing_policy": "fast"}
    runner._research_or_restore_model_prior = lambda **_kwargs: SimpleNamespace(
        packet_id="RP-TEST", knowledge_status=SimpleNamespace(value="SOURCE_BACKED"),
    )
    runner._validate_citations = lambda _ids: None
    calls = []

    def revise(participant_id, **kwargs):
        calls.append((participant_id, kwargs["stage"]))
        corrected = kwargs["user"]["draft"]
        return ModuleDraft.model_validate({
            **corrected, "body_markdown": corrected["body_markdown"] + " [RP-TEST]",
        })

    runner._invoke_service = revise
    module = OutlineModule(
        module_id="RM-01", title="研究问题", research_questions=["问题"],
        required_evidence=["原文"], source_submission_refs=["WRITER"],
    )
    draft = ModuleDraft(title="草稿", body_markdown="待核查命题", short_summary="摘要",
                        model_prior_claims=["可外部核查的命题"])
    result = runner._verify_model_prior_claims(
        module=module, version="v071-v1", draft=draft, requester_id="WRITER",
    )
    assert calls == [("WRITER", "writer_apply_model_prior_verification_RM-01_v071-v1")]
    assert result.cited_packet_ids == ["RP-TEST"]
    assert result.model_prior_claims == []


def test_ctrl_r_opens_menu_before_parallel_research_batch_finishes(tmp_path):
    from project_ensemble.runtime.progress import ConsoleProgressReporter

    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    progress = ConsoleProgressReporter(io.StringIO(), color=False, live=True)
    runner.engine = SimpleNamespace(status=SimpleNamespace(phase=None), progress=progress)
    runner.research_max_concurrent_claim_groups = 2
    taskbook = FastTaskbook.model_validate({
        "global_evidence_requirements": ["原文"], "completion_standard": "回答问题",
        "outline": {"report_title": "任务", "scope_note": "范围", "modules": [
            {"module_id": "RM-01", "title": "核心问题", "research_questions": ["问题"],
             "required_evidence": ["原文"], "source_submission_refs": ["WRITER"]},
        ]},
    })
    runner._research_round = lambda _book, _module, _plan, round_number, _prior: (
        FastQueryBatch(finished=round_number > 1,
                       claims=["问题一", "问题二"] if round_number == 1 else [],
                       reason_to_continue_or_stop="完成")
    )
    menu_seen = threading.Event()
    batch_write_lock = threading.Lock()
    runner.batch_control_callback = lambda: (menu_seen.set(), [])[1]

    def research(module, round_number, index, claim):
        if index == 1:
            progress._control_requested.set()
            progress.raise_if_control_requested()
        else:
            assert menu_seen.wait(timeout=3), "menu did not open while another question was running"
        relative = runner._fast_root() / module.module_id / f"research_round_{round_number}_{index:02d}.json"
        result = {"claim": claim, "status": "UNRESOLVED", "packet_id": None}
        runner.repo.docs.write_once(relative, json.dumps(result))
        batch = runner._fast_root() / module.module_id / f"query_round_{round_number}.json"
        with batch_write_lock:
            if not (tmp_path / batch).exists():
                runner.repo.docs.write_once(batch, FastQueryBatch(
                    finished=False, claims=["问题一", "问题二"], reason_to_continue_or_stop="完成",
                ).model_dump_json())
        return result

    runner._research_claim = research
    runner._ensure_module_dossier = lambda module, _coverage, _followups: tmp_path / module.module_id
    runner._research_all_modules(taskbook, {"RM-01": None})
    assert menu_seen.is_set()
    assert all((tmp_path / f"public/literature_report/fast/RM-01/research_round_1_{index:02d}.json").is_file()
               for index in (1, 2))


def test_fast_final_query_batch_still_researches_its_claims(tmp_path):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    runner.engine = SimpleNamespace(status=SimpleNamespace(phase=None), progress=NullProgressReporter())
    runner.research_max_concurrent_claim_groups = 1
    taskbook = FastTaskbook.model_validate({
        "global_evidence_requirements": ["原文"], "completion_standard": "回答问题",
        "outline": {"report_title": "任务", "scope_note": "范围", "modules": [
            {"module_id": "RM-01", "title": "核心问题", "research_questions": ["问题"],
             "required_evidence": ["原文"], "source_submission_refs": ["WRITER"]},
        ]},
    })
    calls = []

    def final_batch(_book, module, _plan, round_number, _prior):
        calls.append(round_number)
        batch = FastQueryBatch(
            finished=True, claims=["最后一项命题"],
            reason_to_continue_or_stop="本批之后不再查询",
        )
        runner.repo.docs.write_once(
            runner._fast_root() / module.module_id / f"query_round_{round_number}.json",
            batch.model_dump_json(),
        )
        return batch

    def research(module, round_number, index, claim):
        result = {"claim": claim, "status": "UNRESOLVED", "packet_id": None}
        runner.repo.docs.write_once(
            runner._fast_root() / module.module_id / f"research_round_{round_number}_{index:02d}.json",
            json.dumps(result),
        )
        return result

    runner._research_round = final_batch
    runner._research_claim = research
    runner._ensure_module_dossier = lambda module, coverage, _followups: coverage
    runner._research_all_modules(taskbook, {"RM-01": None})
    assert calls == [1]
    coverage = json.loads((tmp_path / runner._fast_root() / "RM-01/coverage.json").read_text())
    assert [item["claim"] for item in coverage["outcomes"]] == ["最后一项命题"]


def test_fast_openalex_500_uses_one_audited_technician_query_repair(tmp_path):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    manifest = tmp_path / "identity_private/meeting_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"technician_model": ["fake", "coding-model"]}))
    events = []
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, docs=Docs(), meeting_id="LR-TEST",
        events=SimpleNamespace(append=lambda *args, **kwargs: events.append(args[0])),
    )
    progress = NullProgressReporter()
    runner.engine = SimpleNamespace(progress=progress)
    original = NormalizedClaim.model_validate({
        "is_researchable": True, "normalized_claim": "Shannon defined conditional entropy",
        "verification_question": "What did Shannon define?",
        "supporting_query": "long Boolean supporting query",
        "contradictory_query": "long Boolean contradictory query",
        "limitations_query": "long Boolean limitations query",
        "alternatives_query": "long Boolean alternatives query",
        "scope_terms": ["conditional entropy"], "source_domain": "ACADEMIC",
        "source_domain_rationale": "Original academic definition",
        "freshness_class": "STABLE", "freshness_rationale": "Historical source",
    })
    model_calls = []

    def invoke(participant_id, *, stage, schema, system, user):
        model_calls.append(participant_id)
        assert participant_id == "TECHNICIAN" and schema is FastSearchQueries
        assert user["claim"] == "原始命题"
        return FastSearchQueries(
            supporting_query="Shannon conditional entropy",
            contradictory_query="Shannon entropy logarithm base",
            limitations_query="conditional entropy discrete variables",
            alternatives_query="entropy chain rule information theory",
        )

    class Retriever:
        backend_ids = ("openalex",)
        calls = []

        def retrieve(self, claim):
            self.calls.append(claim.supporting_query)
            if claim.supporting_query == original.supporting_query:
                raise TransientProviderError("OpenAlex retrieval failed: HTTP 500")
            return ResearchRetrievalResult([], [{"query": claim.supporting_query}], ("openalex",))

    retriever = Retriever()
    runner._invoke_service = invoke
    runner.research_desk = SimpleNamespace(
        retriever=retriever, retrieval_max_retries=0,
        retrieval_retry_base_delay_seconds=0,
    )
    result = runner._fast_retrieve_claim(SimpleNamespace(module_id="RM-07"), 1, 6,
                                         "原始命题", original)
    assert result.effective_backend_ids == ("openalex",)
    assert retriever.calls == [original.supporting_query, "Shannon conditional entropy"]
    assert model_calls == ["TECHNICIAN"]
    saved = json.loads((tmp_path / "audit_private/research/fast_stages/"
                        "FAST-RM-07-1-06-retrieval.json").read_text())
    assert saved["normalized_claim"] == original.model_dump(mode="json")
    assert saved["technician_search_repair_path"]
    assert "TECHNICIAN_SEARCH_REPAIR_RECORDED" in events


def test_research_fallback_scopes_do_not_replace_other_parallel_requests(tmp_path):
    from project_ensemble.errors import PermanentProviderError
    from project_ensemble.orchestration.literature_report import OutlineModule
    from project_ensemble.runtime.research_fallbacks import ResearchFallbacks

    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    class Events:
        def append(self, *_args, **_kwargs):
            pass

    class Engine:
        runtime = ("deepseek", "flash")

        @contextmanager
        def temporary_runtime(self, _participant, provider, model):
            previous = self.runtime
            self.runtime = (provider, model)
            try:
                yield
            finally:
                self.runtime = previous

    manifest = tmp_path / "identity_private/meeting_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text('{"research_model":["deepseek","flash"]}', encoding="utf-8")
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs(), events=Events(), meeting_id="LR-TEST")
    runner.engine = Engine()
    calls = []

    def research(**_kwargs):
        calls.append(runner.engine.runtime)
        if runner.engine.runtime == ("deepseek", "flash"):
            raise PermanentProviderError("source unavailable")
        return SimpleNamespace(packet_id="RP-TEST")

    runner._research_or_restore_model_prior = research
    module = OutlineModule(module_id="RM-01", title="问题", research_questions=["问题"],
                           required_evidence=["原文"], source_submission_refs=["WRITER"])
    fallbacks = ResearchFallbacks(runner.repo)
    fallbacks.choose(request_id="FAST-RM-01-1-01", source=("deepseek", "flash"),
                     target=("codex", "sol"), scope="REQUEST_ONLY", reason="Human one-time choice")
    assert runner._research_claim(module, 1, 1, "主张一")["status"] == "PACKET"
    assert calls == [("codex", "sol")]
    assert runner.engine.runtime == ("deepseek", "flash")

    fallbacks.choose(request_id="FAST-RM-01-1-02", source=("deepseek", "flash"),
                     target=("codex", "sol"), scope="ON_FUTURE_FAILURES", reason="Human meeting rule")
    assert runner._research_claim(module, 1, 2, "主张二")["status"] == "PACKET"
    assert calls[-2:] == [("deepseek", "flash"), ("codex", "sol")]
    assert runner.engine.runtime == ("deepseek", "flash")


def test_restored_fast_research_question_recovers_openalex_failure_reason(tmp_path):
    trace = tmp_path / "audit_private/research/traces/FAST-RM-02-1-01.json"
    trace.parent.mkdir(parents=True)
    trace.write_text(json.dumps({"search_queries": [
        {"backend_id": "openalex", "status": "BACKEND_UNAVAILABLE",
         "error_summary": "OpenAlex retrieval failed: HTTP 429; daily_remaining=0; reset_seconds=120"},
    ]}), encoding="utf-8")
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)

    assert runner._restored_openalex_warning("RM-02", 1, 1) == (
        "HTTP 429; daily_remaining=0; reset_seconds=120"
    )
    supplement = tmp_path / "public/literature_report/fast/RM-02/research_round_1_01_openalex_supplement.json"
    supplement.parent.mkdir(parents=True)
    supplement.write_text(json.dumps({"status": "PACKET", "packet_id": "RP-SUPPLEMENT"}), encoding="utf-8")
    recheck = tmp_path / "audit_private/research/fast_stages/FAST-RM-02-1-01-retrieval-recheck.json"
    recheck.parent.mkdir(parents=True)
    recheck.write_text(json.dumps({"effective_backend_ids": ["tavily", "openalex"],
                                   "failed_backend_ids": []}), encoding="utf-8")
    assert runner._restored_openalex_warning("RM-02", 1, 1) is None
    assert runner._restored_openalex_warning("RM-02", 1, 2) is None


@pytest.mark.parametrize("policy,allow_recheck", [("wait", False), ("tavily", True)])
def test_fast_resume_rechecks_tavily_only_429_without_rewriting_frozen_stages(
    tmp_path, policy, allow_recheck,
):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    class OpenAlex:
        calls = 0

        def retrieve(self, _claim):
            self.calls += 1
            return ResearchRetrievalResult(
                candidates=[{"source_id": "OA-1"}],
                query_trace=[{"backend_id": "openalex", "query": "q", "purpose": "supporting"}],
                effective_backend_ids=("openalex",),
            )

    stage = tmp_path / "audit_private/research/fast_stages"
    stage.mkdir(parents=True)
    original = stage / "FAST-RM-02-1-01-retrieval.json"
    original.write_text(json.dumps({
        "claim": "问题", "normalized_claim": {"id": "claim"},
        "candidates": [{"source_id": "T-1"}],
        "query_trace": [{"backend_id": "openalex", "status": "BACKEND_UNAVAILABLE",
                         "error_summary": "HTTP 429"}],
        "effective_backend_ids": ["tavily"], "failed_backend_ids": ["openalex"],
    }), encoding="utf-8")
    old_content = original.read_text(encoding="utf-8")
    oa = OpenAlex()
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    runner.research_desk = SimpleNamespace(
        retriever=PolicyResearchRetriever(oa, SimpleNamespace(), quota_policy=policy),
    )
    module = SimpleNamespace(module_id="RM-02")
    normalized = SimpleNamespace(is_researchable=True,
                                 model_dump=lambda **_: {"id": "claim"})

    result = runner._fast_retrieve_claim(
        module, 1, 1, "问题", normalized, allow_recheck=allow_recheck,
    )
    assert {item["source_id"] for item in result.candidates} == {"T-1", "OA-1"}
    assert result.failed_backend_ids == ()
    assert original.read_text(encoding="utf-8") == old_content
    assert (stage / "FAST-RM-02-1-01-retrieval-recheck.json").is_file()
    runner._fast_retrieve_claim(module, 1, 1, "问题", normalized)
    assert oa.calls == 1


def test_completed_tavily_only_claim_gets_append_only_openalex_supplement(tmp_path):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    module = SimpleNamespace(module_id="RM-02")
    base = runner._fast_root() / "RM-02"
    original = tmp_path / base / "research_round_1_02.json"
    original.parent.mkdir(parents=True)
    original.write_text(json.dumps({"claim": "问题", "status": "PACKET",
                                    "packet_id": "RP-ORIGINAL"}), encoding="utf-8")
    stage = tmp_path / "audit_private/research/fast_stages/FAST-RM-02-1-02-retrieval.json"
    stage.parent.mkdir(parents=True)
    stage.write_text(json.dumps({
        "failed_backend_ids": ["openalex"], "effective_backend_ids": ["tavily"],
        "query_trace": [{"backend_id": "openalex", "error_summary": "HTTP 429"}],
    }), encoding="utf-8")
    assert runner._needs_openalex_supplement("RM-02", 1, 2)
    calls = []

    def research(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(packet_id="RP-SUPPLEMENT")

    runner._research_or_restore_model_prior = research
    result = runner._supplement_frozen_fast_claim(
        module, 1, 2, "问题", normalized_claim=SimpleNamespace(),
        retrieval_result=ResearchRetrievalResult([], [], ("openalex", "tavily")),
        prepared_sources=([], []),
    )
    assert result["packet_id"] == "RP-SUPPLEMENT"
    assert calls[0]["request_id"] == "FAST-RM-02-1-02-OA-SUPP"
    assert calls[0]["force_refresh"] is True
    assert json.loads(original.read_text())["packet_id"] == "RP-ORIGINAL"
    assert not runner._needs_openalex_supplement("RM-02", 1, 2)


@pytest.mark.parametrize("blocked_stage", ["retrieve", "read", "finish"])
def test_staged_fast_research_pauses_after_confirmed_daily_exhaustion(
    tmp_path, blocked_stage,
):
    module = OutlineModule(
        module_id="RM-01", title="研究问题", research_questions=["问题"],
        required_evidence=["原文"], source_submission_refs=["WRITER"],
    )
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine = SimpleNamespace(progress=NullProgressReporter())
    runner.research_desk = object()
    runner.research_max_concurrent_claim_groups = 2
    completed = []
    attempts = {"retrieve": 0, "read": 0, "finish": 0}

    runner._fast_normalize_claim = lambda _module, _round, _index, _claim: SimpleNamespace(is_researchable=True)

    def retrieve(_module, _round, index, _claim, _normalized):
        if index == 1 and blocked_stage == "retrieve":
            attempts["retrieve"] += 1
            if attempts["retrieve"] == 1:
                raise OpenAlexDailyQuotaExhausted("daily credits exhausted", reset_seconds=0)
        return SimpleNamespace(query_trace=[])

    def read_sources(_module, _round, index, _claim, *_args):
        if index == 1 and blocked_stage == "read":
            attempts["read"] += 1
            if attempts["read"] == 1:
                raise OpenAlexDailyQuotaExhausted("daily credits exhausted", reset_seconds=0)
        return ([], [])

    def finish(_module, _round, index, _claim, **_kwargs):
        if index == 1 and blocked_stage == "finish":
            attempts["finish"] += 1
            if attempts["finish"] == 1:
                raise OpenAlexDailyQuotaExhausted("daily credits exhausted", reset_seconds=0)
        completed.append(index)
        return {"claim": f"问题 {index}", "status": "PACKET", "packet_id": f"RP-{index}"}

    runner._fast_retrieve_claim = retrieve
    runner._fast_read_sources = read_sources
    runner._research_claim = finish
    outcomes = {"RM-01": []}
    with pytest.raises(FastResearchDeskPause):
        runner._run_staged_research_jobs(
            [(module, 1, "问题 1"), (module, 2, "问题 2")], 1, outcomes,
        )

    assert 1 not in completed
    assert attempts[blocked_stage] == 1
    assert all(item["packet_id"] == "RP-2" for item in outcomes["RM-01"])


def test_daily_quota_prompt_does_not_block_unrelated_inflight_finish(tmp_path):
    module = OutlineModule(module_id="RM-01", title="研究问题", research_questions=["问题"],
                           required_evidence=["原文"], source_submission_refs=["WRITER"])
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine = SimpleNamespace(progress=NullProgressReporter())
    runner.research_desk = object()
    runner.research_max_concurrent_claim_groups = 2
    other_finished = threading.Event()
    runner._fast_normalize_claim = lambda *_args: SimpleNamespace(is_researchable=True)
    runner._fast_retrieve_claim = lambda *_args: SimpleNamespace(query_trace=[])
    runner._fast_read_sources = lambda *_args: ([], [])

    def finish(_module, _round, index, _claim, **_kwargs):
        if index == 1:
            raise OpenAlexDailyQuotaExhausted("daily credits exhausted", reset_seconds=3600)
        other_finished.set()
        return {"claim": "问题 2", "status": "PACKET", "packet_id": "RP-2"}

    runner._research_claim = finish
    observed_during_prompt = []

    def choose_without_backup(_error):
        observed_during_prompt.append(other_finished.wait(timeout=3))
        return False

    runner.batch_daily_quota_callback = choose_without_backup
    outcomes = {"RM-01": []}
    with pytest.raises(FastResearchDeskPause):
        runner._run_staged_research_jobs(
            [(module, 1, "问题 1"), (module, 2, "问题 2")], 1, outcomes,
        )
    assert other_finished.is_set()
    assert observed_during_prompt == [True]
    assert [item["packet_id"] for item in outcomes["RM-01"]] == ["RP-2"]


def test_daily_quota_backup_choice_retries_only_unfinished_search(tmp_path, monkeypatch):
    module = OutlineModule(module_id="RM-01", title="研究问题", research_questions=["问题"],
                           required_evidence=["原文"], source_submission_refs=["WRITER"])
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine = SimpleNamespace(progress=NullProgressReporter())
    runner.research_desk = object()
    runner.research_max_concurrent_claim_groups = 2
    runner._fast_normalize_claim = lambda *_args: SimpleNamespace(is_researchable=True)
    attempts = {1: 0, 2: 0}

    def retrieve(_module, _round, index, _claim, _normalized):
        attempts[index] += 1
        if index == 1 and attempts[index] == 1:
            raise OpenAlexDailyQuotaExhausted("daily credits exhausted", reset_seconds=3600)
        return SimpleNamespace(query_trace=[])

    runner._fast_retrieve_claim = retrieve
    runner._fast_read_sources = lambda *_args: ([], [])
    runner._research_claim = lambda _module, _round, index, _claim, **_kwargs: {
        "claim": f"问题 {index}", "status": "PACKET", "packet_id": f"RP-{index}",
    }
    runner.batch_daily_quota_callback = lambda _error: True
    runner.batch_retriever_refresh = lambda: None
    recorded = []
    monkeypatch.setattr(
        "project_ensemble.runtime.run_controls.record_run_control",
        lambda _repo, **kwargs: recorded.append(kwargs),
    )
    outcomes = {"RM-01": []}
    runner._run_staged_research_jobs(
        [(module, 1, "问题 1"), (module, 2, "问题 2")], 1, outcomes,
    )
    assert attempts == {1: 2, 2: 1}
    assert {item["packet_id"] for item in outcomes["RM-01"]} == {"RP-1", "RP-2"}
    assert recorded[0]["value"] == "tavily"


def test_next_search_can_start_before_previous_source_reading_finishes(tmp_path):
    module = OutlineModule(module_id="RM-01", title="研究问题", research_questions=["问题"],
                           required_evidence=["原文"], source_submission_refs=["WRITER"])
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine = SimpleNamespace(progress=NullProgressReporter())
    runner.research_desk = object()
    runner.research_max_concurrent_claim_groups = 2
    first_read_started = threading.Event()
    next_search_started = threading.Event()
    runner._fast_normalize_claim = lambda *_args: SimpleNamespace(is_researchable=True)

    def retrieve(_module, _round, index, _claim, _normalized):
        if index == 2:
            assert first_read_started.wait(timeout=3)
            next_search_started.set()
        return SimpleNamespace(query_trace=[])

    def read_sources(_module, _round, index, _claim, *_args):
        if index == 1:
            first_read_started.set()
            assert next_search_started.wait(timeout=3)
        return ([], [])

    runner._fast_retrieve_claim = retrieve
    runner._fast_read_sources = read_sources
    runner._research_claim = lambda _module, _round, index, _claim, **_kwargs: {
        "claim": f"问题 {index}", "status": "PACKET", "packet_id": f"RP-{index}",
    }
    outcomes = {"RM-01": []}
    runner._run_staged_research_jobs(
        [(module, 1, "问题 1"), (module, 2, "问题 2")], 1, outcomes,
    )
    assert next_search_started.is_set()
    assert {item["packet_id"] for item in outcomes["RM-01"]} == {"RP-1", "RP-2"}


def test_ctrl_r_increase_starts_unstarted_fast_question_before_active_one_finishes(
    tmp_path, monkeypatch,
):
    module = OutlineModule(module_id="RM-01", title="研究问题", research_questions=["问题"],
                           required_evidence=["原文"], source_submission_refs=["WRITER"])
    first_search_started = threading.Event()
    second_search_started = threading.Event()
    menu_applied = threading.Event()

    class Progress(NullProgressReporter):
        def control_request_pending(self):
            return first_search_started.is_set() and not menu_applied.is_set()

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine = SimpleNamespace(progress=Progress())
    runner.research_desk = object()
    runner.research_max_concurrent_claim_groups = 1
    runner._fast_normalize_claim = lambda *_args: SimpleNamespace(is_researchable=True)

    def retrieve(_module, _round, index, _claim, _normalized):
        if index == 1:
            first_search_started.set()
            assert second_search_started.wait(timeout=3)
        else:
            second_search_started.set()
        return SimpleNamespace(query_trace=[])

    runner._fast_retrieve_claim = retrieve
    runner._fast_read_sources = lambda *_args: ([], [])
    runner._research_claim = lambda _module, _round, index, _claim, **_kwargs: {
        "claim": f"问题 {index}", "status": "PACKET", "packet_id": f"RP-{index}",
    }

    def choose_increase():
        menu_applied.set()
        return [{"kind": "runtime_control", "control_kind": "research_parallelism",
                 "target": None, "value": 2, "reason": "use both slots"}]

    runner.batch_control_callback = choose_increase
    monkeypatch.setattr(
        "project_ensemble.runtime.run_controls.record_run_control",
        lambda *_args, **_kwargs: None,
    )
    outcomes = {"RM-01": []}
    runner._run_staged_research_jobs(
        [(module, 1, "问题 1"), (module, 2, "问题 2")], 1, outcomes,
    )
    assert second_search_started.is_set()
    assert {item["packet_id"] for item in outcomes["RM-01"]} == {"RP-1", "RP-2"}


def test_model_call_gate_resizes_without_interrupting_inflight_calls():
    gate = AdjustableCallGate(1)
    release = threading.Event()
    entered = [threading.Event() for _ in range(3)]
    threads = []

    def call(index):
        with gate:
            entered[index].set()
            assert release.wait(timeout=3)

    first = threading.Thread(target=call, args=(0,))
    first.start()
    threads.append(first)
    assert entered[0].wait(timeout=3)
    second = threading.Thread(target=call, args=(1,))
    second.start()
    threads.append(second)
    assert not entered[1].wait(timeout=0.05)
    gate.set_limit(2)
    assert entered[1].wait(timeout=3)
    gate.set_limit(1)
    third = threading.Thread(target=call, args=(2,))
    third.start()
    threads.append(third)
    assert not entered[2].wait(timeout=0.05)
    release.set()
    assert entered[2].wait(timeout=3)
    for thread in threads:
        thread.join(timeout=3)


def test_ctrl_r_decrease_waits_for_existing_slots_before_starting_new_question(
    tmp_path, monkeypatch,
):
    module = OutlineModule(module_id="RM-01", title="研究问题", research_questions=["问题"],
                           required_evidence=["原文"], source_submission_refs=["WRITER"])
    both_started = threading.Event()
    menu_applied = threading.Event()
    release_second = threading.Event()
    third_started = threading.Event()
    starts = set()
    lock = threading.Lock()
    observations = []

    class Progress(NullProgressReporter):
        def control_request_pending(self):
            return both_started.is_set() and not menu_applied.is_set()

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine = SimpleNamespace(progress=Progress())
    runner.research_desk = object()
    runner.research_max_concurrent_claim_groups = 2
    runner._fast_normalize_claim = lambda *_args: SimpleNamespace(is_researchable=True)

    def retrieve(_module, _round, index, _claim, _normalized):
        with lock:
            starts.add(index)
            if {1, 2} <= starts:
                both_started.set()
        if index == 1:
            assert menu_applied.wait(timeout=3)
        elif index == 2:
            assert release_second.wait(timeout=3)
        else:
            third_started.set()
        return SimpleNamespace(query_trace=[])

    runner._fast_retrieve_claim = retrieve
    runner._fast_read_sources = lambda *_args: ([], [])
    runner._research_claim = lambda _module, _round, index, _claim, **_kwargs: {
        "claim": f"问题 {index}", "status": "PACKET", "packet_id": f"RP-{index}",
    }

    def choose_decrease():
        menu_applied.set()
        return [{"kind": "runtime_control", "control_kind": "research_parallelism",
                 "target": None, "value": 1, "reason": "reduce load"}]

    runner.batch_control_callback = choose_decrease
    monkeypatch.setattr(
        "project_ensemble.runtime.run_controls.record_run_control",
        lambda *_args, **_kwargs: None,
    )

    def observe_then_release():
        assert menu_applied.wait(timeout=3)
        assert both_started.wait(timeout=3)
        assert not third_started.wait(timeout=0.3)
        observations.append(True)
        release_second.set()

    observer = threading.Thread(target=observe_then_release)
    observer.start()
    outcomes = {"RM-01": []}
    runner._run_staged_research_jobs(
        [(module, 1, "问题 1"), (module, 2, "问题 2"), (module, 3, "问题 3")],
        1, outcomes,
    )
    observer.join(timeout=3)
    assert observations == [True]
    assert third_started.is_set()
    assert {item["packet_id"] for item in outcomes["RM-01"]} == {"RP-1", "RP-2", "RP-3"}


def test_ctrl_r_model_change_applies_to_next_unstarted_fast_question(
    tmp_path, monkeypatch,
):
    module = OutlineModule(module_id="RM-01", title="研究问题", research_questions=["问题"],
                           required_evidence=["原文"], source_submission_refs=["WRITER"])
    first_started = threading.Event()
    replacement_done = threading.Event()
    current = {"runtime": ("old", "model")}
    seen = {}

    class Progress(NullProgressReporter):
        def control_request_pending(self):
            return first_started.is_set() and not replacement_done.is_set()

    class ReplacementService:
        def __init__(self, _repo):
            pass

        def replace(self, **_kwargs):
            current["runtime"] = ("new", "model")
            replacement_done.set()

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine = SimpleNamespace(progress=Progress())
    runner.research_desk = object()
    runner.research_max_concurrent_claim_groups = 1

    def normalize(_module, _round, index, _claim):
        seen[index] = current["runtime"]
        if index == 1:
            first_started.set()
            assert replacement_done.wait(timeout=3)
        return SimpleNamespace(is_researchable=True)

    runner._fast_normalize_claim = normalize
    runner._fast_retrieve_claim = lambda *_args: SimpleNamespace(query_trace=[])
    runner._fast_read_sources = lambda *_args: ([], [])
    runner._research_claim = lambda _module, _round, index, _claim, **_kwargs: {
        "claim": f"问题 {index}", "status": "PACKET", "packet_id": f"RP-{index}",
    }
    runner.batch_control_callback = lambda: [{
        "kind": "replace", "participant_id": "RESEARCH_DESK",
        "provider_id": "new", "model_id": "model", "reason": "switch now",
    }]
    monkeypatch.setattr(
        "project_ensemble.orchestration.literature_fast.ModelReplacementService",
        ReplacementService,
    )
    monkeypatch.setattr(
        "project_ensemble.orchestration.literature_fast.current_runtime_for",
        lambda *_args: current["runtime"],
    )
    outcomes = {"RM-01": []}
    runner._run_staged_research_jobs(
        [(module, 1, "问题 1"), (module, 2, "问题 2")], 1, outcomes,
    )
    assert seen == {1: ("old", "model"), 2: ("new", "model")}


def test_fast_science_review_hides_individual_submissions_until_group_freezes(tmp_path):
    class Docs:
        def write_once(self, relative, content):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(content, encoding="utf-8")

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path, docs=Docs())
    runner.active = [
        {"representative_id": rid,
         "runtime": {"persona": Persona.LIBRARIAN.value, "provider_id": provider, "model_id": "m"}}
        for rid, provider in (("R-A", "a"), ("R-B", "b"))
    ]
    calls = []

    def reviewer(participant_id, *, stage, schema, system, user):
        calls.append(participant_id)
        assert not (tmp_path / "public/literature_report/fast/RM-01/science_review_v1.json").exists()
        return ScienceChecklist(issues=[], glossary_corrections=[])

    runner._invoke_service = reviewer
    dossier = tmp_path / "dossier.json"
    dossier.write_text('{"module_id":"RM-01","packets":[]}', encoding="utf-8")
    chapter = WriterChapter(draft=ModuleDraft(
        title="标题", body_markdown="科学正文", short_summary="小结",
    ))
    module = SimpleNamespace(module_id="RM-01", model_dump=lambda **_: {"module_id": "RM-01"})
    group = runner._science_review(module, 1, chapter, dossier)
    assert calls == ["R-A"]
    assert len(json.loads(group.read_text())["reviews"]) == 1
    assert (tmp_path / "governance_private/literature_report/fast/RM-01/science_v1_R-A.json").is_file()
    assert not (tmp_path / "public/literature_report/fast/RM-01/science_v1_R-A.json").exists()


def test_final_fast_science_issue_is_superseded_with_local_repair_choice(tmp_path):
    from project_ensemble.storage.documents import ImmutableDocumentStore

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )
    service = HumanConsultationService(runner.repo)
    old_id = "HC-FAST-SCIENCE-RM-05-FINAL"
    service.open_issue(HumanConsultationIssue(
        issue_id=old_id, meeting_id="LR-TEST", reason_code="FAST_SCIENCE_REVIEW_HUMAN_REQUIRED",
        stage="FAST_SCIENCE_REVIEW", question="Old final choice",
        options=["ACCEPT_WITH_DISCLOSED_LIMITATION", "KEEP_PAUSED"],
        context={"module_id": "RM-05", "recheck_path": "public/recheck.json"},
    ))
    original_bytes = (tmp_path / "human_private/consultations" / f"{old_id}.issue.json").read_bytes()
    module = SimpleNamespace(module_id="RM-05")
    with pytest.raises(LiteratureWritingPaused):
        runner._consult_local_science_repair(module, 3, tmp_path / "public/recheck.json")
    successor = service.open_issues()
    assert len(successor) == 1
    assert successor[0].issue_id == "HC-FAST-SCIENCE-RM-05-LOCAL-V3"
    assert successor[0].options[0] == "RETRY_WRITER_LOCAL_REPAIR"
    assert (tmp_path / "human_private/consultations" / f"{old_id}.issue.json").read_bytes() == original_bytes
    assert (tmp_path / "human_private/consultations" / f"{old_id}.superseded.json").is_file()


def test_fast_local_science_repair_changes_only_exact_selected_passages(tmp_path):
    from project_ensemble.storage.documents import ImmutableDocumentStore

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )
    runner.engine = SimpleNamespace(
        progress=NullProgressReporter(), status=SimpleNamespace(phase=None),
    )
    runner._v071_progress = (1, 1)
    runner._validate_citations = lambda packet_ids: None
    module = OutlineModule(
        module_id="RM-05", title="Regulation", research_questions=["What is supported?"],
        required_evidence=["Primary sources"], source_submission_refs=["S-1"],
    )
    catalog = (tmp_path / "public/literature_report/modules/RM-05/research/"
               "chapter_citation_catalog.json")
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"sources": [{"citation_id": "C5-1", "packet_ids": ["RP-1"]}]}))
    recheck = tmp_path / "public/literature_report/fast/RM-05/science_recheck_v3.json"
    recheck.parent.mkdir(parents=True)
    recheck.write_text(json.dumps({"votes": [{"remaining_material_problems": [
        "The publication year is wrong.", "The conclusion overstates the source.",
    ]}]}))
    original = WriterChapter(draft=ModuleDraft(
        title="Regulation", body_markdown="A 2021 report proves approval [C5-1].\n\nUnaffected paragraph.",
        short_summary="Summary.", cited_packet_ids=["RP-1"],
    ))
    runner._invoke_service = lambda *_args, **_kwargs: FastLocalScienceRepair(edits=[{
        "old_text": "A 2021 report proves approval",
        "new_text": "A report mentions approval without establishing its date or legal basis",
        "objection_numbers": [1, 2],
    }])
    draft_path, repaired = runner._local_science_repair(module, 4, original, recheck)
    assert "Unaffected paragraph." in repaired.draft.body_markdown
    assert "2021 report proves" not in repaired.draft.body_markdown
    assert original.draft.body_markdown == "A 2021 report proves approval [C5-1].\n\nUnaffected paragraph."
    assert draft_path.is_file()
    assert runner._local_science_repair(module, 4, original, recheck)[1] == repaired


def test_fast_local_science_repair_can_add_a_new_glossary_entry(tmp_path):
    from project_ensemble.storage.documents import ImmutableDocumentStore

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )
    runner.engine = SimpleNamespace(
        progress=NullProgressReporter(), status=SimpleNamespace(phase=None),
    )
    runner._v071_progress = (1, 1)
    runner._validate_citations = lambda packet_ids: None
    module = OutlineModule(
        module_id="RM-05", title="Interface scaling", research_questions=["What is supported?"],
        required_evidence=["Primary sources"], source_submission_refs=["S-1"],
    )
    catalog = (tmp_path / "public/literature_report/modules/RM-05/research/"
               "chapter_citation_catalog.json")
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"sources": [{"citation_id": "C5-1", "packet_ids": ["RP-1"]}]}))
    recheck = tmp_path / "public/literature_report/fast/RM-05/science_recheck_v3.json"
    recheck.parent.mkdir(parents=True)
    recheck.write_text(json.dumps({"votes": [{"remaining_material_problems": [
        "术语表修订意见：补入‘亚临界大体系窗口’的定义。",
    ]}]}))
    original = WriterChapter(draft=ModuleDraft(
        title="Interface scaling",
        body_markdown="该分析关注亚临界大体系窗口中的界面涨落。",
        short_summary="比较尺度效应。",
    ))
    runner._invoke_service = lambda *_args, **_kwargs: FastLocalScienceRepair(
        glossary_additions=[{
            "entry": {
                "term": "亚临界大体系窗口",
                "explanation_mode": "NATURAL_LANGUAGE",
                "explanation": "温度低于临界点且体系足够大、可容纳一维界面的模拟条件。",
                "source_citation_ids": ["C5-1"],
            },
            "objection_numbers": [1],
        }],
    )

    draft_path, repaired = runner._local_science_repair(module, 4, original, recheck)

    assert repaired.draft.body_markdown == original.draft.body_markdown
    assert len(repaired.glossary_additions) == 1
    assert repaired.glossary_additions[0].term == "亚临界大体系窗口"
    assert repaired.glossary_additions[0].source_citation_ids == ["C5-1"]
    assert draft_path.is_file()


def test_fast_local_science_repair_uses_paragraph_numbers_for_duplicate_text_and_fresh_cycle(tmp_path):
    from project_ensemble.storage.documents import ImmutableDocumentStore

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )
    runner.engine = SimpleNamespace(
        progress=NullProgressReporter(), status=SimpleNamespace(phase=None),
    )
    runner._v071_progress = (1, 1)
    runner._validate_citations = lambda packet_ids: None
    module = OutlineModule(
        module_id="RM-08", title="Interface scaling", research_questions=["What is supported?"],
        required_evidence=["Primary sources"], source_submission_refs=["S-1"],
    )
    catalog = (tmp_path / "public/literature_report/modules/RM-08/research/"
               "chapter_citation_catalog.json")
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"sources": []}))
    recheck = tmp_path / "public/literature_report/fast/RM-08/science_recheck_v2.json"
    recheck.parent.mkdir(parents=True)
    recheck.write_text(json.dumps({"votes": [{"remaining_material_problems": [
        "The second repeated paragraph needs correction.",
    ]}]}))
    original = WriterChapter(draft=ModuleDraft(
        title="Interface scaling",
        body_markdown="Shared sentence.\n\nShared sentence.",
        short_summary="Two paragraphs repeat the same sentence.",
    ))
    previous_attempt = tmp_path / "public/literature_report/modules/RM-08/writing_v071/"
    previous_attempt.mkdir(parents=True)
    (previous_attempt / "writer_v3_local_patch_c03_a03.json").write_text("{}")
    called_stages = []

    def invoke(_participant_id, *, stage, **_kwargs):
        called_stages.append(stage)
        return FastLocalScienceRepair(edits=[{
            "paragraph_number": 2,
            "new_text": "The second paragraph now states the qualified result.",
            "objection_numbers": [1],
        }])

    runner._invoke_service = invoke
    _draft_path, repaired = runner._local_science_repair(module, 3, original, recheck)

    assert repaired.draft.body_markdown == (
        "Shared sentence.\n\nThe second paragraph now states the qualified result."
    )
    assert called_stages == ["fast_local_science_repair_RM-08_v3_c4_a1"]


def test_fast_local_science_repair_can_edit_a_glossary_entry_by_term(tmp_path):
    from project_ensemble.storage.documents import ImmutableDocumentStore

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )
    runner.engine = SimpleNamespace(
        progress=NullProgressReporter(), status=SimpleNamespace(phase=None),
    )
    runner._v071_progress = (1, 1)
    runner._validate_citations = lambda packet_ids: None
    module = OutlineModule(
        module_id="RM-08", title="Interface scaling", research_questions=["What is supported?"],
        required_evidence=["Primary sources"], source_submission_refs=["S-1"],
    )
    catalog = (tmp_path / "public/literature_report/modules/RM-08/research/"
               "chapter_citation_catalog.json")
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"sources": []}))
    recheck = tmp_path / "public/literature_report/fast/RM-08/science_recheck_v2.json"
    recheck.parent.mkdir(parents=True)
    recheck.write_text(json.dumps({"votes": [{"remaining_material_problems": [
        "Clarify the Gibbs dividing line term in the glossary.",
    ]}]}))
    original = WriterChapter(
        draft=ModuleDraft(
            title="Interface scaling", body_markdown="The line is defined locally.",
            short_summary="Definitions are compared.",
        ),
        glossary_additions=[{
            "term": "Gibbs dividing line",
            "explanation_mode": "NATURAL_LANGUAGE",
            "explanation": "An interface convention.",
        }],
    )
    runner._invoke_service = lambda *_args, **_kwargs: FastLocalScienceRepair(
        glossary_edits=[{
            "term": "Gibbs dividing line",
            "field": "explanation",
            "new_text": "A chosen line used to divide the two coexisting phases; its position is conventional.",
            "objection_numbers": [1],
        }],
    )

    _draft_path, repaired = runner._local_science_repair(module, 3, original, recheck)

    assert repaired.glossary_additions[0].explanation.startswith("A chosen line")


def test_fast_local_science_repair_hides_legacy_packet_ids_and_rebuilds_citations(tmp_path):
    from project_ensemble.storage.documents import ImmutableDocumentStore

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )
    runner.engine = SimpleNamespace(
        progress=NullProgressReporter(), status=SimpleNamespace(phase=None),
    )
    runner._v071_progress = (1, 1)
    runner._validate_citations = lambda _packet_ids: None
    module = OutlineModule(
        module_id="RM-08", title="Interface scaling", research_questions=["What is supported?"],
        required_evidence=["Primary sources"], source_submission_refs=["S-1"],
    )
    catalog = (tmp_path / "public/literature_report/modules/RM-08/research/"
               "chapter_citation_catalog.json")
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"sources": [{
        "citation_id": "C8-1", "packet_ids": ["RP-OLD"],
        "title": "A source title", "authors": ["A. Author"],
        "publication_year": 1991, "doi": "10.1000/example",
    }]}), encoding="utf-8")
    recheck = tmp_path / "public/literature_report/fast/RM-08/science_recheck_v2.json"
    recheck.parent.mkdir(parents=True)
    recheck.write_text(json.dumps({"votes": [{"remaining_material_problems": [
        "Replace [RP-OLD] with the reader-facing citation for the 1991 source.",
    ]}]}), encoding="utf-8")
    original = WriterChapter(draft=ModuleDraft(
        title="Interface scaling",
        body_markdown="The 1991 study reports the result [RP-OLD].",
        short_summary="The result is reported in [RP-OLD].",
        cited_packet_ids=["RP-OLD"],
    ))
    captured_users = []

    def invoke(_participant_id, *, user, **_kwargs):
        captured_users.append(user)
        return FastLocalScienceRepair(edits=[
            {
                "paragraph_number": 1,
                "new_text": "The 1991 study reports the result [C8-1].",
                "objection_numbers": [1],
            },
            {
                "target_field": "short_summary", "replace_entire_field": True,
                "new_text": "The 1991 study reports the result [C8-1].",
                "objection_numbers": [1],
            },
        ])

    runner._invoke_service = invoke
    _draft_path, repaired = runner._local_science_repair(module, 3, original, recheck)

    prompt_payload = json.dumps(captured_users[0], ensure_ascii=False)
    assert "RP-OLD" not in prompt_payload
    assert "[来源待核]" in prompt_payload
    assert "[C8-1]" in repaired.draft.body_markdown
    assert "[C8-1]" in repaired.draft.short_summary
    assert repaired.draft.cited_packet_ids == ["RP-OLD"]


def test_fast_local_science_objections_can_be_built_from_first_review(tmp_path):
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    review = tmp_path / "public/literature_report/fast/RM-01/science_review_v1.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps({"reviews": [{"checklist": {
        "issues": [{
            "location_excerpt": "Claim under review.",
            "questioned_claim": "The claim is too strong.",
            "why_it_matters": "The source supports only an association.",
            "suggested_response": "Qualify the wording.",
        }],
        "glossary_corrections": ["Clarify the specialized term."],
    }}]}), encoding="utf-8")
    objections = runner._local_science_objections(review)
    assert len(objections) == 2
    assert "Claim under review." in objections[0]
    assert "supports only an association" in objections[0]
    assert objections[1] == "术语表修订意见：Clarify the specialized term."
