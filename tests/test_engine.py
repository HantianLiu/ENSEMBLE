import json

import pytest
from pydantic import BaseModel, ConfigDict

from project_ensemble.domain import DecisionRigor, GenerationResponse, ReasoningEffort
from project_ensemble.errors import (
    EmptyModelOutputError,
    InputContextLimitError,
    OutputLimitReachedError,
    PermanentProviderError,
    PolicyNotConfiguredError,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.runtime.research_fallbacks import ResearchFallbacks
from project_ensemble.storage.meeting import MeetingRepository


class CapturingNotifier:
    def __init__(self):
        self.calls = []

    def send_escalation(self, **kwargs):
        self.calls.append(kwargs)
        return True


def make_repo(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        task_description="task",
        escalation_email="human@example.test",
    )
    registry = json.loads((repo.root / "identity_private/representative_registry.json").read_text())
    return repo, registry[0]["representative_id"]


def test_engine_records_success_without_putting_text_in_event_log(tmp_path):
    repo, representative_id = make_repo(tmp_path)
    notifier = CapturingNotifier()
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], ["answer text"])},
        notifier=notifier,
    )
    result = engine.invoke_participant(
        representative_id,
        system_text="current-stage-only context",
        user_text="task action",
        stage="initial_draft",
    )
    assert result.text == "answer text"
    event_text = repo.events.path.read_text()
    assert "PROVIDER_EXCHANGE_RECORDED" in event_text
    assert "answer text" not in event_text
    exchanges = list((repo.root / "governance_private/provider_exchanges").glob("*.json"))
    assert len(exchanges) == 1
    assert "answer text" in exchanges[0].read_text()
    telemetry = list((repo.root / "governance_private/telemetry").glob("*.json"))
    assert len(telemetry) == 1
    token_record = json.loads(telemetry[0].read_text())
    assert token_record["exchange_id"] == exchanges[0].stem
    assert token_record["prompt_tokens"] is None
    assert "PROVIDER_TOKEN_TELEMETRY_RECORDED" in event_text
    assert not notifier.calls
    assert repo.events.verify()


def test_research_desk_once_backup_runs_only_after_primary_failure(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("primary", "m")],
        chair_model=("primary", "m"), governance_docs=gov,
        research_enabled=True, research_model=("primary", "m"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
    )

    class Primary(ScriptedProviderAdapter):
        calls = 0

        def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                raise PermanentProviderError("content rejected")
            return GenerationResponse(text="primary recovered", provider_id="primary",
                                      model_id=request.model_id)

    primary = Primary("primary", ["m"], [])
    backup = ScriptedProviderAdapter("backup", ["b"], ["backup answer"])
    ResearchFallbacks(repo).choose(
        request_id="PROVIDER-FAILURE-1", source=("primary", "m"),
        target=("backup", "b"), scope="NEXT_FAILURE_ONLY", reason="Human choice",
    )
    engine = MeetingEngine(
        repo=repo, adapters={"primary": primary, "backup": backup},
        notifier=CapturingNotifier(), max_retries=0,
    )
    first = engine.invoke_participant(
        "RESEARCH_DESK", system_text="check", user_text="claim", stage="research",
    )
    second = engine.invoke_participant(
        "RESEARCH_DESK", system_text="check", user_text="next claim", stage="research",
    )
    assert first.text == "backup answer"
    assert second.text == "primary recovered"
    assert engine._runtime_for("RESEARCH_DESK") == ("primary", "m")
    assert repo.events.verify()


def test_deliberation_writing_language_is_frozen_per_meeting_not_taken_from_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
    from project_ensemble.user_settings import save_interface_language
    save_interface_language("zh")
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")], chair_model=("fake", "m"),
        governance_docs=gov, task_description="task", deliberation_language="en",
    )
    prompts = []

    class CapturingAdapter(ScriptedProviderAdapter):
        def generate(self, request):
            prompts.append(request.system_text)
            return super().generate(request)

    adapter = CapturingAdapter("fake", ["m"], ["answer"])
    engine = MeetingEngine(repo=repo, adapters={"fake": adapter}, notifier=CapturingNotifier())
    engine.invoke_participant("CHAIR", system_text="meeting procedure", user_text="draft", stage="initial_draft")
    assert "Use English for reader-facing deliberation prose" in prompts[0]
    assert json.loads(repo.docs.read_text("identity_private/meeting_manifest.json"))["deliberation_language"] == "en"


