import pytest
from project_ensemble.providers.retry import call_with_retries
from project_ensemble.errors import TransientProviderError, RepresentativeUnavailableError
from project_ensemble.config import EmailNotificationConfig
from project_ensemble.domain import GenerationRequest
from project_ensemble.notifications.email import EmailNotifier
from project_ensemble.orchestration.escalation import HumanEscalationService
from project_ensemble.orchestration.invocation import RepresentativeInvoker
from project_ensemble.orchestration.retry_state import MeetingStatus
from project_ensemble.providers.base import ProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


def test_initial_plus_three_retries():
    calls = {"n": 0}
    retries = []
    delays = []
    def f():
        calls["n"] += 1
        raise TransientProviderError("no")
    with pytest.raises(RepresentativeUnavailableError, match="last transient failure: no"):
        call_with_retries(
            f,
            max_retries=3,
            base_delay_seconds=30,
            sleep=delays.append,
            on_retry=lambda number, maximum, delay, exc: retries.append(
                (number, maximum, delay, type(exc).__name__)
            ),
        )
    assert calls["n"] == 4
    assert retries == [
        (1, 3, 30, "TransientProviderError"),
        (2, 3, 60, "TransientProviderError"),
        (3, 3, 120, "TransientProviderError"),
    ]
    assert delays == [30, 60, 120]


def test_provider_retry_after_takes_precedence_over_shorter_backoff():
    delays = []

    def fail():
        raise TransientProviderError("rate limited", retry_after_seconds=90)

    with pytest.raises(RepresentativeUnavailableError):
        call_with_retries(fail, max_retries=1, base_delay_seconds=30, sleep=delays.append)
    assert delays == [90]


def test_unavailable_representative_persists_pause_and_escalates(tmp_path):
    class AlwaysUnavailable(ProviderAdapter):
        provider_id = "fake"

        def list_models(self):
            return []

        def generate(self, request):
            raise TransientProviderError("offline")

    class CapturingNotifier:
        def __init__(self):
            self.events = []

        def send_escalation(self, **kwargs):
            self.events.append(kwargs)
            return True

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
    captured = CapturingNotifier()
    escalation = HumanEscalationService(repo, captured)
    invoker = RepresentativeInvoker(max_retries=3, on_unavailable=escalation.representative_unavailable)
    status = MeetingStatus()
    request = GenerationRequest(model_id="m", system_text="s", user_text="u")
    with pytest.raises(RepresentativeUnavailableError):
        invoker.invoke("R-TEST", AlwaysUnavailable(), request, status, sleep=lambda _: None)
    assert status.phase.value == "PAUSED"
    assert len(captured.events) == 1
    assert captured.events[0]["reason_code"] == "REPRESENTATIVE_UNAVAILABLE"
    text = repo.events.path.read_text()
    assert "MEETING_PAUSED" in text
    assert "HUMAN_INTERVENTION_REQUIRED" in text
    assert repo.events.verify()
