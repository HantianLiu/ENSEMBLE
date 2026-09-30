from project_ensemble.domain import Persona
from project_ensemble.runtime.documents import GovernanceDocumentResolver


def test_resolver_selects_exactly_one_persona_and_stage():
    r = GovernanceDocumentResolver("docs/governance")
    spec = r.representative_context_spec(persona=Persona.LIBRARIAN, stage="ballot")
    assert spec.persona_runtime.name == "librarian.md"
    assert spec.current_stage_protocol.name == "phase_ballot.md"


def test_literature_research_has_independent_common_and_persona_prompts():
    resolver = GovernanceDocumentResolver("docs/governance")
    for persona in Persona:
        deliberation = resolver.representative_context_spec(
            persona=persona, stage="general_position", prompt_family="deliberation"
        )
        literature = resolver.representative_context_spec(
            persona=persona, stage="research_decomposition",
            prompt_family="literature_research"
        )
        legacy = resolver.representative_context_spec(
            persona=persona, stage="research_decomposition",
            prompt_family="legacy_shared"
        )
        assert literature.common_rules.name == "literature_representative_rules.md"
        assert literature.persona_runtime.parent.name == "literature_research"
        assert literature.common_rules.is_file() and literature.persona_runtime.is_file()
        assert deliberation.common_rules.name == "common_representative_rules.md"
        assert deliberation.persona_runtime.parent.name == "deliberation"
        assert legacy.common_rules == deliberation.common_rules
        assert legacy.persona_runtime == deliberation.persona_runtime


def test_current_ballot_context_includes_frozen_meeting_threshold_policy(tmp_path):
    meeting = tmp_path / "M-POLICY"
    task = meeting / "public/task.json"
    policy = meeting / "public/decision_policy.json"
    task.parent.mkdir(parents=True)
    task.write_text("{}", encoding="utf-8")
    policy.write_text('{"decision_rigor":"relaxed"}', encoding="utf-8")
    resolver = GovernanceDocumentResolver("docs/governance")
    ballot = resolver.representative_context_spec(
        persona=Persona.LIBRARIAN, stage="ballot", public_state_files=(task,)
    )
    position = resolver.representative_context_spec(
        persona=Persona.LIBRARIAN, stage="general_position", public_state_files=(task,)
    )
    assert policy in ballot.public_state_files
    assert policy not in position.public_state_files