def test_relaxed_human_threshold_ruling_is_visible_to_chair_but_not_ordinary_position(tmp_path):
    class CapturingAdapter(ScriptedProviderAdapter):
        prompts = None

        def generate(self, request):
            self.prompts.append(request.system_text)
            return super().generate(request)

    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        task_description="task",
        decision_rigor=DecisionRigor.RELAXED,
    )
    representative_id = json.loads(
        repo.docs.read_text("identity_private/representative_registry.json")
    )[0]["representative_id"]
    adapter = CapturingAdapter("fake", ["m"], ["chair answer", "position answer"])
    adapter.prompts = []
    engine = MeetingEngine(repo=repo, adapters={"fake": adapter}, notifier=CapturingNotifier())
    engine.invoke_participant("CHAIR", system_text="baseline text", user_text="action", stage="chair_review")
    engine.invoke_participant(
        representative_id, system_text="current position", user_text="action", stage="general_position"
    )
    assert "floor(N_ACTIVE/2)+1" in adapter.prompts[0]
    assert "floor(N_ACTIVE/2)+1" not in adapter.prompts[1]


def test_engine_pauses_before_provider_call_when_input_safety_budget_is_exceeded(tmp_path):
    repo, representative_id = make_repo(tmp_path)
    adapter = ScriptedProviderAdapter("fake", ["m"], ["must not be consumed"])
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": adapter},
        notifier=CapturingNotifier(),
        input_context_budgets={("fake", "m"): 600},
    )

    with pytest.raises(InputContextLimitError, match="exceeds the frozen safety budget"):
        engine.invoke_participant(
            representative_id,
            system_text="x" * 1_000,
            user_text="task",
            stage="general_position",
        )

    assert not list((repo.root / "governance_private/provider_exchanges").glob("*.json"))
    events = repo.events.path.read_text(encoding="utf-8")
    assert "MODEL_INPUT_CONTEXT_PREFLIGHT" in events
    assert "MODEL_INPUT_CONTEXT_BUDGET_EXCEEDED" in events


def test_engine_checks_provider_character_limit_separately_from_token_budget(tmp_path):
    repo, representative_id = make_repo(tmp_path)
    adapter = ScriptedProviderAdapter("fake", ["m"], ["must not be consumed"])
    adapter.maximum_input_characters = 10
    engine = MeetingEngine(
        repo=repo, adapters={"fake": adapter}, notifier=CapturingNotifier(),
        input_context_budgets={("fake", "m"): 10_000},
    )
    with pytest.raises(InputContextLimitError, match="above the 10-character safety budget"):
        engine.invoke_participant(
            representative_id, system_text="system", user_text="x" * 11,
            stage="general_position",
        )
    events = repo.events.path.read_text(encoding="utf-8")
    assert "MODEL_INPUT_CHARACTER_PREFLIGHT" in events
    assert "MODEL_INPUT_CHARACTER_BUDGET_EXCEEDED" in events


