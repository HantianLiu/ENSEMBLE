import io
import json
from types import SimpleNamespace

import pytest

from project_ensemble import cli
from project_ensemble.errors import RepresentativeUnavailableError
from project_ensemble.orchestration.consultations import HumanConsultationIssue, HumanConsultationService
from project_ensemble.runtime.fast_science_delegation import (
    AIScienceDecision, AUTOMATIC_SCIENCE_RETRY_LIMIT_EFFECT,
    MAX_STANDING_AI_RETRIES_PER_MODULE, can_delegate_fast_science, delegate_fast_science,
)
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.runtime.ai_delegation_settings import (
    available_delegations, delegation_setting, set_delegation_settings,
)
from project_ensemble.runtime.fast_science_delegation import try_automatic_fast_science


@pytest.fixture
def consultation(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")], chair_model=("fake", "m"),
        governance_docs="docs/governance", task_description="科学代裁测试",
    )
    service = HumanConsultationService(repo)
    review_path = "public/literature_report/fast/RM-02/science_recheck_v2.json"
    repo.docs.write_once(review_path, json.dumps({"votes": [{
        "remaining_material_problems": ["公式应声明定义域", "术语应解释变量"],
    }]}))
    repo.docs.write_once(
        "public/literature_report/modules/RM-02/writing_v071/writer_v2_validated.json",
        json.dumps({"draft": {"title": "方法导引", "body_markdown": "公式说明。",
                              "short_summary": "概要。"}}),
    )
    issue = HumanConsultationIssue(
        issue_id="HC-FAST-SCIENCE-RM-02-LOCAL-V3", meeting_id=repo.meeting_id,
        reason_code="FAST_SCIENCE_REVIEW_HUMAN_REQUIRED", stage="FAST_SCIENCE_REVIEW",
        question="当前稿仍有科学异议",
        options=["RETRY_WRITER_LOCAL_REPAIR", "REWRITE_WHOLE_MODULE",
                 "ACCEPT_WITH_DISCLOSED_LIMITATION", "KEEP_PAUSED"],
        context={"module_id": "RM-02", "recheck_path": review_path},
    )
    service.open_issue(issue)
    return repo, service, issue


def decision(action="RETRY_WRITER_LOCAL_REPAIR", numbers=(1, 2)):
    return AIScienceDecision(
        decision=action, rationale="局部补充定义域与术语说明即可。",
        objections=[{"objection_number": number, "treatment": "REVISE",
                     "rationale": f"第 {number} 条应补充适用条件。"} for number in numbers],
    )


def engine_with(result):
    calls = []

    def invoke(participant, **kwargs):
        calls.append((participant, kwargs))
        return SimpleNamespace(text="{}")

    return SimpleNamespace(
        find_recorded_response=lambda *args, **kwargs: None,
        invoke_participant=invoke,
        validate_structured_response=lambda *args, **kwargs: result,
    ), calls


def test_ai_action_is_available_without_automatic_authorization_or_call(consultation):
    repo, service, issue = consultation
    source = repo.root / "human_private/consultations" / f"{issue.issue_id}.issue.json"
    old_bytes = source.read_bytes()
    engine, calls = engine_with(decision())
    output = io.StringIO()
    assert not cli._prompt_for_one_consultation(repo, input_fn=lambda _: "", output=output, engine=engine)
    assert "5. 交给 AI 代裁" in output.getvalue()
    assert calls == []
    assert not list((repo.root / "human_private/consultations").glob("*.ai_authorization.json"))
    assert service.resolution(issue.issue_id) is None
    assert source.read_bytes() == old_bytes


