from project_ensemble.domain import GenerationRequest, MeetingPhase
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.orchestration.invocation import RepresentativeInvoker
from project_ensemble.orchestration.retry_state import MeetingStatus


def test_scripted_provider_invocation():
    p = ScriptedProviderAdapter("fake", ["m"], ["answer"])
    req = GenerationRequest(model_id="m", system_text="s", user_text="u")
    status = MeetingStatus()
    out = RepresentativeInvoker().invoke("R1", p, req, status)
    assert out.text == "answer"
    assert status.phase == MeetingPhase.INIT