def test_technician_repairs_oversized_evidence_view_without_editing_frozen_sources(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")], chair_model=("fake", "m"),
        technician_model=("fake", "m"), technician_reasoning_effort=ReasoningEffort.DEFAULT,
        governance_docs=gov, task_description="human task",
    )
    representative_id = json.loads(repo.docs.read_text(
        "identity_private/representative_registry.json"
    ))[0]["representative_id"]

    class CapturingAdapter(ScriptedProviderAdapter):
        def __init__(self):
            super().__init__("fake", ["m"], ['{"priority":[0,1,2]}', "review done"])
            self.requests = []
            self.maximum_input_characters = 1000

        def generate(self, request):
            self.requests.append(request)
            return super().generate(request)

    adapter = CapturingAdapter()
    engine = MeetingEngine(repo=repo, adapters={"fake": adapter}, notifier=CapturingNotifier())
    original = json.dumps({
        "task": "compare papers", "needed_packet_id": "RP-A",
        "evidence_packets": [
            {"packet_id": "RP-A", "text": "a" * 550},
            {"packet_id": "RP-B", "text": "b" * 550},
            {"packet_id": "RP-C", "text": "c" * 550},
        ],
    })
    result = engine.invoke_participant(
        representative_id, system_text="review", user_text=original,
        stage="general_position",
    )
    assert result.text == "review done"
    assert len(adapter.requests) == 2
    assert "candidate_count" in adapter.requests[0].user_text
    assert "RP-A" in adapter.requests[1].user_text
    assert len(adapter.requests[1].user_text) <= 1000
    records = list((repo.root / "audit_private/technician").glob("*/record.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["scope"] == (
        "MEETING_REQUEST_ONLY; NO_PROGRAM_OR_FROZEN_DOCUMENT_EDIT"
    )
    assert (gov / "rule.md").read_text() == "rule"


def test_engine_resolves_configured_then_reported_then_safe_concurrency(tmp_path):
    class ReportingAdapter(ScriptedProviderAdapter):
        def reported_concurrency_limit(self, model_id):
            return 3 if model_id == "reported" else None

    repo, _representative_id = make_repo(tmp_path)
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ReportingAdapter("fake", ["m"], [])},
        notifier=CapturingNotifier(),
        configured_concurrency_limits={
            ("fake", "*"): 2,
            ("fake", "exact"): 4,
        },
    )
    assert engine.model_concurrency_limit("fake", "exact") == 4
    assert engine.model_concurrency_limit("fake", "reported") == 2

    reported_only = MeetingEngine(
        repo=repo,
        adapters={"fake": ReportingAdapter("fake", ["m"], [])},
        notifier=CapturingNotifier(),
    )
    assert reported_only.model_concurrency_limit("fake", "reported") == 3
    assert reported_only.model_concurrency_limit("fake", "unknown") == 1


def test_recorded_response_recovery_accepts_only_context_section_reordering(tmp_path):
    repo, representative_id = make_repo(tmp_path)
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], ["answer text"])},
        notifier=CapturingNotifier(),
    )
    old_layout = (
        "## COMMON RULES\ncommon\n\n"
        "## YOUR PERSONA\npersona\n\n"
        "## CURRENT STAGE\nstage\n\n"
        "## PUBLIC STATE: task.md\npublic\n"
    )
    new_layout = (
        "## COMMON RULES\ncommon\n\n"
        "## CURRENT STAGE\nstage\n\n"
        "## PUBLIC STATE: task.md\npublic\n\n"
        "## YOUR PERSONA\npersona\n"
    )
    original = engine.invoke_participant(
        representative_id,
        system_text=old_layout,
        user_text="same action",
        stage="general_position",
    )

    recovered = engine.find_recorded_response(
        representative_id,
        system_text=new_layout,
        user_text="same action",
        stage="general_position",
    )
    changed = engine.find_recorded_response(
        representative_id,
        system_text=new_layout.replace("public", "changed public"),
        user_text="same action",
        stage="general_position",
    )

    assert recovered == original
    assert changed is None


def test_engine_repairs_schema_once_without_changing_substantive_choice(tmp_path):
    class Action(BaseModel):
        model_config = ConfigDict(extra="forbid")
        action: str

    repo, representative_id = make_repo(tmp_path)
    engine = MeetingEngine(
        repo=repo,
        adapters={
            "fake": ScriptedProviderAdapter(
                "fake", ["m"], ['{"wrong":"SUPPORT"}', '{"action":"SUPPORT"}']
            )
        },
        notifier=CapturingNotifier(),
    )
    original = engine.invoke_participant(
        representative_id,
        system_text="s",
        user_text="u",
        stage="general_position",
    )

    parsed = engine.validate_structured_response(
        representative_id,
        response=original,
        schema_model=Action,
        stage="general_position",
    )

    assert parsed.action == "SUPPORT"
    events = repo.events.path.read_text()
    assert "MODEL_OUTPUT_SCHEMA_REPAIR_REQUESTED" in events
    assert "MODEL_OUTPUT_SCHEMA_REPAIR_SUCCEEDED" in events
    assert repo.events.verify()


