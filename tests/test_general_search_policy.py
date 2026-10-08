import io
import json
import sys

import pytest

from project_ensemble import cli
from project_ensemble.config import EnsembleConfig, ProviderConfig, TavilyResearchConfig
from project_ensemble.domain import ModelDescriptor, ReasoningEffort
from project_ensemble.errors import TransientProviderError
from project_ensemble.research.openalex import OpenAlexRetriever
from project_ensemble.research.retrievers import CompositeRetriever, PolicyResearchRetriever, ResearchRetrievalResult, TavilyRetriever
from project_ensemble.research.search_policy import general_search_allowed
from project_ensemble.research.source_reading import SourceReader
from project_ensemble.runtime.run_controls import effective_openalex_quota_policy, record_run_control
from project_ensemble.startup import TerminalWizard, start_meeting
from project_ensemble.storage.meeting import MeetingRepository


def _config():
    return EnsembleConfig(providers={
        "fake": ProviderConfig(kind="openai_compatible", base_url="https://unused.invalid", api_key_env="UNUSED_TEST_KEY"),
    })


def _meeting(tmp_path, allowed=True):
    governance = tmp_path / "rules"
    governance.mkdir(exist_ok=True)
    (governance / "rule.md").write_text("fixture rule", encoding="utf-8")
    return MeetingRepository.create(
        tmp_path / ("allowed" if allowed else "denied"),
        selected_models=[("fake", "m")], chair_model=("fake", "m"),
        governance_docs=governance, research_enabled=True,
        task_description="fixture research task",
        research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        general_search_allowed=allowed, openalex_quota_policy="wait",
    )


@pytest.mark.parametrize("allowed", [True, False])
def test_permission_is_frozen_publicly_privately_and_on_resume(tmp_path, allowed):
    repo = _meeting(tmp_path, allowed)
    original = {}
    for name in ("public/meeting_manifest.json", "identity_private/meeting_manifest.json"):
        path = repo.root / name
        original[name] = path.read_bytes()
        assert json.loads(original[name])["general_search_allowed"] is allowed
    assert general_search_allowed(MeetingRepository(repo.root)) is allowed
    assert '"general_search_allowed":' in repo.events.path.read_text()
    assert repo.events.verify()
    assert all((repo.root / name).read_bytes() == value for name, value in original.items())


@pytest.mark.parametrize("outcome", ["empty", "connection_failure", "daily_quota"])
def test_denied_meeting_never_reads_tavily_key_or_builds_backend(tmp_path, monkeypatch, outcome):
    repo = _meeting(tmp_path, False)
    cfg = _config()
    cfg.research.tavily.enabled = True
    monkeypatch.setattr(TavilyResearchConfig, "api_key", lambda _self: pytest.fail("must not read Tavily credentials"))
    monkeypatch.setattr(TavilyRetriever, "__init__", lambda *_a, **_k: pytest.fail("must not build Tavily"))
    calls = []

    def retrieve(self, query):
        calls.append(query)
        if outcome != "empty":
            from project_ensemble.research.openalex import OpenAlexDailyQuotaExhausted
            error = (OpenAlexDailyQuotaExhausted("daily quota", reset_seconds=1) if outcome == "daily_quota"
                     else TransientProviderError("connection failure"))
            raise error
        return ResearchRetrievalResult([], [], ("openalex",))

    monkeypatch.setattr(OpenAlexRetriever, "retrieve_exploratory", retrieve)
    retriever = cli._build_research_retriever(cfg, MeetingRepository(repo.root))
    assert retriever.backend_ids == ("openalex",)
    if outcome == "empty":
        assert retriever.retrieve_exploratory("academic question").candidates == []
    else:
        with pytest.raises(TransientProviderError):
            retriever.retrieve_exploratory("academic question")
    assert calls == ["academic question"]
    assert cfg.research.tavily.enabled is True


def test_permission_does_not_change_other_meetings_or_global_config(tmp_path, monkeypatch):
    denied = _meeting(tmp_path, False)
    allowed = _meeting(tmp_path, True)
    cfg = _config()
    cfg.research.tavily.enabled = True
    monkeypatch.setattr(TavilyResearchConfig, "api_key", lambda _self: "fixture-key")
    original = cfg.model_dump()
    assert isinstance(cli._build_research_retriever(cfg, denied), OpenAlexRetriever)
    other = cli._build_research_retriever(cfg, allowed)
    assert isinstance(other, PolicyResearchRetriever)
    assert isinstance(other.tavily, TavilyRetriever)
    assert cfg.model_dump() == original


