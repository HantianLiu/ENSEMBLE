import json

from project_ensemble.domain import ModelDescriptor, ReasoningEffort
from project_ensemble.orchestration.budgets import (
    add_replacement_input_context_budgets,
    add_replacement_output_budgets,
    budget_map,
    calculate_token_budget,
    input_context_budget_map,
    load_or_freeze_input_context_budgets,
    load_or_freeze_meeting_budgets,
)
from project_ensemble.runtime.model_replacements import ModelReplacementService
from project_ensemble.storage.meeting import MeetingRepository


def test_budget_targets_half_context_and_respects_output_cap():
    uncapped = calculate_token_budget(
        ModelDescriptor(provider_id="p", model_id="m", input_token_limit=1000),
        context_fraction=0.50,
        fallback_tokens=64,
    )
    assert uncapped.requested_output_tokens == 500
    assert uncapped.basis == "ADVERTISED_CONTEXT_FRACTION"

    capped = calculate_token_budget(
        ModelDescriptor(
            provider_id="p",
            model_id="m",
            input_token_limit=1000,
            output_token_limit=400,
        ),
        context_fraction=0.50,
        fallback_tokens=64,
    )
    assert capped.requested_output_tokens == 400
    assert capped.basis == "ADVERTISED_CONTEXT_FRACTION_CAPPED_BY_OUTPUT_LIMIT"


def test_budget_uses_explicit_fallback_when_catalog_omits_context(tmp_path):
    descriptor = ModelDescriptor(provider_id="deepseek", model_id="flash")
    budget = calculate_token_budget(descriptor, context_fraction=0.50, fallback_tokens=65536)
    assert budget.requested_output_tokens == 65536
    assert budget.basis == "CONFIGURED_FALLBACK_NO_CONTEXT_METADATA"


def test_meeting_budget_snapshot_is_immutable_and_reused(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("p", "m")],
        chair_model=("p", "m"),
        governance_docs=gov,
    )

    class Adapter:
        def list_models(self):
            return [ModelDescriptor(provider_id="p", model_id="m", input_token_limit=1000)]

    first = load_or_freeze_meeting_budgets(
        repo=repo,
        adapters={"p": Adapter()},
        context_fraction=0.50,
        fallback_tokens=64,
    )
    assert budget_map(first) == {("p", "m"): 500}

    class ChangedAdapter:
        def list_models(self):
            raise AssertionError("frozen snapshot must be reused without live rediscovery")

    resumed = load_or_freeze_meeting_budgets(
        repo=repo,
        adapters={"p": ChangedAdapter()},
        context_fraction=0.75,
        fallback_tokens=999,
    )
    assert resumed == first
    persisted = json.loads((repo.root / "governance_private/model_token_budgets.json").read_text())
    assert persisted["models"][0]["requested_output_tokens"] == 500
    assert "MODEL_TOKEN_BUDGETS_FROZEN" in repo.events.path.read_text()
    assert repo.events.verify()


def test_budget_snapshot_includes_chair_and_research_desk_models(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("p", "representative")],
        chair_model=("p", "chair"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("p", "research"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
    )

    class Adapter:
        def list_models(self):
            return [
                ModelDescriptor(provider_id="p", model_id=name, input_token_limit=1000)
                for name in ("representative", "chair", "research")
            ]

    snapshot = load_or_freeze_meeting_budgets(
        repo=repo,
        adapters={"p": Adapter()},
        context_fraction=0.50,
        fallback_tokens=64,
    )

    assert budget_map(snapshot) == {
        ("p", "representative"): 500,
        ("p", "chair"): 500,
        ("p", "research"): 500,
    }


def test_input_context_budget_uses_advertised_limit_and_explicit_fallback(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("p", "known"), ("p", "unknown")],
        chair_model=("p", "known"),
        governance_docs=gov,
    )

    class Adapter:
        def list_models(self):
            return [
                ModelDescriptor(provider_id="p", model_id="known", input_token_limit=1000),
                ModelDescriptor(provider_id="p", model_id="unknown"),
            ]

    snapshot = load_or_freeze_input_context_budgets(
        repo=repo,
        adapters={"p": Adapter()},
        safety_fraction=0.80,
        fallback_tokens=256,
    )

    assert input_context_budget_map(snapshot) == {
        ("p", "known"): 800,
        ("p", "unknown"): 256,
    }
    assert snapshot.estimator == "CEIL_UTF8_BYTES_DIVIDED_BY_3_PLUS_512"
    assert (repo.root / "governance_private/model_input_context_budgets.json").exists()