def test_stage_specific_repair_receives_concrete_diagnostic_and_may_remove_unsupported_content(
    tmp_path,
):
    class Action(BaseModel):
        model_config = ConfigDict(extra="forbid")
        action: str

    class CapturingAdapter(ScriptedProviderAdapter):
        def __init__(self):
            super().__init__("fake", ["m"], ['{"wrong":"invented"}', '{"action":"UNRESOLVED"}'])
            self.requests = []

        def generate(self, request):
            self.requests.append(request)
            return super().generate(request)

    repo, representative_id = make_repo(tmp_path)
    adapter = CapturingAdapter()
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": adapter},
        notifier=CapturingNotifier(),
    )
    original = engine.invoke_participant(
        representative_id,
        system_text="s",
        user_text="u",
        stage="research_evidence_synthesis",
    )

    parsed = engine.validate_structured_response(
        representative_id,
        response=original,
        schema_model=Action,
        stage="research_evidence_synthesis",
        repair_guidance="只允许候选来源 W1；删除无依据断言。",
        repair_diagnostic=lambda raw: "packet.sources[1].source_id='W2'：候选之外。",
    )

    assert parsed.action == "UNRESOLVED"
    assert "允许删除或降级违规内容" in adapter.requests[1].system_text
    assert "packet.sources[1].source_id='W2'：候选之外" in adapter.requests[1].user_text
    assert "Preserve the participant's substantive choice" not in adapter.requests[1].system_text


def test_schema_repair_uses_kimi_required_temperature_and_can_resume(tmp_path):
    class Action(BaseModel):
        model_config = ConfigDict(extra="forbid")
        action: str

    class CapturingAdapter(ScriptedProviderAdapter):
        requests = None

        def generate(self, request):
            self.requests.append(request)
            return super().generate(request)

    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("kimi", "kimi-k3")],
        chair_model=("kimi", "kimi-k3"),
        governance_docs=gov,
        task_description="task",
        escalation_email="human@example.test",
    )
    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    representative_id = registry[0]["representative_id"]
    adapter = CapturingAdapter(
        "kimi", ["kimi-k3"], ['{"wrong":"SUPPORT"}', '{"action":"SUPPORT"}']
    )
    adapter.requests = []
    engine = MeetingEngine(
        repo=repo,
        adapters={"kimi": adapter},
        notifier=CapturingNotifier(),
    )
    original = engine.invoke_participant(
        representative_id,
        system_text="s",
        user_text="u",
        stage="general_position",
    )

    parsed = engine.validate_structured_response(
        representative_id,
        response=original,
        schema_model=Action,
        stage="general_position",
    )

    assert parsed.action == "SUPPORT"
    assert adapter.requests[1].temperature == 1.0
    exchange_count = len(
        list((repo.root / "governance_private/provider_exchanges").glob("*.json"))
    )

    parsed_after_resume = engine.validate_structured_response(
        representative_id,
        response=original,
        schema_model=Action,
        stage="general_position",
    )

    assert parsed_after_resume.action == "SUPPORT"
    assert len(adapter.requests) == 2
    assert len(
        list((repo.root / "governance_private/provider_exchanges").glob("*.json"))
    ) == exchange_count


def test_schema_validation_allows_a_separately_recorded_second_repair(tmp_path):
    class Action(BaseModel):
        model_config = ConfigDict(extra="forbid")
        action: str

    repo, representative_id = make_repo(tmp_path)
    engine = MeetingEngine(
        repo=repo,
        adapters={
            "fake": ScriptedProviderAdapter(
                "fake",
                ["m"],
                ['{"wrong":"first"}', '{"still_wrong":"repair"}', '{"action":"SUPPORT"}'],
            )
        },
        notifier=CapturingNotifier(),
    )
    original = engine.invoke_participant(
        representative_id,
        system_text="s",
        user_text="u",
        stage="general_position",
    )

    parsed = engine.validate_structured_response(
        representative_id,
        response=original,
        schema_model=Action,
        stage="general_position",
    )

    assert parsed.action == "SUPPORT"
    events = repo.events.path.read_text()
    assert "MODEL_OUTPUT_SCHEMA_SECOND_REPAIR_REQUESTED" in events
    assert "MODEL_OUTPUT_SCHEMA_SECOND_REPAIR_SUCCEEDED" in events