def test_legacy_meeting_without_permission_keeps_existing_behavior(tmp_path):
    (tmp_path / "identity_private").mkdir()
    (tmp_path / "identity_private/meeting_manifest.json").write_text("{}", encoding="utf-8")
    assert general_search_allowed(MeetingRepository(tmp_path)) is True
    assert general_search_allowed(None) is True


@pytest.mark.parametrize("private,public", [(False, True), ("false", False), (None, False)])
def test_corrupt_or_conflicting_permission_fails_closed(tmp_path, private, public):
    for folder, value in (("identity_private", private), ("public", public)):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "meeting_manifest.json").write_text(
            json.dumps({"general_search_allowed": value}), encoding="utf-8",
        )
    with pytest.raises(ValueError, match="拒绝调用通用搜索"):
        general_search_allowed(MeetingRepository(tmp_path))


def test_source_reading_cannot_use_injected_tavily_for_extract_or_search(tmp_path):
    repo = _meeting(tmp_path, False)
    academic = OpenAlexRetriever()
    injected = TavilyRetriever(api_key="fixture-key")
    reader = SourceReader(
        repo=repo, retriever=CompositeRetriever([academic, injected]),
    )
    assert reader.tavily is None
    assert reader.search_backend is academic


def test_quota_override_cannot_reenable_general_search(tmp_path):
    repo = _meeting(tmp_path, False)
    original_events = repo.events.path.read_bytes()
    with pytest.raises(ValueError, match="禁止通用搜索"):
        record_run_control(repo, kind="openalex_quota_policy", target=None, value="tavily")
    assert not (repo.root / "human_private/runtime_controls").exists()
    assert repo.events.path.read_bytes() == original_events
    assert effective_openalex_quota_policy(repo, "tavily") == "wait"
    assert effective_openalex_quota_policy(repo, "legacy_parallel") == "wait"


def test_old_runtime_fallback_record_cannot_override_frozen_denial(tmp_path):
    repo = _meeting(tmp_path, False)
    # Simulate an incompatible imported old control, not a permitted new write.
    folder = repo.root / "human_private/runtime_controls"
    folder.mkdir()
    (folder / "control-000001.json").write_text(
        json.dumps({"kind": "openalex_quota_policy", "value": "tavily"}), encoding="utf-8",
    )
    assert effective_openalex_quota_policy(repo, "wait") == "wait"


def test_ctrl_r_does_not_offer_tavily_in_denied_meeting(tmp_path, monkeypatch):
    repo = _meeting(tmp_path, False)
    cfg = _config()
    cfg.research.tavily.enabled = True
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: "b")
    output = io.StringIO()
    assert cli._interactive_run_control(repo=repo, cfg=cfg, mode=6, deferred=True, output=output) is None
    assert "当前禁止通用搜索" in output.getvalue()
    assert "2. 限流时先用 Tavily" not in output.getvalue()


@pytest.mark.parametrize("language,answer,allowed", [("zh", "2", False), ("en", "1", True), ("zh", "", True)])
def test_initialization_menu_is_meeting_local_and_bilingual(language, answer, allowed):
    output = io.StringIO()
    prompts = []
    wizard = TerminalWizard(input_fn=lambda prompt: prompts.append(prompt) or answer, output=output)
    wizard.language = language
    cfg = _config()
    original = cfg.model_dump()
    assert wizard._choose_general_search(cfg) is allowed
    assert cfg.model_dump() == original
    assert "Tavily" in output.getvalue()
    assert ("本会议" if language == "zh" else "this meeting") in output.getvalue()
    assert ("通用网页搜索" if language == "zh" else "General web search") in prompts[0]


def test_initialization_menu_retries_invalid_input():
    answers = iter(["invalid", "2"])
    output = io.StringIO()
    wizard = TerminalWizard(input_fn=lambda _prompt: next(answers), output=output)
    wizard.language = "zh"
    assert wizard._choose_general_search(_config()) is False
    assert "请输入 1 或 2" in output.getvalue()


