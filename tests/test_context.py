import json
from pathlib import Path

import pytest

from project_ensemble.domain import Persona
from project_ensemble.errors import AccessDeniedError
from project_ensemble.runtime.context import RepresentativeContextAssembler, RepresentativeContextSpec
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.storage.events import HashChainEventLog


def test_context_contains_only_explicit_files(tmp_path):
    files = {}
    for name, text in {
        "common.md": "COMMON",
        "persona.md": "PERSONA",
        "stage.md": "CURRENT_STAGE",
        "public.md": "PUBLIC",
        "future.md": "FUTURE_SECRET",
    }.items():
        p = tmp_path / name
        p.write_text(text)
        files[name] = p
    spec = RepresentativeContextSpec(
        common_rules=files["common.md"],
        persona_runtime=files["persona.md"],
        current_stage_protocol=files["stage.md"],
        public_state_files=(files["public.md"],),
    )
    out = RepresentativeContextAssembler().assemble(spec)
    assert "COMMON" in out and "PERSONA" in out and "CURRENT_STAGE" in out and "PUBLIC" in out
    assert "FUTURE_SECRET" not in out
    assert out.index("COMMON") < out.index("CURRENT_STAGE") < out.index("PUBLIC") < out.index(
        "PERSONA"
    )


def test_representative_common_rules_require_concise_reasons_but_exempt_writing(tmp_path):
    governance = Path(__file__).resolve().parents[1] / "docs/governance"
    persona = tmp_path / "persona.md"
    stage = tmp_path / "stage.md"
    persona.write_text("PERSONA", encoding="utf-8")
    stage.write_text("STAGE", encoding="utf-8")
    context = RepresentativeContextAssembler().assemble(
        RepresentativeContextSpec(
            common_rules=governance / "01_constitution/common_representative_rules.md",
            persona_runtime=persona,
            current_stage_protocol=stage,
        )
    )
    assert "所有提议、修正案、质疑、反对意见及否决理由均应采用“最短充分说明”" in context
    assert "写作任务的正文不受本条的简短要求" in context
    assert "不得以精简为由省略决定性证据、风险或反例" in context


@pytest.mark.parametrize("persona", list(Persona))
def test_representative_persona_prompt_is_task_focused_and_human_readable(persona):
    governance = Path(__file__).resolve().parents[1] / "docs/governance"
    spec = GovernanceDocumentResolver(governance).representative_context_spec(
        persona=persona, stage="general_position"
    )
    persona_prompt = spec.persona_runtime.read_text(encoding="utf-8")
    assert "你是本场议事会议的一名 Representative" not in persona_prompt
    assert "不赋予新的身份、叙事角色或制度解释权" in persona_prompt
    assert "面向人类读者" in persona_prompt
    assert "不必要的长篇输出" in persona_prompt
    assert "正文写作可按任务需要充分展开" in persona_prompt


def test_audited_context_records_each_authorized_read_and_rejects_other_private_state(
    tmp_path,
):
    governance = tmp_path / "governance"
    common = governance / "01_constitution/common_representative_rules.md"
    persona = governance / "07_runtime_memory/deliberation/systems_integrator.md"
    stage = governance / "07_runtime_protocol/deliberation/phase_ballot.md"
    for path, text in ((common, "COMMON"), (persona, "PERSONA"), (stage, "STAGE")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    meeting = tmp_path / "M-AUDIT"
    public = meeting / "public/task.json"
    own = meeting / "representatives/R-AAAAAA/status_transition_001.json"
    other = meeting / "representatives/R-BBBBBB/status_transition_001.json"
    for path, text in (
        (public, '{"task":"x"}'),
        (own, '{"status":"ACTIVE"}'),
        (other, '{"status":"ACTIVE"}'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    assembler = RepresentativeContextAssembler()
    allowed = RepresentativeContextSpec(
        common_rules=common,
        persona_runtime=persona,
        current_stage_protocol=stage,
        public_state_files=(public,),
        own_state_files=(own,),
        representative_id="R-AAAAAA",
        stage="ballot",
        meeting_root=meeting,
        governance_root=governance,
    )
    context = assembler.assemble(allowed)
    assert "ACTIVE" in context

    denied = RepresentativeContextSpec(
        common_rules=common,
        persona_runtime=persona,
        current_stage_protocol=stage,
        own_state_files=(other,),
        representative_id="R-AAAAAA",
        stage="ballot",
        meeting_root=meeting,
        governance_root=governance,
    )
    with pytest.raises(AccessDeniedError, match="own-state context path"):
        assembler.assemble(denied)

    ledger = meeting / "governance_private/representative_file_access.jsonl"
    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert sum(item["event_type"] == "REPRESENTATIVE_FILE_READ" for item in records) == 8
    assert records[-1]["event_type"] == "REPRESENTATIVE_FILE_ACCESS_DENIED"
    assert records[-1]["payload"]["decision"] == "DENIED_BEFORE_CONTEXT_RELEASE"
    assert HashChainEventLog(ledger).verify()


def test_research_snapshots_are_released_as_a_bounded_relevance_view(tmp_path):
    files = {}
    for name, text in {
        "common.md": "COMMON",
        "persona.md": "PERSONA",
        "stage.md": "STAGE",
        "public.md": "capillary wave interface",
    }.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        files[name] = path
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "round_id": "research-round-1",
                "packets": [
                    {
                        "packet_id": "RP-RELEVANT",
                        "normalized_claim": "capillary wave interface method",
                        "verification_question": "Does the interface admit a height field?",
                        "scope_terms": ["capillary", "interface"],
                        "supporting_evidence": [],
                        "contradictory_evidence": [],
                        "scope_limitations": [],
                        "canonical_alternatives": [],
                        "sources": [],
                    },
                    {
                        "packet_id": "RP-IRRELEVANT",
                        "normalized_claim": "A completely unrelated astronomy result " + "X" * 20_000,
                        "verification_question": "astronomy?",
                        "scope_terms": ["astronomy"],
                        "supporting_evidence": [],
                        "contradictory_evidence": [],
                        "scope_limitations": [],
                        "canonical_alternatives": [],
                        "sources": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    spec = RepresentativeContextSpec(
        common_rules=files["common.md"],
        persona_runtime=files["persona.md"],
        current_stage_protocol=files["stage.md"],
        public_state_files=(files["public.md"],),
        research_evidence_files=(snapshot,),
        research_evidence_max_packets=1,
        research_evidence_max_chars=4_000,
    )

    context = RepresentativeContextAssembler().assemble(spec)

    evidence = context.split("## PUBLIC RESEARCH EVIDENCE", 1)[1].split(
        "## YOUR PERSONA", 1
    )[0]
    assert "RP-RELEVANT" in evidence
    assert "RP-IRRELEVANT" not in evidence
    assert "research-round-1" in evidence
    assert len(evidence) <= 4_200
    assert "complete released evidence database remains public" in evidence