def test_engine_backfills_normalized_telemetry_from_legacy_exchange(tmp_path):
    repo, representative_id = make_repo(tmp_path)
    repo.docs.write_once(
        "governance_private/provider_exchanges/X-LEGACY.json",
        json.dumps(
            {
                "exchange_id": "X-LEGACY",
                "participant_id": representative_id,
                "provider_id": "fake",
                "model_id": "m",
                "stage": "general_position",
                "request": {},
                "response": {
                    "usage": {
                        "prompt_tokens": 100,
                        "prompt_tokens_details": {"cached_tokens": 40},
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    }
                },
            }
        ),
    )

    MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], [])},
        notifier=CapturingNotifier(),
    )

    telemetry = json.loads(
        (repo.root / "governance_private/telemetry/X-LEGACY.json").read_text()
    )
    assert telemetry["cached_tokens"] == 40
    assert telemetry["cache_miss_tokens"] == 60
    assert telemetry["cache_hit_rate"] == 0.4
    assert "PROVIDER_TOKEN_TELEMETRY_BACKFILL_COMPLETED" in repo.events.path.read_text()
    assert repo.events.verify()


def test_engine_applies_model_specific_output_budget(tmp_path):
    class CapturingAdapter(ScriptedProviderAdapter):
        request = None

        def generate(self, request):
            self.request = request
            return GenerationResponse(text="answer", provider_id="fake", model_id=request.model_id)

    repo, representative_id = make_repo(tmp_path)
    adapter = CapturingAdapter("fake", ["m"])
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": adapter},
        notifier=CapturingNotifier(),
        output_token_budgets={("fake", "m"): 50000},
    )
    engine.invoke_participant(
        representative_id,
        system_text="s",
        user_text="u",
        stage="general_position",
    )
    assert adapter.request.max_output_tokens == 50000


def test_engine_applies_frozen_role_specific_reasoning_effort(tmp_path):
    class CapturingAdapter(ScriptedProviderAdapter):
        requests = None

        def generate(self, request):
            self.requests.append(request)
            return super().generate(request)

    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        representative_reasoning_effort=ReasoningEffort.LOW,
        chair_reasoning_effort=ReasoningEffort.HIGH,
    )
    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    representative_id = registry[0]["representative_id"]
    adapter = CapturingAdapter("fake", ["m"], ["representative", "chair"])
    adapter.requests = []
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": adapter},
        notifier=CapturingNotifier(),
    )

    engine.invoke_participant(
        representative_id,
        system_text="s",
        user_text="u",
        stage="proposal",
    )
    engine.invoke_participant(
        "CHAIR",
        system_text="s",
        user_text="u",
        stage="chair_review",
    )

    assert adapter.requests[0].reasoning_effort == ReasoningEffort.LOW
    assert adapter.requests[1].reasoning_effort == ReasoningEffort.HIGH


def test_engine_retries_empty_reachable_output_and_preserves_attempts(tmp_path):
    repo, representative_id = make_repo(tmp_path)
    notifier = CapturingNotifier()
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], ["", "answer"])},
        notifier=notifier,
        max_retries=1,
        retry_base_delay_seconds=0,
    )
    response = engine.invoke_participant(
        representative_id,
        system_text="s",
        user_text="u",
        stage="ballot",
        sleep=lambda _seconds: None,
    )
    assert response.text == "answer"
    assert len(list((repo.root / "governance_private/provider_exchanges").glob("*.json"))) == 2
    assert len(list((repo.root / "governance_private/telemetry").glob("*.json"))) == 2
    events = repo.events.path.read_text()
    assert "PROVIDER_RETRY_SCHEDULED" in events
    assert "EmptyModelOutputError" in events
    assert not notifier.calls
    assert repo.events.verify()


