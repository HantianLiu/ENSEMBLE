from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from project_ensemble.domain import ModelDescriptor
from project_ensemble.providers.base import ProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.runtime.model_replacements import replacement_model_pairs
from project_ensemble.runtime.research_fallbacks import ResearchFallbacks
from project_ensemble.runtime.model_fallback_order import ModelFallbackOrderService


class ModelTokenBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_id: str
    model_id: str
    advertised_input_context_tokens: int | None = None
    advertised_output_limit_tokens: int | None = None
    target_context_fraction: float = Field(gt=0, le=1)
    requested_output_tokens: int = Field(gt=0)
    basis: Literal[
        "ADVERTISED_CONTEXT_FRACTION",
        "ADVERTISED_CONTEXT_FRACTION_CAPPED_BY_OUTPUT_LIMIT",
        "CONFIGURED_FALLBACK_NO_CONTEXT_METADATA",
    ]


class MeetingTokenBudgetSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_context_fraction: float = Field(gt=0, le=1)
    configured_fallback_tokens: int = Field(gt=0)
    models: list[ModelTokenBudget]


class ModelInputContextBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_id: str
    model_id: str
    advertised_input_context_tokens: int | None = None
    configured_input_context_tokens: int | None = None
    safety_fraction: float = Field(gt=0, le=1)
    maximum_input_tokens: int = Field(gt=0)
    basis: Literal[
        "ADVERTISED_INPUT_CONTEXT_FRACTION",
        "CONFIGURED_MODEL_INPUT_CONTEXT_FRACTION",
        "CONFIGURED_FALLBACK",
    ]


class MeetingInputContextBudgetSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    safety_fraction: float = Field(gt=0, le=1)
    configured_fallback_tokens: int = Field(gt=0)
    estimator: str
    models: list[ModelInputContextBudget]


def calculate_token_budget(
    descriptor: ModelDescriptor,
    *,
    context_fraction: float,
    fallback_tokens: int,
) -> ModelTokenBudget:
    if not 0 < context_fraction <= 1:
        raise ValueError("context_fraction must be in (0, 1]")
    if fallback_tokens <= 0:
        raise ValueError("fallback_tokens must be positive")

    if descriptor.input_token_limit is None:
        requested = fallback_tokens
        basis = "CONFIGURED_FALLBACK_NO_CONTEXT_METADATA"
    else:
        requested = max(1, math.floor(descriptor.input_token_limit * context_fraction))
        basis = "ADVERTISED_CONTEXT_FRACTION"
        if descriptor.output_token_limit is not None and requested > descriptor.output_token_limit:
            requested = descriptor.output_token_limit
            basis = "ADVERTISED_CONTEXT_FRACTION_CAPPED_BY_OUTPUT_LIMIT"

    return ModelTokenBudget(
        provider_id=descriptor.provider_id,
        model_id=descriptor.model_id,
        advertised_input_context_tokens=descriptor.input_token_limit,
        advertised_output_limit_tokens=descriptor.output_token_limit,
        target_context_fraction=context_fraction,
        requested_output_tokens=requested,
        basis=basis,
    )