def test_selecting_ai_records_one_issue_authority_and_next_revision_action(consultation):
    repo, service, issue = consultation
    engine, calls = engine_with(decision())
    answers = iter(["5", "1"])
    assert cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers),
                                           output=io.StringIO(), engine=engine)
    resolution = service.resolution(issue.issue_id)
    assert resolution.authority == "DELEGATED_AI"
    assert resolution.decision == "RETRY_WRITER_LOCAL_REPAIR"
    assert resolution.scope == "THIS_CONSULTATION_ONLY"
    assert "异议 1" in resolution.rationale and "异议 2" in resolution.rationale
    authorization = json.loads((repo.root / resolution.authorization_record_path).read_text())
    assert authorization["issue_id"] == issue.issue_id
    assert authorization["science_recheck_required"] is True
    assert "ACCEPT_WITH_DISCLOSED_LIMITATION" not in authorization["allowed_decisions"]
    assert len(calls) == 1 and calls[0][0] != "WRITER"
    context = json.loads(calls[0][1]["user_text"].split("\nTARGET JSON SCHEMA:")[0])
    assert len(context["numbered_objections"]) == 2
    assert context["current_chapter"]["draft"]["body_markdown"] == "公式说明。"
    assert "runtime" not in context and "representative_registry" not in context
    public_resolution = json.loads((repo.root / "public/procedural_consultations" /
                                    f"{issue.issue_id}.resolution.json").read_text())
    assert public_resolution["authority"] == "DELEGATED_AI"
    assert "ai_rationale" in public_resolution
    assert repo.events.verify()
    assert delegate_fast_science(repo, issue, engine=engine) == resolution
    assert len(calls) == 1


def test_ai_failure_returns_to_manual_menu_and_does_not_resolve_consultation(consultation):
    repo, service, issue = consultation
    engine, _ = engine_with(decision())
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise RepresentativeUnavailableError("代裁模型暂时不可用")

    engine.invoke_participant = unavailable
    answers = iter(["5", "1", "1", ""])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers), output=output, engine=engine)
    resolution = service.resolution(issue.issue_id)
    assert resolution.authority == "HUMAN"
    assert resolution.decision == "RETRY_WRITER_LOCAL_REPAIR"
    assert "可在下面手动选择" in output.getvalue()
    assert len(calls) == 1


@pytest.mark.parametrize("numbers", [(1,), (1, 1), (1, 3)])
def test_ai_must_address_each_objection_once_before_resolution(consultation, numbers):
    repo, service, issue = consultation
    engine, _ = engine_with(decision(numbers=numbers))
    with pytest.raises(ValueError, match="完整处理"):
        delegate_fast_science(repo, issue, engine=engine)
    assert service.resolution(issue.issue_id) is None
    assert service.open_issues() == [issue]


def test_ai_pause_keeps_original_menu_open_for_human_on_resume(consultation):
    repo, service, issue = consultation
    engine, _ = engine_with(decision("KEEP_PAUSED"))
    answers = iter(["5", "1"])
    assert not cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers), output=io.StringIO(), engine=engine)
    assert service.resolution(issue.issue_id) is None
    assert service.open_issues() == [issue]
    answers = iter(["1", ""])
    assert cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers), output=io.StringIO(), engine=engine)
    assert service.resolution(issue.issue_id).authority == "HUMAN"


def test_delegation_cannot_resolve_an_unrelated_consultation_or_publish_with_limitations(consultation):
    repo, service, issue = consultation
    assert not can_delegate_fast_science(issue.model_copy(update={"stage": "FAST_SCOPE_QUESTION"}))
    assert not can_delegate_fast_science(issue.model_copy(update={"context": {"last_problem": "format"}}))
    with pytest.raises(ValueError, match="authorization"):
        service._resolve(issue_id=issue.issue_id, decision="RETRY_WRITER_LOCAL_REPAIR",
                         rationale="伪造代裁", scope="THIS_CONSULTATION_ONLY",
                         authority="DELEGATED_AI", authorization_record_path="missing.json")
    with pytest.raises(ValueError, match="authorized"):
        service._resolve(issue_id=issue.issue_id, decision="ACCEPT_WITH_DISCLOSED_LIMITATION",
                         rationale="擅自放行", scope="THIS_CONSULTATION_ONLY",
                         authority="DELEGATED_AI", authorization_record_path="missing.json")
    assert service.resolution(issue.issue_id) is None


def test_ai_option_without_engine_leaves_manual_options_usable(consultation):
    repo, service, issue = consultation
    answers = iter(["5", "1", ""])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers), output=output)
    assert "当前入口无法调用 AI" in output.getvalue()
    assert service.resolution(issue.issue_id).authority == "HUMAN"
    assert not list((repo.root / "human_private/consultations").glob("*.ai_authorization.json"))


def next_issue(repo, service, issue):
    successor = issue.model_copy(update={"issue_id": "HC-FAST-SCIENCE-RM-02-LOCAL-V4"})
    service.open_issue(successor)
    return successor


