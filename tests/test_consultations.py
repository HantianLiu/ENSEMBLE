import json
from types import SimpleNamespace

import pytest

from project_ensemble.orchestration.consultations import (
    ChairDelegatedScienceDecision,
    HumanConsultationIssue,
    HumanConsultationService,
    ScienceConsultationAuthorityService,
    ThresholdRecord,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


def make_repo(tmp_path):
    return MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="task",
    )


def make_rendering_repo(tmp_path, *, default="human"):
    repo = MeetingRepository(tmp_path / "SR-TEST")
    repo.docs.write_once(
        "public/meeting_manifest.json",
        json.dumps({
            "meeting_id": "SR-TEST", "title": "test", "created_at": "2026-01-01T00:00:00Z",
            "meeting_type": "scholarly_rendering", "participant_count": 0,
            "rendering_science_consultation_authority": default,
        }),
    )
    return repo


def test_science_delegation_can_switch_during_meeting_without_rewriting_manifest(tmp_path):
    repo = make_rendering_repo(tmp_path)
    authority = ScienceConsultationAuthorityService(repo)
    assert authority.current() == ("human", "public/meeting_manifest.json")
    first = authority.set_mode("chair")
    assert authority.current() == ("chair", first)
    second = authority.set_mode("human")
    assert authority.current() == ("human", second)
    assert json.loads((repo.root / first).read_text())["authority"] == "chair"
    assert json.loads((repo.root / "public/meeting_manifest.json").read_text())[
        "rendering_science_consultation_authority"
    ] == "human"


def test_meeting_ai_settings_include_and_control_existing_chair_delegation(tmp_path):
    from project_ensemble.runtime.ai_delegation_settings import (
        available_delegations, delegation_setting, set_delegation_settings,
    )
    repo = make_rendering_repo(tmp_path)
    assert available_delegations(repo) == ["scholarly_science"]
    grant = set_delegation_settings(repo, {"scholarly_science": True})
    assert delegation_setting(repo, "scholarly_science") == (True, grant)
    set_delegation_settings(repo, {"scholarly_science": False})
    assert ScienceConsultationAuthorityService(repo).current()[0] == "human"
    assert json.loads((repo.root / grant).read_text())["authority"] == "chair"


def test_science_authority_cli_switches_by_direct_path(tmp_path, capsys):
    from project_ensemble.cli import cmd_science_authority

    repo = make_rendering_repo(tmp_path)
    assert cmd_science_authority(SimpleNamespace(meeting=str(repo.root), mode="chair", config=None)) == 0
    assert ScienceConsultationAuthorityService(repo).current()[0] == "chair"
    assert "主席代裁" in capsys.readouterr().out


def test_chair_delegation_records_distinct_authority_and_requires_current_permission(tmp_path):
    repo = make_rendering_repo(tmp_path)
    service = HumanConsultationService(repo)
    issue = HumanConsultationIssue(
        issue_id="HC-SR-SR-001-C001-O001", meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW", question="Scientific objection?",
        options=["ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION", "DIRECT_CHAIR_SCIENCE_REVISION"],
    )
    service.open_issue(issue)
    calls = []
    engine = SimpleNamespace(
        find_recorded_response=lambda *args, **kwargs: None,
        invoke_participant=lambda *args, **kwargs: calls.append(kwargs) or SimpleNamespace(text="{}"),
        validate_structured_response=lambda *args, **kwargs: ChairDelegatedScienceDecision(
            decision="REJECT_SCIENCE_OBJECTION", rationale="Current redraw already states the limited claim."
        ),
    )
    assert service.ask_chair_to_decide_science_objection(issue=issue, engine=engine) is None
    assert not calls
    authority = ScienceConsultationAuthorityService(repo)
    authorization_path = authority.set_mode("chair")
    ruling = service.ask_chair_to_decide_science_objection(issue=issue, engine=engine)
    assert ruling is not None
    assert ruling.authority == "DELEGATED_CHAIR"
    assert ruling.authorization_record_path == authorization_path
    assert len(calls) == 1
    assert service.resolution(issue.issue_id) == ruling
    assert repo.events.verify()
    authority.set_mode("human")
    assert service.resolution(issue.issue_id) == ruling


def test_chair_delegation_does_not_apply_to_other_consultations(tmp_path):
    repo = make_rendering_repo(tmp_path, default="chair")
    service = HumanConsultationService(repo)
    issue = HumanConsultationIssue(
        issue_id="HC-SR-SR-001-CITATION-R1", meeting_id=repo.meeting_id,
        reason_code="CITATION_ANCHOR_CONFLICT", stage="SCHOLARLY_CITATION_REVIEW",
        question="Citation question?", options=["A", "B"],
    )
    service.open_issue(issue)
    assert service.ask_chair_to_decide_science_objection(
        issue=issue, engine=SimpleNamespace()
    ) is None
    assert service.resolution(issue.issue_id) is None


