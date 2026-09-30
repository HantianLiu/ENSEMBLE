from project_ensemble.domain import Persona
from project_ensemble.private_registry import build_private_registry


def test_model_persona_factorial_and_opaque_ids():
    registry = build_private_registry([("deepseek", "m1"), ("kimi", "m2"), ("gemini", "m3")])
    assert len(registry) == 12
    assert len({r.representative_id for r in registry}) == 12
    assert all(r.representative_id.startswith("R-") for r in registry)
    # Each model traverses all personas.
    for provider, model in [("deepseek", "m1"), ("kimi", "m2"), ("gemini", "m3")]:
        ps = {r.runtime.persona for r in registry if r.runtime.provider_id == provider and r.runtime.model_id == model}
        assert ps == set(Persona)