def test_wizard_denial_skips_fallback_menu_and_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda *_a: [ModelDescriptor(provider_id="fake", model_id="m")],
    )
    cfg = _config()
    cfg.research.tavily.enabled = True
    answers = iter(["4", "1", "1", "1", "", "可核查的事实主张。", "", "1", "y"])
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        if prompt.startswith("通用网页搜索"):
            return "2"
        if prompt.startswith(("每个模型最多同时调用多少次", "学术搜索引擎")):
            return ""
        return next(answers)

    output = io.StringIO()
    selection = TerminalWizard(input_fn=answer, output=output).collect(cfg)
    assert selection.general_search_allowed is False
    assert selection.openalex_quota_policy == "wait"
    assert not any(prompt.startswith("选择 1–2；回车默认等待") for prompt in prompts)
    assert "本会议通用搜索: 禁用" in output.getvalue()
    governance = tmp_path / "rules"
    governance.mkdir()
    (governance / "rule.md").write_text("fixture", encoding="utf-8")
    repo = start_meeting(cfg, selection, governance_docs=governance, output_directory=tmp_path / "meetings")
    assert general_search_allowed(repo) is False
    assert isinstance(cli._build_research_retriever(cfg, repo), OpenAlexRetriever)


def test_conflicting_initial_quota_authorization_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="禁止通用搜索"):
        MeetingRepository.create(
            tmp_path / "meetings", selected_models=[("fake", "m")], chair_model=("fake", "m"),
            governance_docs=tmp_path, research_enabled=True,
            research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
            general_search_allowed=False, openalex_quota_policy="tavily",
        )
    assert not (tmp_path / "meetings").exists()


@pytest.mark.parametrize("flag,allowed", [("--no-general-search", False), ("--general-search", True)])
def test_noninteractive_start_freezes_same_permission(tmp_path, monkeypatch, flag, allowed):
    cfg = _config()
    cfg._source_path = tmp_path / "fixture.toml"
    cfg._source_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "_load_config", lambda _path: cfg)
    monkeypatch.setattr(cli, "discover_models", lambda *_a: [ModelDescriptor(provider_id="fake", model_id="m")])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "ensemble", "start", "--config", str(cfg.source_path), "--non-interactive", "--meeting-type", "research",
        "--provider", "fake", "--research-model", "fake:m", "--task", "fixture",
        "--governance-docs", str(tmp_path), flag,
    ])
    assert cli.main() == 0
    roots = list(tmp_path.glob("LR-*"))
    assert len(roots) == 1
    assert general_search_allowed(MeetingRepository(roots[0])) is allowed

@pytest.mark.parametrize("initial", [True, False])
def test_runtime_toggle_is_append_only_repeated_and_retained_on_resume(tmp_path, monkeypatch, initial):
    repo = _meeting(tmp_path, initial)
    cfg = _config()
    cfg.research.tavily.enabled = True
    monkeypatch.setattr(TavilyResearchConfig, "api_key", lambda _self: "fixture-key")
    originals = {name: (repo.root / name).read_bytes() for name in (
        "public/meeting_manifest.json", "identity_private/meeting_manifest.json",
    )}
    config_before = cfg.model_dump()
    for value in (not initial, initial, not initial):
        record_run_control(repo, kind="general_search_allowed", target=None, value=value)
        resumed = MeetingRepository(repo.root)
        assert general_search_allowed(resumed) is value
        retriever = cli._build_research_retriever(cfg, resumed)
        assert isinstance(retriever, PolicyResearchRetriever if value else OpenAlexRetriever)
        reader = SourceReader(repo=resumed, retriever=retriever)
        assert (reader.tavily is not None) is value
    assert all((repo.root / name).read_bytes() == content for name, content in originals.items())
    records = sorted((repo.root / "human_private/runtime_controls").glob("control-*.json"))
    assert len(records) == 3
    assert [json.loads(path.read_text())["sequence"] for path in records] == [1, 2, 3]
    assert cfg.model_dump() == config_before
    assert repo.events.verify()