def test_this_once_does_not_authorize_next_issue_and_shows_model(consultation):
    repo, service, issue = consultation
    engine, calls = engine_with(decision())
    answers = iter(["5", ""])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers), output=output, engine=engine)
    successor = next_issue(repo, service, issue)
    assert "当前代裁模型：fake:m" in output.getvalue()
    assert not delegation_setting(repo, "fast_science")[0]
    assert try_automatic_fast_science(repo, successor, engine=engine) is None
    assert len(calls) == 1


def test_meeting_scope_survives_resume_and_automatically_delegates_next_issue(consultation):
    repo, service, issue = consultation
    engine, calls = engine_with(decision())
    source = (repo.root / "human_private/consultations" / f"{issue.issue_id}.issue.json").read_bytes()
    answers = iter(["5", "2"])
    assert cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers), output=io.StringIO(), engine=engine)
    enabled, grant = delegation_setting(MeetingRepository(repo.root), "fast_science")
    assert enabled
    successor = next_issue(repo, service, issue)
    result = try_automatic_fast_science(MeetingRepository(repo.root), successor, engine=engine)
    assert result.authority == "DELEGATED_AI"
    authorization = json.loads((repo.root / result.authorization_record_path).read_text())
    assert authorization["standing_authorization_record_path"] == grant
    assert authorization["science_recheck_required"]
    assert len(calls) == 2
    assert (repo.root / "human_private/consultations" / f"{issue.issue_id}.issue.json").read_bytes() == source
    assert repo.events.verify()


def test_standing_ai_retry_limit_returns_to_human_without_dropping_science(consultation):
    repo, service, issue = consultation
    engine, calls = engine_with(decision())
    set_delegation_settings(repo, {"fast_science": True})

    current = issue
    for version in range(3, 3 + MAX_STANDING_AI_RETRIES_PER_MODULE):
        ruling = try_automatic_fast_science(repo, current, engine=engine)
        assert ruling is not None
        assert ruling.authority == "DELEGATED_AI"
        current = issue.model_copy(update={
            "issue_id": f"HC-FAST-SCIENCE-RM-02-LOCAL-V{version + 1}",
        })
        service.open_issue(current)

    assert try_automatic_fast_science(repo, current, engine=engine) is None
    assert len(calls) == MAX_STANDING_AI_RETRIES_PER_MODULE
    assert service.resolution(current.issue_id) is None
    assert service.open_issues() == [current]
    fallback = json.loads((repo.root / "public/procedural_consultations" /
                           f"{current.issue_id}.ai_automatic_fallback.json").read_text())
    assert fallback["effect"] == AUTOMATIC_SCIENCE_RETRY_LIMIT_EFFECT
    assert "异议" in fallback["reason"] and "当前稿" in fallback["reason"]

    answers = iter(["1", ""])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _: next(answers), output=output, engine=engine,
    )
    assert service.resolution(current.issue_id).authority == "HUMAN"
    assert "异议和当前稿均保留" in output.getvalue()
    assert len(calls) == MAX_STANDING_AI_RETRIES_PER_MODULE


def test_meeting_switches_are_independent_revocable_and_append_only(consultation):
    repo, service, issue = consultation
    assert available_delegations(repo) == ["fast_scope", "fast_science"]
    enabled_path = set_delegation_settings(repo, {"fast_scope": True, "fast_science": True})
    original = (repo.root / enabled_path).read_bytes()
    set_delegation_settings(repo, {"fast_science": False})
    assert delegation_setting(repo, "fast_scope")[0]
    assert not delegation_setting(repo, "fast_science")[0]
    assert (repo.root / enabled_path).read_bytes() == original
    engine, calls = engine_with(decision())
    assert try_automatic_fast_science(repo, issue, engine=engine) is None
    assert calls == []
    assert service.open_issues() == [issue]
    with pytest.raises(ValueError):
        set_delegation_settings(repo, {"all_human_decisions": True})
    with pytest.raises(ValueError):
        set_delegation_settings(repo, {"fast_science": "on"})


@pytest.mark.parametrize("outcome", ["error", "KEEP_PAUSED"])
def test_automatic_failure_or_pause_returns_to_manual_without_resume_loop(consultation, outcome):
    repo, service, issue = consultation
    engine, calls = engine_with(decision("KEEP_PAUSED"))
    if outcome == "error":
        def unavailable(*args, **kwargs):
            calls.append(1)
            raise RepresentativeUnavailableError("暂不可用")
        engine.invoke_participant = unavailable
    set_delegation_settings(repo, {"fast_science": True})
    assert try_automatic_fast_science(repo, issue, engine=engine) is None
    assert try_automatic_fast_science(MeetingRepository(repo.root), issue, engine=engine) is None
    assert len(calls) == 1
    answers = iter(["1", ""])
    assert cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers), output=io.StringIO(), engine=engine)
    assert service.resolution(issue.issue_id).authority == "HUMAN"
    assert len(calls) == 1