def test_engine_pauses_only_after_empty_output_retry_policy_is_exhausted(tmp_path):
    repo, representative_id = make_repo(tmp_path)
    notifier = CapturingNotifier()
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], ["", ""])},
        notifier=notifier,
        max_retries=1,
        retry_base_delay_seconds=0,
    )
    with pytest.raises(EmptyModelOutputError, match="after 2 attempts"):
        engine.invoke_participant(
            representative_id,
            system_text="s",
            user_text="u",
            stage="ballot",
            sleep=lambda _seconds: None,
        )
    assert engine.status.phase.value == "PAUSED"
    assert notifier.calls[0]["reason_code"] == "MODEL_OUTPUT_EMPTY_AFTER_RETRIES"
    events = repo.events.path.read_text()
    assert "MODEL_OUTPUT_EMPTY_AFTER_RETRIES" in events
    assert repo.events.verify()


def test_engine_distinguishes_reasoning_exhausting_output_limit(tmp_path):
    class OutputLimitedAdapter(ScriptedProviderAdapter):
        def generate(self, request):
            return GenerationResponse(
                text="",
                provider_id="fake",
                model_id=request.model_id,
                usage={"completion_tokens": 4096, "completion_tokens_details": {"reasoning_tokens": 4096}},
                raw={"choices": [{"finish_reason": "length", "message": {"content": ""}}]},
            )

    repo, representative_id = make_repo(tmp_path)
    notifier = CapturingNotifier()
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": OutputLimitedAdapter("fake", ["m"])},
        notifier=notifier,
    )
    with pytest.raises(OutputLimitReachedError, match="4096 tokens"):
        engine.invoke_participant(
            representative_id,
            system_text="s",
            user_text="u",
            stage="general_position",
            max_output_tokens=4096,
        )
    assert engine.find_recorded_response(
        representative_id,
        system_text="s",
        user_text="u",
        stage="general_position",
    ) is None
    assert engine.status.phase.value == "PAUSED"
    assert notifier.calls[0]["reason_code"] == "MODEL_OUTPUT_LIMIT_REACHED"
    assert "MODEL_OUTPUT_LIMIT_REACHED" in repo.events.path.read_text()
    assert repo.events.verify()


def test_optional_source_review_output_limit_does_not_pause_meeting(tmp_path):
    class OutputLimitedAdapter(ScriptedProviderAdapter):
        def generate(self, request):
            return GenerationResponse(
                text="", provider_id="fake", model_id=request.model_id,
                raw={"choices": [{"finish_reason": "length", "message": {"content": ""}}]},
            )

    repo, representative_id = make_repo(tmp_path)
    notifier = CapturingNotifier()
    engine = MeetingEngine(
        repo=repo, adapters={"fake": OutputLimitedAdapter("fake", ["m"])},
        notifier=notifier,
    )
    with engine.recoverable_call():
        with pytest.raises(OutputLimitReachedError):
            engine.invoke_participant(
                representative_id, system_text="s", user_text="u",
                stage="research_evidence_synthesis_source_review",
            )
    assert engine.status.phase.value != "PAUSED"
    assert not notifier.calls
    assert "MEETING_FAILED" not in repo.events.path.read_text()

    with pytest.raises(OutputLimitReachedError):
        engine.invoke_participant(
            representative_id, system_text="s", user_text="u",
            stage="research_evidence_synthesis",
        )
    assert engine.status.phase.value == "PAUSED"
    assert notifier.calls[0]["reason_code"] == "MODEL_OUTPUT_LIMIT_REACHED"


def test_engine_permanent_provider_failure_escalates(tmp_path):
    class BrokenAdapter(ScriptedProviderAdapter):
        def generate(self, request):
            raise PermanentProviderError("invalid provider configuration")

    repo, representative_id = make_repo(tmp_path)
    notifier = CapturingNotifier()
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": BrokenAdapter("fake", ["m"])},
        notifier=notifier,
    )
    with pytest.raises(PermanentProviderError):
        engine.invoke_participant(
            representative_id,
            system_text="s",
            user_text="u",
            stage="initial_draft",
        )
    assert notifier.calls[0]["reason_code"] == "PROVIDER_CALL_FAILED"
    assert "invalid provider configuration" not in repo.events.path.read_text()
    diagnostics = list((repo.root / "audit_private/provider_failures").glob("PF-*.json"))
    assert len(diagnostics) == 1
    assert json.loads(diagnostics[0].read_text(encoding="utf-8"))["error_message"] == (
        "invalid provider configuration"
    )
    assert repo.events.verify()