def test_explicit_runtime_permission_enables_quota_override_without_affecting_other_meeting(tmp_path):
    repo = _meeting(tmp_path, False)
    other = _meeting(tmp_path, True)
    record_run_control(repo, kind="general_search_allowed", target=None, value=True)
    record_run_control(repo, kind="openalex_quota_policy", target=None, value="tavily")
    assert effective_openalex_quota_policy(repo, "wait") == "tavily"
    record_run_control(repo, kind="general_search_allowed", target=None, value=False)
    assert effective_openalex_quota_policy(repo, "wait") == "wait"
    assert general_search_allowed(other) is True


@pytest.mark.parametrize("value,target", [(1, None), ("false", None), (None, None), (False, "RESEARCH_DESK")])
def test_runtime_search_permission_requires_boolean_and_meeting_wide_target(tmp_path, value, target):
    repo = _meeting(tmp_path)
    events = repo.events.path.read_bytes()
    with pytest.raises(ValueError, match="meeting-wide boolean"):
        record_run_control(repo, kind="general_search_allowed", target=target, value=value)
    assert repo.events.path.read_bytes() == events
    assert not (repo.root / "human_private/runtime_controls").exists()


@pytest.mark.parametrize("value,authority", [("false", "HUMAN"), (True, "AI")])
def test_invalid_runtime_search_permission_fails_closed(tmp_path, value, authority):
    repo = _meeting(tmp_path)
    folder = repo.root / "human_private/runtime_controls"
    folder.mkdir()
    (folder / "control-000001.json").write_text(json.dumps({
        "kind": "general_search_allowed", "value": value, "authority": authority,
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="开关记录无效"):
        general_search_allowed(repo)


@pytest.mark.parametrize("initial,answer,value", [(True, "2", False), (False, "1", True)])
@pytest.mark.parametrize("deferred", [True, False])
def test_ctrl_r_search_toggle_supports_immediate_and_queued_changes(tmp_path, monkeypatch, initial, answer, value, deferred):
    repo = _meeting(tmp_path, initial)
    original = (repo.root / "identity_private/meeting_manifest.json").read_bytes()
    answers = iter(["9", answer])
    prompts = []
    monkeypatch.setattr(cli, "terminal_input", lambda prompt: prompts.append(prompt) or next(answers))
    changes = cli._interactive_model_replacement(repo=repo, cfg=_config(), deferred=deferred)
    if deferred:
        assert changes == [{
            "kind": "runtime_control", "control_kind": "general_search_allowed",
            "target": None, "value": value, "reason": None,
        }]
        assert general_search_allowed(repo) is initial
    else:
        assert changes == []
        assert general_search_allowed(repo) is value
    assert (repo.root / "identity_private/meeting_manifest.json").read_bytes() == original
    assert not any("原因" in prompt for prompt in prompts)


@pytest.mark.parametrize("answer", ["b", ""])
def test_cancelled_search_toggle_does_not_write_runtime_control(tmp_path, monkeypatch, answer):
    repo = _meeting(tmp_path)
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: answer)
    assert cli._interactive_run_control(repo=repo, cfg=_config(), mode=9, deferred=False, output=io.StringIO()) is None
    assert not (repo.root / "human_private/runtime_controls").exists()


def test_runtime_allow_does_not_enable_unconfigured_global_backend(tmp_path, monkeypatch):
    repo = _meeting(tmp_path, False)
    cfg = _config()
    answers = iter(["9", "1"])
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: next(answers))
    assert cli._interactive_model_replacement(repo=repo, cfg=cfg) == []
    assert general_search_allowed(repo) is True
    assert cfg.research.tavily.enabled is False
    assert isinstance(cli._build_research_retriever(cfg, repo), OpenAlexRetriever)