def test_revocation_during_ai_call_leaves_science_unresolved(consultation):
    repo, service, issue = consultation
    engine, calls = engine_with(decision())
    original_invoke = engine.invoke_participant
    def revoke(*args, **kwargs):
        set_delegation_settings(repo, {"fast_science": False})
        return original_invoke(*args, **kwargs)
    engine.invoke_participant = revoke
    set_delegation_settings(repo, {"fast_science": True})
    assert try_automatic_fast_science(repo, issue, engine=engine) is None
    assert service.resolution(issue.issue_id) is None
    assert service.open_issues() == [issue]
    assert len(calls) == 1


def test_settings_menu_enables_all_and_toggles_individual_types(consultation):
    repo, service, issue = consultation
    answers = iter(["1", "4", "b"])
    output = io.StringIO()
    cli._interactive_ai_delegation_settings(repo, input_fn=lambda _: next(answers), output=output)
    assert "会议设置 · AI 代裁" in output.getvalue()
    assert "fake:m" in output.getvalue()
    assert delegation_setting(repo, "fast_scope")[0]
    assert not delegation_setting(repo, "fast_science")[0]
    assert service.resolution(issue.issue_id) is None
    answers = iter(["2", "b"])
    cli._interactive_ai_delegation_settings(repo, input_fn=lambda _: next(answers), output=io.StringIO())
    assert not delegation_setting(repo, "fast_scope")[0]


def test_scope_settings_override_legacy_standing_grant(consultation):
    from project_ensemble.runtime.fast_scope_consultation import (
        _save_standing_writer_delegation, _standing_writer_delegation,
    )
    repo, service, issue = consultation
    _save_standing_writer_delegation(repo, issue.issue_id)
    legacy = repo.root / "human_private/consultations/fast_scope_writer_standing_delegation.json"
    original = legacy.read_bytes()
    assert _standing_writer_delegation(repo)
    set_delegation_settings(repo, {"fast_scope": False})
    assert not _standing_writer_delegation(repo)
    _save_standing_writer_delegation(repo, issue.issue_id)
    assert _standing_writer_delegation(repo)
    assert legacy.read_bytes() == original


def test_runner_uses_standing_science_grant_without_terminal_prompt(consultation):
    from project_ensemble.orchestration.literature_fast import FastLiteratureRunner
    repo, service, issue = consultation
    engine, calls = engine_with(decision())
    runner = object.__new__(FastLiteratureRunner)
    runner.repo, runner.engine, runner.max_output_tokens = repo, engine, None
    set_delegation_settings(repo, {"fast_science": True})
    result = runner._consult(issue.issue_id, issue.stage, issue.question, issue.options, issue.context)
    assert result.authority == "DELEGATED_AI"
    assert len(calls) == 1


def test_revoked_standing_grant_cannot_be_committed_but_explicit_once_can_reauthorize(consultation):
    repo, service, issue = consultation
    engine, _ = engine_with(decision(numbers=(1,)))
    grant = set_delegation_settings(repo, {"fast_science": True})
    with pytest.raises(ValueError):
        delegate_fast_science(repo, issue, engine=engine, standing_authorization_path=grant)
    original_path = f"human_private/consultations/{issue.issue_id}.ai_authorization.json"
    original = (repo.root / original_path).read_bytes()
    set_delegation_settings(repo, {"fast_science": False})
    with pytest.raises(ValueError, match="revoked"):
        service._resolve(issue_id=issue.issue_id, decision="RETRY_WRITER_LOCAL_REPAIR",
                         rationale="过期授权", scope="THIS_CONSULTATION_ONLY", authority="DELEGATED_AI",
                         authorization_record_path=original_path)
    engine, _ = engine_with(decision())
    ruling = delegate_fast_science(repo, issue, engine=engine)
    assert ruling.authority == "DELEGATED_AI"
    assert ruling.authorization_record_path != original_path
    assert (repo.root / original_path).read_bytes() == original
    assert not delegation_setting(repo, "fast_science")[0]