def load_or_freeze_meeting_budgets(
    *,
    repo: MeetingRepository,
    adapters: dict[str, ProviderAdapter],
    context_fraction: float,
    fallback_tokens: int,
) -> MeetingTokenBudgetSnapshot:
    relative = Path("governance_private/model_token_budgets.json")
    absolute = repo.root / relative
    if absolute.exists():
        return MeetingTokenBudgetSnapshot.model_validate_json(absolute.read_text(encoding="utf-8"))

    manifest = json.loads(repo.docs.read_text("identity_private/meeting_manifest.json"))
    selected = {tuple(model) for model in manifest.get("selected_models", [])}
    if manifest.get("chair_model"):
        selected.add(tuple(manifest["chair_model"]))
    if manifest.get("research_enabled") and manifest.get("research_model"):
        selected.add(tuple(manifest["research_model"]))
    if manifest.get("technician_model"):
        selected.add(tuple(manifest["technician_model"]))
    descriptors: dict[tuple[str, str], ModelDescriptor] = {}
    for provider_id in sorted({provider_id for provider_id, _ in selected}):
        adapter = adapters.get(provider_id)
        if adapter is None:
            raise ValueError(f"no adapter configured for selected provider {provider_id}")
        for descriptor in adapter.list_models():
            key = (descriptor.provider_id, descriptor.model_id)
            if key in selected:
                descriptors[key] = descriptor

    missing = sorted(selected - set(descriptors))
    if missing:
        rendered = ", ".join(f"{provider}:{model}" for provider, model in missing)
        raise ValueError(f"selected models disappeared from live discovery: {rendered}")

    snapshot = MeetingTokenBudgetSnapshot(
        target_context_fraction=context_fraction,
        configured_fallback_tokens=fallback_tokens,
        models=[
            calculate_token_budget(
                descriptors[key],
                context_fraction=context_fraction,
                fallback_tokens=fallback_tokens,
            )
            for key in sorted(selected)
        ],
    )
    repo.docs.write_once(relative, snapshot.model_dump_json(indent=2))
    repo.events.append(
        "MODEL_TOKEN_BUDGETS_FROZEN",
        {
            "meeting_id": repo.meeting_id,
            "target_context_fraction": context_fraction,
            "configured_fallback_tokens": fallback_tokens,
            "record_path": str(relative),
        },
        actor="orchestrator",
    )
    return snapshot


def budget_map(snapshot: MeetingTokenBudgetSnapshot) -> dict[tuple[str, str], int]:
    return {
        (budget.provider_id, budget.model_id): budget.requested_output_tokens
        for budget in snapshot.models
    }


def add_replacement_output_budgets(
    *,
    repo: MeetingRepository,
    adapters: dict[str, ProviderAdapter],
    budgets: dict[tuple[str, str], int],
    context_fraction: float,
    fallback_tokens: int,
) -> dict[tuple[str, str], int]:
    """Resolve budgets for runtimes introduced after the immutable freeze.

    The original meeting snapshot remains immutable.  A replacement gets the
    same configured safety policy, resolved just for this resumed process.
    """

    result = dict(budgets)
    targets = (replacement_model_pairs(repo) | ResearchFallbacks(repo).target_models()
               | ModelFallbackOrderService(repo).target_models())
    for key in sorted(targets - set(result)):
        provider_id, model_id = key
        adapter = adapters.get(provider_id)
        if adapter is None:
            raise ValueError(f"no adapter is configured for replacement provider {provider_id}")
        descriptor = next(
            (item for item in adapter.list_models() if (item.provider_id, item.model_id) == key),
            None,
        )
        if descriptor is None:
            raise ValueError(f"replacement model disappeared from live discovery: {provider_id}:{model_id}")
        result[key] = calculate_token_budget(
            descriptor,
            context_fraction=context_fraction,
            fallback_tokens=fallback_tokens,
        ).requested_output_tokens
    return result


def add_replacement_input_context_budgets(
    *,
    repo: MeetingRepository,
    adapters: dict[str, ProviderAdapter],
    budgets: dict[tuple[str, str], int],
    safety_fraction: float,
    fallback_tokens: int,
    configured_model_limits: dict[tuple[str, str], int] | None = None,
) -> dict[tuple[str, str], int]:
    """Apply the frozen input safety rule to newly introduced runtimes."""

    result = dict(budgets)
    targets = (replacement_model_pairs(repo) | ResearchFallbacks(repo).target_models()
               | ModelFallbackOrderService(repo).target_models())
    for key in sorted(targets - set(result)):
        provider_id, model_id = key
        adapter = adapters.get(provider_id)
        if adapter is None:
            raise ValueError(f"no adapter is configured for replacement provider {provider_id}")
        descriptor = next(
            (item for item in adapter.list_models() if (item.provider_id, item.model_id) == key),
            None,
        )
        if descriptor is None:
            raise ValueError(f"replacement model disappeared from live discovery: {provider_id}:{model_id}")
        effective_limit = descriptor.input_token_limit
        if effective_limit is None:
            effective_limit = (configured_model_limits or {}).get(key)
        result[key] = (
            max(1, math.floor(effective_limit * safety_fraction))
            if effective_limit is not None
            else fallback_tokens
        )
    return result