def test_configured_model_limit_upgrades_old_fallback_without_rewriting_snapshot(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("deepseek", "deepseek-flash")],
        chair_model=("deepseek", "deepseek-flash"),
        governance_docs=gov,
    )

    class Adapter:
        def list_models(self):
            return [ModelDescriptor(provider_id="deepseek", model_id="deepseek-flash")]

    frozen = load_or_freeze_input_context_budgets(
        repo=repo,
        adapters={"deepseek": Adapter()},
        safety_fraction=0.80,
        fallback_tokens=256,
    )
    assert input_context_budget_map(frozen) == {("deepseek", "deepseek-flash"): 256}

    upgraded = load_or_freeze_input_context_budgets(
        repo=repo,
        adapters={"deepseek": Adapter()},
        safety_fraction=0.80,
        fallback_tokens=256,
        configured_model_limits={("deepseek", "deepseek-flash"): 1000},
    )
    assert input_context_budget_map(upgraded) == {("deepseek", "deepseek-flash"): 800}
    assert upgraded.models[0].basis == "CONFIGURED_MODEL_INPUT_CONTEXT_FRACTION"

    persisted_frozen = json.loads(
        (repo.root / "governance_private/model_input_context_budgets.json").read_text()
    )
    assert persisted_frozen["models"][0]["maximum_input_tokens"] == 256
    upgrade_path = repo.root / "governance_private/model_input_context_budget_upgrades.json"
    assert json.loads(upgrade_path.read_text())["models"][0]["maximum_input_tokens"] == 800
    assert "MODEL_INPUT_CONTEXT_BUDGETS_UPGRADED" in repo.events.path.read_text()

    # The upgrade itself is durable: later config drift cannot silently alter
    # the acceptance criterion for this meeting.
    resumed = load_or_freeze_input_context_budgets(
        repo=repo,
        adapters={"deepseek": Adapter()},
        safety_fraction=0.80,
        fallback_tokens=256,
        configured_model_limits={("deepseek", "deepseek-flash"): 2000},
    )
    assert input_context_budget_map(resumed) == {("deepseek", "deepseek-flash"): 800}


def test_resume_budgets_ignore_superseded_replacement_providers(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("codex", "original")],
        chair_model=("codex", "original"),
        governance_docs=gov,
    )
    replacements = ModelReplacementService(repo)
    replacements.replace(
        participant_id="CHAIR", provider_id="kimi", model_id="intermediate",
        reason="temporary replacement",
    )
    replacements.replace(
        participant_id="CHAIR", provider_id="lithos", model_id="active",
        reason="later replacement",
    )

    class ActiveAdapter:
        def list_models(self):
            return [ModelDescriptor(provider_id="lithos", model_id="active", input_token_limit=1000)]

    adapters = {"lithos": ActiveAdapter()}
    output_budgets = add_replacement_output_budgets(
        repo=repo,
        adapters=adapters,
        budgets={("codex", "original"): 500},
        context_fraction=0.80,
        fallback_tokens=256,
    )
    input_budgets = add_replacement_input_context_budgets(
        repo=repo,
        adapters=adapters,
        budgets={("codex", "original"): 800},
        safety_fraction=0.80,
        fallback_tokens=256,
    )

    assert output_budgets == {("codex", "original"): 500, ("lithos", "active"): 800}
    assert input_budgets == {("codex", "original"): 800, ("lithos", "active"): 800}


def test_later_model_metadata_is_added_as_an_immutable_incremental_upgrade(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("deepseek", "flash"), ("glm", "glm-5.3-flashx")],
        chair_model=("deepseek", "flash"),
        governance_docs=gov,
    )

    class DeepSeekAdapter:
        def list_models(self):
            return [ModelDescriptor(provider_id="deepseek", model_id="flash")]

    class GlmAdapter:
        def list_models(self):
            return [ModelDescriptor(provider_id="glm", model_id="glm-5.3-flashx")]

    adapters = {"deepseek": DeepSeekAdapter(), "glm": GlmAdapter()}
    frozen = load_or_freeze_input_context_budgets(
        repo=repo,
        adapters=adapters,
        safety_fraction=0.80,
        fallback_tokens=256,
    )
    assert set(input_context_budget_map(frozen).values()) == {256}

    first = load_or_freeze_input_context_budgets(
        repo=repo,
        adapters=adapters,
        safety_fraction=0.80,
        fallback_tokens=256,
        configured_model_limits={("deepseek", "flash"): 1000},
    )
    assert input_context_budget_map(first) == {
        ("deepseek", "flash"): 800,
        ("glm", "glm-5.3-flashx"): 256,
    }

    second = load_or_freeze_input_context_budgets(
        repo=repo,
        adapters=adapters,
        safety_fraction=0.80,
        fallback_tokens=256,
        configured_model_limits={
            ("deepseek", "flash"): 1000,
            ("glm", "glm-5.3-flashx"): 2000,
        },
    )
    assert input_context_budget_map(second) == {
        ("deepseek", "flash"): 800,
        ("glm", "glm-5.3-flashx"): 1600,
    }
    incremental = (
        repo.root / "governance_private/model_input_context_budget_upgrades_v2.json"
    )
    assert incremental.is_file()
    assert json.loads(incremental.read_text())["models"][0]["model_id"] == "glm-5.3-flashx"
    assert repo.events.verify()