def test_live_toggle_refreshes_retriever_and_reader_for_future_calls(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    repo = _meeting(tmp_path)
    cfg = _config()
    cfg.research.tavily.enabled = True
    monkeypatch.setattr(TavilyResearchConfig, "api_key", lambda _self: "fixture-key")
    retriever = cli._build_research_retriever(cfg, repo)
    previous_reader = SourceReader(repo=repo, retriever=retriever, max_sources=2)
    desk = SimpleNamespace(
        retriever=retriever, source_reader=previous_reader,
        update_model_concurrency_limit=lambda _value: None,
    )
    progress = SimpleNamespace(live_research_desk=desk, info=lambda _text: None)
    engine = SimpleNamespace(
        adapters={}, max_output_tokens=1, participant_concurrency_limit=lambda _who: 1,
    )
    monkeypatch.setattr(cli, "_meeting_concurrency_limits", lambda *_a: {})
    monkeypatch.setattr(cli, "_configured_input_context_budgets", lambda **_k: {})
    monkeypatch.setattr(cli, "_interactive_model_replacement", lambda **_k: [{
        "kind": "runtime_control", "control_kind": "general_search_allowed",
        "target": None, "value": False, "reason": None,
    }])
    cli._install_live_batch_controls(progress=progress, repo=repo, cfg=cfg, engine=engine)
    progress.live_batch_control_callback()
    assert isinstance(desk.retriever, OpenAlexRetriever)
    assert desk.source_reader.tavily is None
    assert desk.source_reader.fetcher is previous_reader.fetcher
    assert desk.source_reader.max_sources == 2
    assert previous_reader.tavily is not None  # active calls' existing reader is not mutated
    assert general_search_allowed(repo) is False


def test_fast_live_toggle_refreshes_route_without_cancelling_active_work(tmp_path):
    import threading
    from types import SimpleNamespace
    from project_ensemble.orchestration.literature_fast import FastLiteratureRunner
    from project_ensemble.orchestration.literature_report import OutlineModule
    from project_ensemble.runtime.progress import NullProgressReporter

    repo = _meeting(tmp_path)
    module = OutlineModule(
        module_id="RM-01", title="fixture", research_questions=["question"],
        required_evidence=["source"], source_submission_refs=["WRITER"],
    )
    first_started = threading.Event()
    refresh_applied = threading.Event()
    observed = []

    class Progress(NullProgressReporter):
        def control_request_pending(self):
            return first_started.is_set() and not refresh_applied.is_set()

    runner = object.__new__(FastLiteratureRunner)
    runner.repo = repo
    runner.engine = SimpleNamespace(progress=Progress())
    runner.research_desk = object()
    runner.research_max_concurrent_claim_groups = 1
    runner._fast_normalize_claim = lambda *_a: SimpleNamespace(is_researchable=True)

    def retrieve(_module, _round, index, _claim, _normalized):
        if index == 1:
            original_permission = general_search_allowed(repo)
            first_started.set()
            assert refresh_applied.wait(timeout=3)
            observed.append(original_permission)
        else:
            observed.append(general_search_allowed(repo))
        return SimpleNamespace(query_trace=[])

    runner._fast_retrieve_claim = retrieve
    runner._fast_read_sources = lambda *_a: ([], [])
    runner._research_claim = lambda _m, _r, index, _claim, **_k: {
        "claim": f"question {index}", "status": "PACKET", "packet_id": f"RP-{index}",
    }
    runner.batch_control_callback = lambda: [{
        "kind": "runtime_control", "control_kind": "general_search_allowed",
        "target": None, "value": False, "reason": None,
    }]

    def refresh():
        assert general_search_allowed(repo) is False
        refresh_applied.set()

    runner.batch_retriever_refresh = refresh
    outcomes = {"RM-01": []}
    runner._run_staged_research_jobs(
        [(module, 1, "question 1"), (module, 2, "question 2")], 1, outcomes,
    )
    assert observed == [True, False]
    assert len(outcomes["RM-01"]) == 2


def test_archiving_retains_runtime_search_permission_for_post_meeting_qa(tmp_path, monkeypatch):
    from project_ensemble.storage.meeting_management import compact_meeting

    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, False)
    repo.docs.write_once("public/final/final_report_v2.md", "# 完整报告\n\n正文。")
    record_run_control(repo, kind="general_search_allowed", target=None, value=True)
    record_run_control(repo, kind="openalex_quota_policy", target=None, value="tavily")
    original = (repo.root / "identity_private/meeting_manifest.json").read_bytes()
    compact_meeting(repo.root)
    resumed = MeetingRepository(repo.root)
    assert general_search_allowed(resumed) is True
    assert effective_openalex_quota_policy(resumed, "wait") == "tavily"
    assert (repo.root / "identity_private/meeting_manifest.json").read_bytes() == original
    assert len(list((repo.root / "human_private/runtime_controls").glob("control-*.json"))) == 2