def load_or_freeze_input_context_budgets(
    *,
    repo: MeetingRepository,
    adapters: dict[str, ProviderAdapter],
    safety_fraction: float,
    fallback_tokens: int,
    configured_model_limits: dict[tuple[str, str], int] | None = None,
) -> MeetingInputContextBudgetSnapshot:
    """Freeze per-model input safety ceilings independently of output policy."""

    if not 0 < safety_fraction <= 1:
        raise ValueError("safety_fraction must be in (0, 1]")
    if fallback_tokens <= 0:
        raise ValueError("fallback_tokens must be positive")
    relative = Path("governance_private/model_input_context_budgets.json")
    absolute = repo.root / relative
    if absolute.exists():
        frozen = MeetingInputContextBudgetSnapshot.model_validate_json(
            absolute.read_text(encoding="utf-8")
        )
        return _apply_configured_input_context_upgrades(
            repo=repo,
            snapshot=frozen,
            configured_model_limits=configured_model_limits or {},
        )

    manifest = json.loads(repo.docs.read_text("identity_private/meeting_manifest.json"))
    selected = {tuple(model) for model in manifest.get("selected_models", [])}
    if manifest.get("chair_model"):
        selected.add(tuple(manifest["chair_model"]))
    if manifest.get("research_enabled") and manifest.get("research_model"):
        selected.add(tuple(manifest["research_model"]))
    if manifest.get("technician_model"):
        selected.add(tuple(manifest["technician_model"]))
    descriptors: dict[tuple[str, str], ModelDescriptor] = {}
    for provider_id in sorted({provider_id for provider_id, _ in selected}):
        adapter = adapters.get(provider_id)
        if adapter is None:
            raise ValueError(f"no adapter configured for selected provider {provider_id}")
        for descriptor in adapter.list_models():
            key = (descriptor.provider_id, descriptor.model_id)
            if key in selected:
                descriptors[key] = descriptor
    missing = sorted(selected - set(descriptors))
    if missing:
        rendered = ", ".join(f"{provider}:{model}" for provider, model in missing)
        raise ValueError(f"selected models disappeared from live discovery: {rendered}")

    models: list[ModelInputContextBudget] = []
    for key in sorted(selected):
        descriptor = descriptors[key]
        configured_limit = (configured_model_limits or {}).get(key)
        if descriptor.input_token_limit is not None:
            maximum = max(1, math.floor(descriptor.input_token_limit * safety_fraction))
            basis = "ADVERTISED_INPUT_CONTEXT_FRACTION"
        elif configured_limit is not None:
            maximum = max(1, math.floor(configured_limit * safety_fraction))
            basis = "CONFIGURED_MODEL_INPUT_CONTEXT_FRACTION"
        else:
            maximum = fallback_tokens
            basis = "CONFIGURED_FALLBACK"
        models.append(
            ModelInputContextBudget(
                provider_id=descriptor.provider_id,
                model_id=descriptor.model_id,
                advertised_input_context_tokens=descriptor.input_token_limit,
                configured_input_context_tokens=(
                    configured_limit if descriptor.input_token_limit is None else None
                ),
                safety_fraction=safety_fraction,
                maximum_input_tokens=maximum,
                basis=basis,
            )
        )
    snapshot = MeetingInputContextBudgetSnapshot(
        safety_fraction=safety_fraction,
        configured_fallback_tokens=fallback_tokens,
        estimator="CEIL_UTF8_BYTES_DIVIDED_BY_3_PLUS_512",
        models=models,
    )
    repo.docs.write_once(relative, snapshot.model_dump_json(indent=2))
    repo.events.append(
        "MODEL_INPUT_CONTEXT_BUDGETS_FROZEN",
        {
            "meeting_id": repo.meeting_id,
            "safety_fraction": safety_fraction,
            "configured_fallback_tokens": fallback_tokens,
            "estimator": snapshot.estimator,
            "record_path": str(relative),
        },
        actor="orchestrator",
    )
    return snapshot