def test_human_consultation_records_exact_threshold_and_resolution(tmp_path):
    repo = make_repo(tmp_path)
    service = HumanConsultationService(repo)
    issue = HumanConsultationIssue(
        issue_id="HC-W001-STEP001-CONFLICT",
        meeting_id=repo.meeting_id,
        reason_code="OPERATIVE_AMENDMENT_CONFLICT",
        stage="GENERAL_PRINCIPLE_SEQUENTIAL_AMENDMENT",
        question="Which procedure applies?",
        options=["SEQUENTIAL_BINARY_WITH_CURRENT_TEXT", "DEFER_CURRENT_AMENDMENT"],
        affected_items=["A-1", "A-2"],
        thresholds=[
            ThresholdRecord(
                name="supermajority",
                formula="ceil(3N/4)",
                comparison=">=",
                eligible_count=12,
                required_votes=9,
            )
        ],
    )
    _, created = service.open_issue(issue)
    assert created
    _, created_again = service.open_issue(issue)
    assert not created_again
    resolution = service.resolve(
        issue_id=issue.issue_id,
        decision="DEFER_CURRENT_AMENDMENT",
        rationale="Avoid an underspecified option set.",
        scope="THIS_CONSULTATION_ONLY",
    )
    assert resolution.thresholds[0].required_votes == 9
    assert service.resolution(issue.issue_id) == resolution
    public = json.loads(
        (repo.root / "public/procedural_consultations" / f"{issue.issue_id}.resolution.json").read_text()
    )
    assert public["thresholds"][0]["eligible_count"] == 12
    assert public["thresholds"][0]["required_votes"] == 9
    assert "rationale" not in public
    assert repo.events.verify()


def test_consultation_rejects_unlisted_human_decision(tmp_path):
    repo = make_repo(tmp_path)
    service = HumanConsultationService(repo)
    issue = HumanConsultationIssue(
        issue_id="HC-W001-STEP001-CONFLICT",
        meeting_id=repo.meeting_id,
        reason_code="CONFLICT",
        stage="BALLOT",
        question="Choose.",
        options=["A", "B"],
    )
    service.open_issue(issue)
    with pytest.raises(ValueError, match="decision must be one of"):
        service.resolve(
            issue_id=issue.issue_id,
            decision="UNLISTED",
            rationale="No.",
            scope="THIS_CONSULTATION_ONLY",
        )


def test_human_wording_is_separate_from_the_decision_rationale(tmp_path):
    repo = make_repo(tmp_path)
    service = HumanConsultationService(repo)
    issue = HumanConsultationIssue(
        issue_id="HC-SR-SR-001",
        meeting_id=repo.meeting_id,
        reason_code="SCIENCE_CONFLICT",
        stage="SCHOLARLY_SCIENCE_REVIEW",
        question="Choose the final wording.",
        options=["USE_SOURCE_TEXT", "USE_CURRENT_REDRAW", "USE_HUMAN_WORDING"],
    )
    service.open_issue(issue)
    with pytest.raises(ValueError, match="requires separate final wording"):
        service.resolve(
            issue_id=issue.issue_id,
            decision="USE_HUMAN_WORDING",
            rationale="The alternatives are both ambiguous.",
            scope="THIS_CONSULTATION_ONLY",
        )
    resolution = service.resolve(
        issue_id=issue.issue_id,
        decision="USE_HUMAN_WORDING",
        rationale="The alternatives are both ambiguous.",
        human_wording="The observed association does not establish causation.",
        scope="THIS_CONSULTATION_ONLY",
    )
    assert resolution.rationale != resolution.human_wording


def test_human_can_ask_chair_one_natural_language_question(tmp_path):
    repo = make_repo(tmp_path)
    service = HumanConsultationService(repo)
    issue = HumanConsultationIssue(
        issue_id="HC-W001-STEP001-CONFLICT",
        meeting_id=repo.meeting_id,
        reason_code="CONFLICT",
        stage="BALLOT",
        question="Which procedure applies?",
        options=["A", "B", "REQUEST_CHAIR_RECLASSIFICATION"],
        affected_items=["A-1", "A-2"],
    )
    service.open_issue(issue)

    class Notifier:
        enabled = False

        def send_escalation(self, **kwargs):
            raise AssertionError("notification not expected")

    engine = MeetingEngine(
        repo=repo,
        adapters={
            "fake": ScriptedProviderAdapter(
                "fake",
                ["m"],
                ["这两项目前被登记为程序性互斥，但 Human 可以要求重新检查是否只是内容重叠。"],
            )
        },
        notifier=Notifier(),
    )
    turn = service.ask_chair(
        issue_id=issue.issue_id,
        question="这里的互斥是科学结论矛盾，还是条文位置重叠？",
        engine=engine,
        governance_docs="docs/governance",
        max_output_tokens=512,
    )
    assert turn.turn_number == 1
    assert "内容重叠" in turn.chair_answer
    assert service.dialogue(issue.issue_id) == [turn]
    assert not list((repo.root / "public").rglob("*dialogue*"))
    assert repo.events.verify()


def test_superseded_consultation_is_replaced_without_overwriting_history(tmp_path):
    repo = make_repo(tmp_path)
    service = HumanConsultationService(repo)
    old = HumanConsultationIssue(
        issue_id="HC-W001-STEP001-CONFLICT",
        meeting_id=repo.meeting_id,
        reason_code="CONFLICT",
        stage="BALLOT",
        question="Old question.",
        options=["A", "B"],
    )
    new = old.model_copy(
        update={
            "issue_id": "HC-W001-STEP001-CONFLICT-V2",
            "options": ["A", "B", "REQUEST_CHAIR_RECLASSIFICATION"],
        }
    )
    service.open_issue(old)
    service.supersede(
        issue_id=old.issue_id,
        successor_issue_id=new.issue_id,
        reason="add reclassification option",
    )
    service.open_issue(new)
    assert [item.issue_id for item in service.open_issues()] == [new.issue_id]
    assert (repo.root / "human_private/consultations" / f"{old.issue_id}.issue.json").exists()
    with pytest.raises(ValueError, match="superseded"):
        service.resolve(
            issue_id=old.issue_id,
            decision="A",
            rationale="stale",
            scope="THIS_CONSULTATION_ONLY",
        )