def scope_issue(repo, service):
    issue = HumanConsultationIssue(
        issue_id="HC-FAST-SCOPE-RM-02", meeting_id=repo.meeting_id,
        stage="FAST_SCOPE_QUESTION", reason_code="FAST_SCOPE_QUESTION_HUMAN_REQUIRED",
        question="本模块范围问题", options=["KEEP_APPROVED_SCOPE", "APPROVE_SCOPE_CHANGE"],
        context={"module_id": "RM-02", "questions": ["是否需要扩大范围？"]},
    )
    service.open_issue(issue)
    return issue


def test_scope_meeting_setting_uses_existing_protocol_without_human_input(consultation):
    from project_ensemble.runtime.fast_scope_consultation import WriterScopeRuling, try_automatic_fast_scope
    repo, service, _ = consultation
    issue = scope_issue(repo, service)
    engine, calls = engine_with(WriterScopeRuling(decision="KEEP", rationale="当前范围足以回答问题"))
    set_delegation_settings(repo, {"fast_scope": True})
    ruling = try_automatic_fast_scope(repo, issue, engine=engine)
    assert ruling.decision == "KEEP_APPROVED_SCOPE"
    assert len(calls) == 1
    assert not delegation_setting(repo, "fast_science")[0]


def test_scope_automatic_failure_returns_manual_without_repeating_model_calls(consultation):
    from project_ensemble.runtime.fast_scope_consultation import try_automatic_fast_scope, prompt_fast_scope_consultation
    repo, service, _ = consultation
    issue = scope_issue(repo, service)
    engine, calls = engine_with(None)
    def unavailable(*args, **kwargs):
        calls.append(1)
        raise RepresentativeUnavailableError("暂不可用")
    engine.invoke_participant = unavailable
    set_delegation_settings(repo, {"fast_scope": True})
    assert try_automatic_fast_scope(repo, issue, engine=engine) is None
    assert try_automatic_fast_scope(repo, issue, engine=engine) is None
    answers = iter(["2", "1"])
    assert prompt_fast_scope_consultation(repo, issue, input_fn=lambda _: next(answers),
                                          output=io.StringIO(), engine=engine)
    # The manual recovery must not call the failed delegate again just to show the menu.
    assert len(calls) == 1


def test_initial_meeting_delegation_failure_does_not_auto_retry_on_resume(consultation):
    repo, service, issue = consultation
    engine, calls = engine_with(None)
    def unavailable(*args, **kwargs):
        calls.append(1)
        raise RepresentativeUnavailableError("暂不可用")
    engine.invoke_participant = unavailable
    answers = iter(["5", "2", ""])
    assert not cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers),
                                               output=io.StringIO(), engine=engine)
    assert delegation_setting(repo, "fast_science")[0]
    answers = iter(["1", ""])
    assert cli._prompt_for_one_consultation(repo, input_fn=lambda _: next(answers), output=io.StringIO(), engine=engine)
    assert len(calls) == 1
    assert service.resolution(issue.issue_id).authority == "HUMAN"


def test_runtime_meeting_settings_exposes_ai_switches(consultation, monkeypatch, capsys):
    repo, service, issue = consultation
    answers = iter(["8", "1", "b", "b"])
    monkeypatch.setattr(cli, "terminal_input", lambda _: next(answers))
    # Defaults binding in the nested menu is explicitly supplied below.
    original = cli._interactive_ai_delegation_settings
    monkeypatch.setattr(cli, "_interactive_ai_delegation_settings",
                        lambda repo, **kwargs: original(repo, input_fn=lambda _: next(answers), **kwargs))
    assert cli._interactive_model_replacement(repo=repo, cfg=None) is None
    assert delegation_setting(repo, "fast_science")[0]
    assert delegation_setting(repo, "fast_scope")[0]
    assert "AI 代裁" in capsys.readouterr().err


def test_ai_settings_cli_resolves_direct_meeting_path(consultation, monkeypatch):
    repo, service, issue = consultation
    inspected = []
    monkeypatch.setattr(cli, "_interactive_ai_delegation_settings", lambda repo: inspected.append(repo.root))
    monkeypatch.setattr(cli.sys, "argv", ["ensemble", "ai-delegation", "--meeting", str(repo.root)])
    assert cli.main() == 0
    assert inspected == [repo.root]
