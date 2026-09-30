import json
from pathlib import Path
from types import SimpleNamespace

from project_ensemble.cli import _meeting_governance_docs
from project_ensemble.domain import Persona
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.storage.meeting import directory_digest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CURRENT = PROJECT_ROOT / "docs/governance"
LEGACY = PROJECT_ROOT / "docs/governance_legacy_2026_09_23"
PRE_REPLAN = PROJECT_ROOT / "docs/governance_frozen_2026_09_24_pre_replan"
PRE_REPLAN_DIGEST = "966d434cf009c2a07a4d967a623fa77dbe9e9992b4646f2a9f9b1e5d51a29dd6"


def test_new_literature_prompt_is_chinese_and_distinct_from_deliberation():
    resolver = GovernanceDocumentResolver(CURRENT)
    literature = resolver.representative_context_spec(
        persona=Persona.SYSTEMS_INTEGRATOR,
        stage="literature_module_draft",
        prompt_family="literature_research",
    )
    deliberation = resolver.representative_context_spec(
        persona=Persona.SYSTEMS_INTEGRATOR,
        stage="initial_draft",
        prompt_family="deliberation",
    )
    text = RepresentativeContextAssembler().assemble(literature)
    assert "## 共同规则" in text
    assert "## 当前阶段" in text
    assert "学术文献综述" in text
    assert "总体原则与具体条款" not in text
    assert literature.common_rules != deliberation.common_rules
    assert literature.persona_runtime != deliberation.persona_runtime


def test_old_meeting_selects_exact_archived_governance_package(tmp_path):
    assert directory_digest(CURRENT) != directory_digest(LEGACY)
    manifest = {"governance_digest": directory_digest(LEGACY)}
    repo = SimpleNamespace(
        root=tmp_path,
        docs=SimpleNamespace(read_text=lambda name: json.dumps(manifest)),
    )
    assert Path(_meeting_governance_docs(repo, CURRENT)) == LEGACY


def test_pre_replan_meeting_selects_exact_archived_governance_package(tmp_path):
    assert directory_digest(PRE_REPLAN) == PRE_REPLAN_DIGEST
    assert directory_digest(CURRENT) != PRE_REPLAN_DIGEST
    manifest = {"governance_digest": PRE_REPLAN_DIGEST}
    repo = SimpleNamespace(
        root=tmp_path,
        docs=SimpleNamespace(read_text=lambda name: json.dumps(manifest)),
    )
    assert Path(_meeting_governance_docs(repo, CURRENT)) == PRE_REPLAN