def _apply_configured_input_context_upgrades(
    *,
    repo: MeetingRepository,
    snapshot: MeetingInputContextBudgetSnapshot,
    configured_model_limits: dict[tuple[str, str], int],
) -> MeetingInputContextBudgetSnapshot:
    """Apply a durable metadata upgrade without rewriting the frozen snapshot.

    Older meetings may have frozen the generic fallback because a provider's
    model-list endpoint omitted context metadata.  Once a vendor-verified,
    model-specific capacity is configured, record a separate immutable upgrade
    and reuse it on every subsequent resume.
    """

    base = Path("governance_private/model_input_context_budget_upgrades.json")
    upgrade_paths = [base]
    upgrade_paths.extend(
        path.relative_to(repo.root)
        for path in sorted(
            (repo.root / "governance_private").glob(
                "model_input_context_budget_upgrades_v*.json"
            )
        )
    )
    recorded: dict[tuple[str, str], dict] = {}
    for relative in upgrade_paths:
        absolute = repo.root / relative
        if not absolute.exists():
            continue
        payload = json.loads(absolute.read_text(encoding="utf-8"))
        recorded.update(
            {
                (str(item["provider_id"]), str(item["model_id"])): item
                for item in payload.get("models", [])
            }
        )

    additions: dict[tuple[str, str], dict] = {}
    for budget in snapshot.models:
        key = (budget.provider_id, budget.model_id)
        configured_limit = configured_model_limits.get(key)
        if (
            configured_limit is None
            or budget.advertised_input_context_tokens is not None
            or key in recorded
        ):
            continue
        upgraded_maximum = max(
            1, math.floor(configured_limit * snapshot.safety_fraction)
        )
        if upgraded_maximum <= budget.maximum_input_tokens:
            continue
        additions[key] = {
            "provider_id": budget.provider_id,
            "model_id": budget.model_id,
            "previous_maximum_input_tokens": budget.maximum_input_tokens,
            "configured_input_context_tokens": configured_limit,
            "safety_fraction": snapshot.safety_fraction,
            "maximum_input_tokens": upgraded_maximum,
            "basis": "CONFIGURED_MODEL_INPUT_CONTEXT_FRACTION",
        }
    if additions:
        if not (repo.root / base).exists():
            relative = base
        else:
            version = 2
            while (
                repo.root
                / f"governance_private/model_input_context_budget_upgrades_v{version}.json"
            ).exists():
                version += 1
            relative = Path(f"governance_private/model_input_context_budget_upgrades_v{version}.json")
        payload = {
            "meeting_id": repo.meeting_id,
            "source_snapshot_path": "governance_private/model_input_context_budgets.json",
            "models": [additions[key] for key in sorted(additions)],
        }
        repo.docs.write_once(relative, json.dumps(payload, indent=2, ensure_ascii=False))
        repo.events.append(
            "MODEL_INPUT_CONTEXT_BUDGETS_UPGRADED",
            {
                "meeting_id": repo.meeting_id,
                "model_count": len(additions),
                "record_path": str(relative),
                "reason": "vendor-verified model context metadata configured after initial freeze",
            },
            actor="orchestrator",
        )
        recorded.update(additions)

    if not recorded:
        return snapshot
    upgraded_models: list[ModelInputContextBudget] = []
    for budget in snapshot.models:
        item = recorded.get((budget.provider_id, budget.model_id))
        if item is None:
            upgraded_models.append(budget)
            continue
        upgraded_models.append(
            budget.model_copy(
                update={
                    "configured_input_context_tokens": int(
                        item["configured_input_context_tokens"]
                    ),
                    "maximum_input_tokens": int(item["maximum_input_tokens"]),
                    "basis": "CONFIGURED_MODEL_INPUT_CONTEXT_FRACTION",
                }
            )
        )
    return snapshot.model_copy(update={"models": upgraded_models})


def input_context_budget_map(
    snapshot: MeetingInputContextBudgetSnapshot,
) -> dict[tuple[str, str], int]:
    return {
        (budget.provider_id, budget.model_id): budget.maximum_input_tokens
        for budget in snapshot.models
    }
