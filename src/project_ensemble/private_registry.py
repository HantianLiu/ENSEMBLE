from __future__ import annotations

from project_ensemble.domain import (
    Persona,
    PrivateAuditMember,
    PrivateRepresentative,
    RenderingRole,
    RuntimeConfig,
)
from project_ensemble.ids import audit_member_ids, representative_ids


def build_private_registry(
    selected_models: list[tuple[str, str]],
    personas: list[Persona] | None = None,
    *,
    reasoning_setting: str | None = None,
    reasoning_settings: dict[tuple[str, str], str] | None = None,
) -> list[PrivateRepresentative]:
    personas = (
        [
            Persona.SYSTEMS_INTEGRATOR,
            Persona.PRAGMATIC_MINIMALIST,
            Persona.EXPLORATORY_SYNTHESIST,
            Persona.LIBRARIAN,
        ]
        if personas is None
        else personas
    )
    configs = [
        RuntimeConfig(
            provider_id=provider,
            model_id=model,
            persona=persona,
            reasoning_setting=(
                reasoning_settings.get((provider, model), reasoning_setting)
                if reasoning_settings is not None
                else reasoning_setting
            ),
        )
        for provider, model in selected_models
        for persona in personas
    ]
    ids = representative_ids(len(configs))
    return [PrivateRepresentative(representative_id=rid, runtime=cfg) for rid, cfg in zip(ids, configs, strict=True)]


def build_private_audit_registry(
    selected_models: list[tuple[str, str]],
    *,
    reasoning_setting: str | None = None,
    reasoning_settings: dict[tuple[str, str], str] | None = None,
) -> list[PrivateAuditMember]:
    """Create one fresh Audit Member per selected base model."""
    ids = audit_member_ids(len(selected_models))
    return [
        PrivateAuditMember(
            audit_member_id=member_id,
            provider_id=provider,
            model_id=model,
            reasoning_setting=(
                reasoning_settings.get((provider, model), reasoning_setting)
                if reasoning_settings is not None
                else reasoning_setting
            ),
        )
        for member_id, (provider, model) in zip(ids, selected_models, strict=True)
    ]


def build_private_rendering_registry(
    science_models: list[tuple[str, str]],
    citation_models: list[tuple[str, str]],
    *,
    reasoning_setting: str | None = None,
    reasoning_settings: dict[tuple[str, str], str] | None = None,
) -> list[PrivateRepresentative]:
    """Create one role-scoped reviewer for every selected model.

    Rendering reviewers are not the four deliberative offices.  They use the
    same private runtime envelope so invocation, replacement, telemetry, and
    file-access controls remain shared with the rest of ENSEMBLE.
    """

    assignments = [
        (model, RenderingRole.SCIENCE_BOOKKEEPER) for model in science_models
    ] + [
        (model, RenderingRole.CITATION_BOOKKEEPER) for model in citation_models
    ]
    ids = representative_ids(len(assignments))
    records: list[PrivateRepresentative] = []
    for representative_id, ((provider, model), persona) in zip(
        ids, assignments, strict=True
    ):
        records.append(
            PrivateRepresentative(
                representative_id=representative_id,
                runtime=RuntimeConfig(
                    provider_id=provider,
                    model_id=model,
                    rendering_role=persona,
                    reasoning_setting=(
                        reasoning_settings.get((provider, model), reasoning_setting)
                        if reasoning_settings is not None
                        else reasoning_setting
                    ),
                ),
            )
        )
    return records
