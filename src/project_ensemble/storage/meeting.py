from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import fcntl
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

from project_ensemble.domain import (
    DeliverableType,
    DecisionRigor,
    InheritanceMode,
    MeetingType,
    Persona,
    ReasoningEffort,
    RenderingRole,
)
from project_ensemble.ids import meeting_id, meeting_id_prefix
from project_ensemble.private_registry import (
    build_private_audit_registry,
    build_private_registry,
    build_private_rendering_registry,
)
from project_ensemble.storage.documents import ImmutableDocumentStore
from project_ensemble.storage.events import HashChainEventLog
from project_ensemble.storage.human_outputs import ensure_visible_link
from project_ensemble import GOVERNANCE_VERSION, __version__
from project_ensemble.runtime.prompt_contract import (
    CURRENT_PROMPT_CONTRACT_VERSION, CURRENT_CONTEXT_ASSEMBLY_VERSION,
)


class PublicMeetingManifest(BaseModel):
    meeting_id: str
    title: str = "未命名会议"
    created_at: str
    meeting_type: MeetingType = MeetingType.DELIBERATION
    participant_count: int
    representative_count: int | None = None
    audit_member_count: int | None = None
    research_enabled: bool = False
    general_search_allowed: bool = True
    institutional_access_allowed: bool = False
    academic_search_engine: Literal["openalex"] = "openalex"
    general_search_engine: Literal["tavily", "parallel", "disabled"] | None = None
    planning_exploration_enabled: bool = False
    # A missing field keeps meetings created before this policy on their frozen flow.
    planning_replan_reference_enabled: bool = False
    decision_rigor: DecisionRigor = DecisionRigor.STRICT
    deliverable_type: DeliverableType = DeliverableType.NORMATIVE_INSTRUMENT
    parent_meeting_id: str | None = None
    rendering_science_consultation_authority: Literal["human", "chair"] = "human"
    # Absent in existing meetings: do not retroactively open a new query window.
    rendering_final_science_query_limit: int | None = Field(default=None, ge=0, le=4)
    rendering_target_body_characters: int | None = Field(default=None, ge=1000)
    rendering_scope_description: str | None = None
    literature_writing_policy: Literal["v071", "fast"] | None = None


class PublicTask(BaseModel):
    meeting_id: str
    title: str = "未命名会议"
    meeting_type: MeetingType
    description: str
    deliverable_type: DeliverableType = DeliverableType.NORMATIVE_INSTRUMENT


class EscalationContact(BaseModel):
    meeting_id: str
    email: str


class SessionConfigurationReference(BaseModel):
    meeting_id: str
    config_path: str
    config_sha256: str
    snapshot_path: str
    governance_docs_path: str | None = None
    governance_snapshot_path: str | None = None
    model_config_path: str | None = None
    model_config_sha256: str | None = None
    model_config_snapshot_path: str | None = None


class PrivateMeetingManifest(BaseModel):
    meeting_id: str
    title: str = "未命名会议"
    created_at: str
    selected_models: list[tuple[str, str]]
    chair_model: tuple[str, str] | None
    meeting_type: MeetingType = MeetingType.DELIBERATION
    personas: list[str]
    governance_digest: str
    software_version: str = __version__
    governance_version: str = GOVERNANCE_VERSION
    # Missing fields retain the original prompt/context assembly contract.
    prompt_contract_version: int = Field(default=1, ge=1, le=3)
    context_assembly_version: int = Field(default=1, ge=1, le=3)
    evidence_read_protocol_version: int = Field(default=0, ge=0, le=1)
    evidence_read_round_limit: int = Field(default=8, ge=1, le=32)
    representative_prompt_family: Literal[
        "legacy_shared", "deliberation", "literature_research"
    ] = "legacy_shared"
    representative_reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT
    representative_reasoning_effective: dict[str, ReasoningEffort] = Field(default_factory=dict)
    chair_reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT
    research_enabled: bool = False
    general_search_allowed: bool = True
    institutional_access_allowed: bool = False
    academic_search_engine: Literal["openalex"] = "openalex"
    general_search_engine: Literal["tavily", "parallel", "disabled"] | None = None
    # Frozen per meeting: old v0.7 meetings retain their original planning flow.
    planning_exploration_enabled: bool = False
    decision_rigor: DecisionRigor = DecisionRigor.STRICT
    research_model: tuple[str, str] | None = None
    research_reasoning_effort: ReasoningEffort | None = None
    openalex_max_results_per_query: int | None = Field(default=None, ge=1, le=50)
    openalex_quota_policy: Literal["wait", "tavily", "parallel"] | None = None
    research_max_concurrent_claim_groups: int | None = Field(default=None, ge=1)
    model_concurrency_limits: dict[str, int] = Field(default_factory=dict)
    model_concurrency_sources: dict[str, str] = Field(default_factory=dict)
    maximum_parallelism: bool = False
    deliverable_type: DeliverableType = DeliverableType.NORMATIVE_INSTRUMENT
    parent_meeting_id: str | None = None
    parent_meeting_path: str | None = None
    inheritance_mode: InheritanceMode | None = None
    rendering_provisional_source: bool = False
    rendering_science_models: list[tuple[str, str]] = Field(default_factory=list)
    rendering_citation_models: list[tuple[str, str]] = Field(default_factory=list)
    rendering_language: str | None = None
    deliberation_language: Literal["task", "zh", "en", "fr"] | None = None
    rendering_academic_skeleton: bool | None = None
    rendering_full_abstract: bool | None = None
    rendering_section_abstracts: bool | None = None
    rendering_segmentation: int | None = Field(default=None, ge=1, le=5)
    rendering_liveliness: int | None = Field(default=None, ge=1, le=5)
    rendering_output_formats: list[str] = Field(default_factory=list)
    rendering_science_order: list[str] = Field(default_factory=list)
    rendering_science_consultation_authority: Literal["human", "chair"] = "human"
    rendering_final_science_query_limit: int | None = Field(default=None, ge=0, le=4)
    rendering_target_body_characters: int | None = Field(default=None, ge=1000)
    rendering_scope_description: str | None = None
    literature_writing_policy: Literal["v071", "fast"] | None = None
    writer_model: tuple[str, str] | None = None
    writer_reasoning_effort: ReasoningEffort | None = None
    fast_planner_models: list[tuple[str, str]] = Field(default_factory=list)
    fast_planner_reasoning_effective: dict[str, ReasoningEffort] = Field(default_factory=dict)
    technician_model: tuple[str, str] | None = None
    technician_reasoning_effort: ReasoningEffort | None = None


def directory_digest(root: str | Path) -> str:
    root = Path(root)
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = str(p.relative_to(root)).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _configured_model_file(config_source: Path, config_bytes: bytes) -> Path | None:
    data = tomllib.loads(config_bytes.decode("utf-8"))
    configured = (data.get("project") or {}).get("model_config_file")
    if not configured:
        return None
    path = Path(str(configured)).expanduser()
    if not path.is_absolute():
        path = config_source.parent / path
    return path.resolve()


def _write_prompt_and_lineage(
    repo: "MeetingRepository",
    *,
    task_description: str,
    title: str,
    meeting_type: MeetingType,
    parent_meeting_id: str | None,
    parent_meeting_path: Path | None,
    inheritance_mode: InheritanceMode | None,
) -> None:
    """Freeze the Human's original prompt and a portable meeting lineage ledger."""

    repo.docs.write_once("original_prompt.txt", task_description.encode("utf-8"))
    ancestors: list[dict] = []
    if parent_meeting_path is not None:
        parent_root = parent_meeting_path.expanduser().resolve()
        parent_manifest = json.loads(
            (parent_root / "public/meeting_manifest.json").read_text(encoding="utf-8")
        )
        if str(parent_manifest.get("meeting_id")) != str(parent_meeting_id):
            raise ValueError("parent meeting ID does not match meeting lineage source")
        parent_prompt_path = parent_root / "original_prompt.txt"
        if parent_prompt_path.is_file():
            parent_prompt = parent_prompt_path.read_bytes()
        else:
            parent_task_path = parent_root / "public/task.json"
            parent_task = (
                json.loads(parent_task_path.read_text(encoding="utf-8"))
                if parent_task_path.is_file()
                else {}
            )
            parent_prompt = str(parent_task.get("description", "")).encode("utf-8")
        repo.docs.write_once("parent_prompt.txt", parent_prompt)
        parent_lineage_path = parent_root / "meeting_lineage.json"
        if parent_lineage_path.is_file():
            parent_lineage = json.loads(
                parent_lineage_path.read_text(encoding="utf-8")
            )
            ancestors = list(parent_lineage.get("meetings", []))
        else:
            ancestors = [
                {
                    "meeting_id": str(parent_meeting_id),
                    "title": str(parent_manifest.get("title", "未命名会议")),
                    "meeting_type": str(
                        parent_manifest.get("meeting_type", MeetingType.DELIBERATION.value)
                    ),
                    "parent_meeting_id": parent_manifest.get("parent_meeting_id"),
                    "inherited_material_categories": [],
                }
            ]

    inherited_categories: list[str] = []
    if inheritance_mode in {InheritanceMode.EVIDENCE, InheritanceMode.BOTH}:
        inherited_categories.append("EVIDENCE")
    if inheritance_mode in {InheritanceMode.FINAL_DOCUMENT, InheritanceMode.BOTH}:
        inherited_categories.append("FINAL_DOCUMENT")
    meetings = [
        *ancestors,
        {
            "meeting_id": repo.meeting_id,
            "title": title,
            "meeting_type": meeting_type.value,
            "parent_meeting_id": parent_meeting_id,
            "inherited_material_categories": inherited_categories,
        },
    ]
    ids = [item.get("meeting_id") for item in meetings]
    if len(ids) != len(set(ids)):
        raise ValueError("meeting lineage contains a cycle or duplicate meeting ID")
    repo.docs.write_once(
        "meeting_lineage.json",
        json.dumps(
            {"schema_version": 1, "meetings": meetings},
            indent=2,
            ensure_ascii=False,
        ),
    )


class MeetingRepository:
    """File-backed meeting workspace with explicit confidentiality compartments."""

    COMPARTMENTS = (
        "public",
        "representatives",
        "chair_private",
        "governance_private",
        "identity_private",
        "human_private",
        "think_tank_private",
        "audit_private",
    )

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.docs = ImmutableDocumentStore(self.root)
        self.events = HashChainEventLog(self.root / "governance_private" / "events.jsonl")

    @classmethod
    def create(
        cls,
        workspace_root: str | Path,
        *,
        selected_models: list[tuple[str, str]],
        chair_model: tuple[str, str] | None,
        governance_docs: str | Path,
        meeting_type: MeetingType = MeetingType.DELIBERATION,
        task_description: str | None = None,
        escalation_email: str | None = None,
        config_path: str | Path | None = None,
        personas: list[Persona] | None = None,
        representative_reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT,
        representative_reasoning_effective: dict[tuple[str, str], ReasoningEffort] | None = None,
        chair_reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT,
        research_enabled: bool = False,
        general_search_allowed: bool = True,
        institutional_access_allowed: bool = False,
        academic_search_engine: Literal["openalex"] = "openalex",
        general_search_engine: Literal["tavily", "parallel", "disabled"] | None = None,
        research_model: tuple[str, str] | None = None,
        research_reasoning_effort: ReasoningEffort | None = None,
        openalex_max_results_per_query: int | None = None,
        openalex_quota_policy: Literal["wait", "tavily", "parallel"] | None = None,
        research_max_concurrent_claim_groups: int | None = None,
        model_concurrency_limits: dict[tuple[str, str], int] | None = None,
        model_concurrency_sources: dict[tuple[str, str], str] | None = None,
        maximum_parallelism: bool = False,
        decision_rigor: DecisionRigor = DecisionRigor.STRICT,
        forced_meeting_id: str | None = None,
        deliverable_type: DeliverableType = DeliverableType.NORMATIVE_INSTRUMENT,
        meeting_title: str = "未命名会议",
        parent_meeting_id: str | None = None,
        parent_meeting_path: str | Path | None = None,
        inheritance_mode: InheritanceMode | None = None,
        rendering_provisional_source: bool = False,
        rendering_science_models: list[tuple[str, str]] | None = None,
        rendering_citation_models: list[tuple[str, str]] | None = None,
        rendering_language: str | None = None,
        deliberation_language: Literal["task", "zh", "en", "fr"] | None = None,
        rendering_academic_skeleton: bool | None = None,
        rendering_full_abstract: bool | None = None,
        rendering_section_abstracts: bool | None = None,
        rendering_segmentation: int | None = None,
        rendering_liveliness: int | None = None,
        rendering_output_formats: list[str] | None = None,
        rendering_science_order: list[str] | None = None,
        rendering_science_consultation_authority: Literal["human", "chair"] = "human",
        rendering_target_body_characters: int | None = None,
        rendering_scope_description: str | None = None,
        literature_writing_policy: Literal["v071", "fast"] | None = None,
        writer_model: tuple[str, str] | None = None,
        writer_reasoning_effort: ReasoningEffort | None = None,
        fast_planner_models: list[tuple[str, str]] | None = None,
        fast_planner_reasoning_effective: dict[str, ReasoningEffort] | None = None,
        technician_model: tuple[str, str] | None = None,
        technician_reasoning_effort: ReasoningEffort | None = None,
        evidence_read_round_limit: int = 8,
    ) -> "MeetingRepository":
        if type(evidence_read_round_limit) is not int or not 1 <= evidence_read_round_limit <= 32:
            raise ValueError("evidence read technical window must be between 1 and 32 turns")
        if (technician_model is None) != (technician_reasoning_effort is None):
            raise ValueError("Technician model and reasoning effort must be configured together")
        if rendering_science_consultation_authority == "chair" and meeting_type != MeetingType.SCHOLARLY_RENDERING:
            raise ValueError("Chair science consultation delegation is only valid for scholarly rendering")
        if rendering_target_body_characters is not None and (
            meeting_type != MeetingType.SCHOLARLY_RENDERING or rendering_target_body_characters < 1000
        ):
            raise ValueError("rendering body target requires scholarly rendering and at least 1000 characters")
        if rendering_scope_description is not None and (
            meeting_type != MeetingType.SCHOLARLY_RENDERING or not rendering_scope_description.strip()
        ):
            raise ValueError("partial rendering scope requires a non-empty scholarly rendering instruction")
        if literature_writing_policy is not None:
            if (deliverable_type != DeliverableType.LITERATURE_REVIEW
                    or writer_model is None or writer_reasoning_effort is None):
                raise ValueError("v0.7.1 literature writing requires a dedicated Writer model and effort")
        if research_enabled and (research_model is None or research_reasoning_effort is None):
            raise ValueError("enabled Research Desk requires a model and reasoning effort")
        if not research_enabled and (research_model is not None or research_reasoning_effort is not None):
            raise ValueError("disabled Research Desk cannot have runtime settings")
        if openalex_max_results_per_query is not None:
            if not research_enabled:
                raise ValueError("disabled Research Desk cannot have an OpenAlex result limit")
            if not 1 <= openalex_max_results_per_query <= 50:
                raise ValueError("OpenAlex results per query must be between 1 and 50")
        if openalex_quota_policy is not None and not research_enabled:
            raise ValueError("OpenAlex quota policy requires Research Desk")
        if not isinstance(general_search_allowed, bool):
            raise ValueError("meeting general-search permission must be boolean")
        if type(institutional_access_allowed) is not bool:
            raise ValueError("institutional access requires an explicit boolean authorization")
        if academic_search_engine != "openalex":
            raise ValueError("academic search currently supports OpenAlex only")
        if general_search_engine not in {None, "tavily", "parallel", "disabled"}:
            raise ValueError("unknown general search engine")
        if general_search_engine == "disabled":
            general_search_allowed = False
        if not general_search_allowed and openalex_quota_policy in {"tavily", "parallel"}:
            raise ValueError("本会议禁止通用搜索，不能预授权 Tavily 额度回退")
        if research_max_concurrent_claim_groups is not None:
            if not research_enabled or meeting_type != MeetingType.DELIBERATION:
                raise ValueError("independent Research Desk parallelism requires a deliberation Research Desk")
            if research_max_concurrent_claim_groups < 1:
                raise ValueError("Research Desk independent-task parallelism must be positive")
        if decision_rigor == DecisionRigor.RELAXED and meeting_type != MeetingType.DELIBERATION:
            raise ValueError("relaxed decision rigor is only available for deliberation meetings")
        science_models = list(rendering_science_models or ())
        citation_models = list(rendering_citation_models or ())
        if rendering_provisional_source and meeting_type != MeetingType.SCHOLARLY_RENDERING:
            raise ValueError("provisional rendering sources are only available to scholarly rendering")
        if meeting_type == MeetingType.RESEARCH:
            if not research_enabled or research_model is None:
                raise ValueError("research-only meetings require an enabled Research Desk")
            if selected_models or chair_model is not None:
                raise ValueError("research-only meetings do not create Representatives or a Chair")
        elif meeting_type == MeetingType.SCHOLARLY_RENDERING:
            if chair_model is None:
                raise ValueError("scholarly-rendering meetings require a Chair/renderer model")
            if deliverable_type != DeliverableType.SCHOLARLY_RENDERING:
                raise ValueError("scholarly-rendering meetings require the scholarly_rendering deliverable")
            if len(science_models) < 2 or len(citation_models) < 2:
                raise ValueError("scholarly rendering requires at least two science and two citation models")
            if len(set(science_models)) != len(science_models):
                raise ValueError("science-bookkeeping models must be unique")
            if len(set(citation_models)) != len(citation_models):
                raise ValueError("citation-bookkeeping models must be unique")
            if not research_enabled:
                raise ValueError("scholarly rendering requires an enabled Research Desk")
            if parent_meeting_path is None:
                raise ValueError("scholarly rendering requires a completed parent report")
            if inheritance_mode != InheritanceMode.BOTH:
                raise ValueError("scholarly rendering must inherit both report and evidence")
            parent_root = Path(parent_meeting_path).expanduser()
            source_report = parent_root / "public/final/literature_review_report.md"
            rendered_report = parent_root / "public/final/scholarly_rendering/scholarly_review.md"
            if rendering_provisional_source:
                provisional_rendering_text(parent_root)
                if source_report.is_file():
                    raise ValueError("a completed report must use the ordinary rendering transfer")
            elif not (source_report.is_file() or rendered_report.is_file()):
                raise ValueError("scholarly rendering parent has no completed Markdown report")
            parent_public_manifest = json.loads(
                (parent_root / "public/meeting_manifest.json").read_text(encoding="utf-8")
            )
            parent_deliverable = parent_public_manifest.get("deliverable_type")
            if parent_deliverable not in {None, DeliverableType.LITERATURE_REVIEW.value,
                                          DeliverableType.SCHOLARLY_RENDERING.value}:
                raise ValueError("scholarly rendering parent must be literature_review or scholarly_rendering")
            if rendering_language not in {"zh", "en", "fr"}:
                raise ValueError("rendering language must be zh, en, or fr")
            formats = list(rendering_output_formats or ())
            if not formats or not set(formats) <= {"md", "latex", "pdf"}:
                raise ValueError("rendering output formats must select md, latex, and/or pdf")
            if len(formats) != len(set(formats)):
                raise ValueError("rendering output formats must be unique")
            expected_order = {f"{provider}:{model}" for provider, model in science_models}
            frozen_order = list(rendering_science_order or ())
            if len(frozen_order) != len(expected_order) or set(frozen_order) != expected_order:
                raise ValueError("science-review order must list every science model exactly once")
        elif chair_model is None and literature_writing_policy != "fast":
            raise ValueError("deliberation and audit meetings require a Chair model")
        if literature_writing_policy == "fast":
            if meeting_type != MeetingType.DELIBERATION or chair_model is not None:
                raise ValueError("fast literature meetings must have no Chair")
            if len(selected_models) < 2 or len(set(selected_models)) != len(selected_models):
                raise ValueError("fast literature meetings need at least two distinct reviewer models")
            if all(model == writer_model for model in selected_models):
                raise ValueError("at least one reviewer model must differ from the Writer")
        if fast_planner_models:
            if literature_writing_policy != "fast" or not 2 <= len(fast_planner_models) <= 3:
                raise ValueError("fast split proposers require two or three models in fast mode")
            if len(set(fast_planner_models)) != len(fast_planner_models):
                raise ValueError("fast split-proposal models must be distinct")
        if (parent_meeting_id is None) != (parent_meeting_path is None):
            raise ValueError("parent meeting ID and path must be supplied together")
        if parent_meeting_path is None and inheritance_mode is not None:
            raise ValueError("inheritance mode requires a parent meeting")
        if parent_meeting_path is not None and inheritance_mode is None:
            # Compatibility for callers created before inheritance became an
            # explicit Human choice: the legacy behavior copied both classes.
            inheritance_mode = InheritanceMode.BOTH
        if deliverable_type == DeliverableType.LITERATURE_REVIEW:
            if meeting_type != MeetingType.DELIBERATION:
                raise ValueError("literature-review deliverables require deliberation procedure")
            if not research_enabled:
                raise ValueError("literature-review deliverables require an enabled Research Desk")
        if deliverable_type == DeliverableType.SCHOLARLY_RENDERING and meeting_type != MeetingType.SCHOLARLY_RENDERING:
            raise ValueError("scholarly-rendering deliverables require scholarly_rendering procedure")
        normalized_title = " ".join(meeting_title.split())
        if not normalized_title:
            raise ValueError("meeting title cannot be empty")
        mid = forced_meeting_id or meeting_id(
            prefix=meeting_id_prefix(meeting_type, deliverable_type)
        )
        workspace = Path(workspace_root)
        workspace.mkdir(parents=True, exist_ok=True)
        root = workspace / mid
        if root.exists():
            raise FileExistsError(f"meeting workspace already exists: {root}")
        staging = workspace / f".{mid}.creating-{secrets.token_hex(4)}"
        staging.mkdir(mode=0o700)
        try:
            for name in cls.COMPARTMENTS:
                (staging / name).mkdir(mode=0o700)

            configured_personas = (
                [
                    Persona.SYSTEMS_INTEGRATOR,
                    Persona.PRAGMATIC_MINIMALIST,
                    Persona.EXPLORATORY_SYNTHESIST,
                    Persona.LIBRARIAN,
                ]
                if personas is None
                else personas
            )
            if meeting_type == MeetingType.DELIBERATION:
                registry = build_private_registry(
                    selected_models,
                    configured_personas,
                    reasoning_setting=representative_reasoning_effort.value,
                    reasoning_settings={
                        key: value.value
                        for key, value in (representative_reasoning_effective or {}).items()
                    },
                )
                public_participants = [
                    {"representative_id": r.representative_id, "status": r.status.value}
                    for r in registry
                ]
                private_registry = [r.model_dump(mode="json") for r in registry]
                public_registry_name = "public/representatives.json"
                private_registry_name = "identity_private/representative_registry.json"
                for r in registry:
                    (staging / "representatives" / r.representative_id).mkdir(mode=0o700)
            elif meeting_type == MeetingType.AUDIT:
                audit_registry = build_private_audit_registry(
                    selected_models,
                    reasoning_setting=representative_reasoning_effort.value,
                    reasoning_settings={
                        key: value.value
                        for key, value in (representative_reasoning_effective or {}).items()
                    },
                )
                public_participants = [{"audit_member_id": x.audit_member_id} for x in audit_registry]
                private_registry = [x.model_dump(mode="json") for x in audit_registry]
                public_registry_name = "public/audit_members.json"
                private_registry_name = "identity_private/audit_member_registry.json"
            elif meeting_type == MeetingType.SCHOLARLY_RENDERING:
                registry = build_private_rendering_registry(
                    science_models,
                    citation_models,
                    reasoning_setting=representative_reasoning_effort.value,
                    reasoning_settings={
                        key: value.value
                        for key, value in (representative_reasoning_effective or {}).items()
                    },
                )
                public_participants = [
                    {
                        "representative_id": record.representative_id,
                        "review_role": record.runtime.rendering_role.value,
                        "status": record.status.value,
                    }
                    for record in registry
                ]
                private_registry = [record.model_dump(mode="json") for record in registry]
                public_registry_name = "public/rendering_reviewers.json"
                private_registry_name = "identity_private/representative_registry.json"
                for record in registry:
                    (staging / "representatives" / record.representative_id).mkdir(mode=0o700)
            else:
                public_participants = []
                private_registry = []
                public_registry_name = "public/research_participants.json"
                private_registry_name = "identity_private/research_participants.json"

            repo = cls(staging)
            from project_ensemble.storage.governance_snapshot import (
                GOVERNANCE_SNAPSHOT, freeze_governance_docs,
            )

            frozen_governance = freeze_governance_docs(
                governance_docs, staging / GOVERNANCE_SNAPSHOT,
            )
            created_at = datetime.now(timezone.utc).isoformat()
            public_manifest = PublicMeetingManifest(
                meeting_id=mid,
                title=normalized_title,
                created_at=created_at,
                meeting_type=meeting_type,
                participant_count=len(public_participants),
                representative_count=(
                    len(public_participants)
                    if meeting_type in {MeetingType.DELIBERATION, MeetingType.SCHOLARLY_RENDERING}
                    else None
                ),
                audit_member_count=(len(public_participants) if meeting_type == MeetingType.AUDIT else None),
                research_enabled=research_enabled,
                general_search_allowed=general_search_allowed,
                institutional_access_allowed=institutional_access_allowed,
                academic_search_engine=academic_search_engine,
                general_search_engine=general_search_engine,
                planning_exploration_enabled=(
                    research_enabled and deliverable_type == DeliverableType.LITERATURE_REVIEW
                ),
                planning_replan_reference_enabled=(
                    deliverable_type == DeliverableType.LITERATURE_REVIEW
                ),
                decision_rigor=decision_rigor,
                deliverable_type=deliverable_type,
                parent_meeting_id=parent_meeting_id,
                rendering_science_consultation_authority=rendering_science_consultation_authority,
                rendering_target_body_characters=rendering_target_body_characters,
                rendering_scope_description=rendering_scope_description,
                literature_writing_policy=literature_writing_policy,
                rendering_final_science_query_limit=(
                    4 if meeting_type == MeetingType.SCHOLARLY_RENDERING else None
                ),
            )
            private_manifest = PrivateMeetingManifest(
                meeting_id=mid,
                title=normalized_title,
                created_at=created_at,
                prompt_contract_version=CURRENT_PROMPT_CONTRACT_VERSION,
                context_assembly_version=CURRENT_CONTEXT_ASSEMBLY_VERSION,
                evidence_read_protocol_version=1,
                evidence_read_round_limit=evidence_read_round_limit,
                selected_models=selected_models,
                chair_model=chair_model,
                meeting_type=meeting_type,
                personas=(
                    [p.value for p in configured_personas]
                    if meeting_type == MeetingType.DELIBERATION
                    else (
                        [
                            RenderingRole.SCIENCE_BOOKKEEPER.value,
                            RenderingRole.CITATION_BOOKKEEPER.value,
                        ]
                        if meeting_type == MeetingType.SCHOLARLY_RENDERING
                        else []
                    )
                ),
                governance_digest=directory_digest(frozen_governance),
                representative_prompt_family=(
                    "literature_research"
                    if deliverable_type == DeliverableType.LITERATURE_REVIEW
                    else "deliberation"
                ),
                representative_reasoning_effort=representative_reasoning_effort,
                representative_reasoning_effective={
                    f"{provider}:{model}": effort
                    for (provider, model), effort in (representative_reasoning_effective or {}).items()
                },
                chair_reasoning_effort=chair_reasoning_effort,
                research_enabled=research_enabled,
                general_search_allowed=general_search_allowed,
                institutional_access_allowed=institutional_access_allowed,
                academic_search_engine=academic_search_engine,
                general_search_engine=general_search_engine,
                planning_exploration_enabled=(
                    research_enabled and deliverable_type == DeliverableType.LITERATURE_REVIEW
                ),
                decision_rigor=decision_rigor,
                research_model=research_model,
                research_reasoning_effort=research_reasoning_effort,
                openalex_max_results_per_query=openalex_max_results_per_query,
                openalex_quota_policy=openalex_quota_policy,
                research_max_concurrent_claim_groups=research_max_concurrent_claim_groups,
                model_concurrency_limits={
                    f"{provider}:{model}": limit
                    for (provider, model), limit in (model_concurrency_limits or {}).items()
                },
                model_concurrency_sources={
                    f"{provider}:{model}": source
                    for (provider, model), source in (model_concurrency_sources or {}).items()
                },
                maximum_parallelism=maximum_parallelism,
                deliverable_type=deliverable_type,
                parent_meeting_id=parent_meeting_id,
                parent_meeting_path=(
                    str(Path(parent_meeting_path).expanduser().resolve())
                    if parent_meeting_path is not None
                    else None
                ),
                inheritance_mode=inheritance_mode,
                rendering_provisional_source=rendering_provisional_source,
                rendering_science_models=science_models,
                rendering_citation_models=citation_models,
                rendering_language=rendering_language,
                deliberation_language=deliberation_language,
                rendering_academic_skeleton=rendering_academic_skeleton,
                rendering_full_abstract=rendering_full_abstract,
                rendering_section_abstracts=rendering_section_abstracts,
                rendering_segmentation=rendering_segmentation,
                rendering_liveliness=rendering_liveliness,
                rendering_output_formats=list(rendering_output_formats or ()),
                rendering_science_order=list(rendering_science_order or ()),
                rendering_science_consultation_authority=rendering_science_consultation_authority,
                rendering_target_body_characters=rendering_target_body_characters,
                rendering_scope_description=rendering_scope_description,
                literature_writing_policy=literature_writing_policy,
                writer_model=writer_model,
                writer_reasoning_effort=writer_reasoning_effort,
                fast_planner_models=list(fast_planner_models or ()),
                fast_planner_reasoning_effective=dict(fast_planner_reasoning_effective or {}),
                technician_model=technician_model,
                technician_reasoning_effort=technician_reasoning_effort,
                rendering_final_science_query_limit=(
                    4 if meeting_type == MeetingType.SCHOLARLY_RENDERING else None
                ),
            )
            repo.docs.write_once(
                "public/meeting_manifest.json",
                json.dumps(public_manifest.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
            if meeting_type == MeetingType.DELIBERATION:
                relaxed = decision_rigor == DecisionRigor.RELAXED
                repo.docs.write_once(
                    "public/decision_policy.json",
                    json.dumps(
                        {
                            "meeting_id": mid,
                            "decision_rigor": decision_rigor.value,
                            "human_selected_at_initialization": True,
                            "high_threshold_formula": (
                                "floor(N_ACTIVE/2)+1" if relaxed else "ceil(3*N_ACTIVE/4)"
                            ),
                            "scope": "Only votes whose original rule requires the 3/4 high threshold; all other thresholds and ballot eligibility rules are unchanged.",
                            "human_ruling": (
                                "本场会议凡原规则要求 3/4 高门槛的表决，一律改为全体合格投票者过半；"
                                "基准制度文件或阶段提示中写明 3/4 的地方，以本场规则为准。"
                                "其他门槛、分母与投票资格均不变。"
                                if relaxed else "本场会议沿用基准制度规定的 3/4 高门槛。"
                            ),
                            "status": "TRIAL" if relaxed else "BASELINE",
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                )
            repo.docs.write_once(public_registry_name, json.dumps(public_participants, indent=2, ensure_ascii=False))
            repo.docs.write_once(
                "identity_private/meeting_manifest.json",
                json.dumps(private_manifest.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
            repo.docs.write_once(private_registry_name, json.dumps(private_registry, indent=2, ensure_ascii=False))
            if task_description is not None:
                task = PublicTask(
                    meeting_id=mid,
                    title=normalized_title,
                    meeting_type=meeting_type,
                    description=task_description,
                    deliverable_type=deliverable_type,
                )
                repo.docs.write_once(
                    "public/task.json",
                    json.dumps(task.model_dump(mode="json"), indent=2, ensure_ascii=False),
                )
            _write_prompt_and_lineage(
                repo,
                task_description=task_description or "",
                title=normalized_title,
                meeting_type=meeting_type,
                parent_meeting_id=parent_meeting_id,
                parent_meeting_path=(
                    Path(parent_meeting_path) if parent_meeting_path is not None else None
                ),
                inheritance_mode=inheritance_mode,
            )
            if escalation_email is not None:
                contact = EscalationContact(meeting_id=mid, email=escalation_email)
                repo.docs.write_once(
                    "human_private/escalation_contact.json",
                    json.dumps(contact.model_dump(mode="json"), indent=2, ensure_ascii=False),
                )
            if config_path is not None:
                config_source = Path(config_path).expanduser().resolve()
                config_bytes = config_source.read_bytes()
                config_snapshot = Path(
                    "human_private/configuration_snapshot/ensemble.toml"
                )
                repo.docs.write_once(config_snapshot, config_bytes)
                model_source = _configured_model_file(config_source, config_bytes)
                model_snapshot: Path | None = None
                model_digest: str | None = None
                if model_source is not None:
                    model_bytes = model_source.read_bytes()
                    model_snapshot = Path(
                        "human_private/configuration_snapshot/model_config.toml"
                    )
                    repo.docs.write_once(model_snapshot, model_bytes)
                    model_digest = hashlib.sha256(model_bytes).hexdigest()
                config_ref = SessionConfigurationReference(
                    meeting_id=mid,
                    config_path=str(config_source),
                    config_sha256=hashlib.sha256(config_bytes).hexdigest(),
                    snapshot_path=str(config_snapshot),
                    governance_docs_path=str(Path(governance_docs).expanduser().resolve()),
                    governance_snapshot_path=str(GOVERNANCE_SNAPSHOT),
                    model_config_path=(str(model_source) if model_source else None),
                    model_config_sha256=model_digest,
                    model_config_snapshot_path=(
                        str(model_snapshot) if model_snapshot else None
                    ),
                )
                repo.docs.write_once(
                    "human_private/session_configuration.json",
                    json.dumps(config_ref.model_dump(mode="json"), indent=2, ensure_ascii=False),
                )
            repo.events.append(
                "MEETING_CREATED",
                {
                    "meeting_id": mid,
                    "title": normalized_title,
                    "meeting_type": meeting_type.value,
                    "participant_count": len(public_participants),
                    "human_escalation_configured": escalation_email is not None,
                    "research_enabled": research_enabled,
                    "general_search_allowed": general_search_allowed,
                    "institutional_access_allowed": institutional_access_allowed,
                    "academic_search_engine": academic_search_engine,
                    "general_search_engine": general_search_engine,
                    "decision_rigor": decision_rigor.value,
                    "deliverable_type": deliverable_type.value,
                    "parent_meeting_id": parent_meeting_id,
                    "inheritance_mode": (
                        inheritance_mode.value if inheritance_mode is not None else None
                    ),
                },
                actor="orchestrator",
            )
            if parent_meeting_path is not None:
                _import_parent_meeting_assets(
                    repo,
                    Path(parent_meeting_path),
                    expected_parent_meeting_id=str(parent_meeting_id),
                    inheritance_mode=inheritance_mode,
                    target_deliverable_type=deliverable_type,
                    rendering_provisional_source=rendering_provisional_source,
                )
                if deliverable_type == DeliverableType.SCHOLARLY_RENDERING:
                    source_relative = Path(
                        "public/continuation/source_artifacts/final/literature_review_report.md"
                    )
                    if not (repo.root / source_relative).is_file():
                        source_relative = Path(
                            "public/continuation/source_artifacts/final/scholarly_rendering/scholarly_review.md"
                        )
                    if (repo.root / source_relative).is_file():
                        ensure_visible_link(
                            repo.root,
                            link_name=(
                                "SOURCE_DRAFT.md" if rendering_provisional_source
                                else "ORIGINAL_REPORT.md"
                            ),
                            target_relative=source_relative,
                        )
            elif deliverable_type == DeliverableType.LITERATURE_REVIEW:
                _write_root_literature_report_origin(repo)
            staging.rename(root)
            return cls(root)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    @property
    def meeting_id(self) -> str:
        data = json.loads(self.docs.read_text("public/meeting_manifest.json"))
        return str(data["meeting_id"])

    def escalation_email(self) -> str | None:
        path = self.root / "human_private/escalation_contact.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data["email"])

    def config_path(self) -> Path:
        path = self.root / "human_private/session_configuration.json"
        if not path.exists():
            raise ValueError("meeting does not record a configuration path; supply --config explicitly")
        data = json.loads(path.read_text(encoding="utf-8"))
        source = Path(data["config_path"]).expanduser()
        if source.is_file():
            return source
        # Relocation may remove the old source configuration. Use only the
        # immutable, digest-checked copy inside this meeting in that case.
        relative_name = data.get("snapshot_path")
        expected_digest = data.get("config_sha256")
        if not isinstance(relative_name, str) or not isinstance(expected_digest, str):
            raise ValueError("recorded configuration is missing; supply --config explicitly")
        relative = Path(relative_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid meeting configuration snapshot path")
        snapshot = (self.root / relative).resolve()
        if not snapshot.is_relative_to(self.root.resolve()) or not snapshot.is_file():
            raise ValueError("recorded configuration snapshot is missing; supply --config explicitly")
        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != expected_digest:
            raise ValueError("recorded configuration snapshot does not match its frozen digest")
        model_name = data.get("model_config_snapshot_path")
        model_digest = data.get("model_config_sha256")
        if model_name is not None or model_digest is not None:
            if not isinstance(model_name, str) or not isinstance(model_digest, str):
                raise ValueError("incomplete frozen model configuration reference")
            model_relative = Path(model_name)
            if model_relative.is_absolute() or ".." in model_relative.parts:
                raise ValueError("invalid model configuration snapshot path")
            model_snapshot = (self.root / model_relative).resolve()
            if not model_snapshot.is_relative_to(self.root.resolve()) or not model_snapshot.is_file():
                raise ValueError("recorded model configuration snapshot is missing")
            if hashlib.sha256(model_snapshot.read_bytes()).hexdigest() != model_digest:
                raise ValueError("recorded model configuration snapshot does not match its frozen digest")
        return snapshot

    @contextmanager
    def exclusive_run_lock(self):
        """Prevent two local processes from advancing one meeting concurrently."""
        path = self.root / "governance_private" / "meeting.run.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(
                    "该会议正在另一个 ensemble 进程中运行；请回到原终端，或等待原进程退出后再恢复"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_CONTINUATION_SOURCE_FILES = (
    "public/task.json",
    "public/archive_manifest.json",
    "public/general_principle/D3.md",
    "public/detailed_clauses/C1.md",
    "public/final/procedurally_certified_resolution.md",
    "public/final/final_report.md",
    "public/final/final_report_v2.md",
    "public/final/literature_review_report.md",
    "public/final/scholarly_rendering/scholarly_review.md",
    "public/final/minority_reports.json",
    "public/final/think_tank_epistemic_reviews.json",
)

_ADVISORY_DOCUMENT_PRIORITY = (
    "public/final/scholarly_rendering/scholarly_review.md",
    "public/final/literature_review_report.md",
    "public/final/final_report_v2.md",
    "public/final/final_report.md",
    "public/final/procedurally_certified_resolution.md",
    "public/detailed_clauses/C1.md",
    "public/general_principle/D3.md",
)


def provisional_rendering_text(parent_root: str | Path) -> str:
    """Return the frozen Chair-patched text, never a partial model response.

    This source has not passed the source meeting's final readability gate.  It
    can only enter a new, explicitly Human-selected rendering meeting.
    """

    root = Path(parent_root).expanduser()
    cert_path = root / "chair_private/literature_report/readability_certification.json"
    if not cert_path.is_file():
        raise ValueError("source meeting has not frozen a failed final readability check")
    cert = json.loads(cert_path.read_text(encoding="utf-8"))
    if cert.get("status") != "REVISION_REQUIRED":
        raise ValueError("source meeting is not paused on a frozen readability failure")
    audit_path = root / "audit_private/literature_report/publication_patches.json"
    if not audit_path.is_file():
        raise ValueError("source meeting has no frozen Chair-patched publication draft")
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    text = payload.get("final_text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("source meeting has no complete frozen publication text")
    if not isinstance(payload.get("decisions"), list):
        raise ValueError("source meeting has no frozen publication-patch decision ledger")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != cert.get("source_sha256"):
        raise ValueError("frozen readability check does not match the Chair-patched source text")
    if (root / "public/literature_report/execution_result.json").exists():
        raise ValueError("source meeting is already complete; use the ordinary rendering transfer")
    return text


def _verify_archived_parent_integrity(parent_root: Path, expected_meeting_id: str) -> dict | None:
    archive_path = parent_root / "public/archive_manifest.json"
    if not archive_path.is_file():
        return None
    record = json.loads(archive_path.read_text(encoding="utf-8"))
    if record.get("status") != "ARCHIVED" or record.get("meeting_id") != expected_meeting_id:
        raise ValueError("归档会议清单状态或会议 ID 不匹配，不能接续")
    hashes = record.get("retained_file_sha256")
    document = record.get("retained_document_path")
    if not isinstance(hashes, dict) or not isinstance(document, str) or document not in hashes:
        raise ValueError("归档会议缺少完整文稿或保留文件的校验信息")
    from project_ensemble.storage.literature_zip_cache import effective_archive_hashes
    hashes = effective_archive_hashes(parent_root, record)
    for relative_text, expected in hashes.items():
        if not isinstance(relative_text, str) or not isinstance(expected, str):
            raise ValueError("归档会议的文件校验记录格式无效")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("归档会议的文件校验路径越过会议目录")
        source = parent_root / relative
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(parent_root):
            raise ValueError(f"归档会议缺少保留文件：{relative_text}")
        with source.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"归档会议文件校验失败：{relative_text}")
    return {**record, "retained_file_sha256": hashes}


def _import_parent_meeting_assets(
    repo: MeetingRepository,
    parent_root: Path,
    *,
    expected_parent_meeting_id: str,
    inheritance_mode: InheritanceMode,
    target_deliverable_type: DeliverableType,
    rendering_provisional_source: bool = False,
) -> None:
    """Copy public source material into a derived meeting with a hash ledger.

    The parent is read-only.  Public Research Desk packets are copied to their
    ordinary child-meeting paths so freshness-aware cache lookup can reuse them.
    Other source artifacts are namespaced under ``public/continuation``.
    """

    parent_root = parent_root.expanduser().resolve()
    manifest_path = parent_root / "public/meeting_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("parent meeting has no public/meeting_manifest.json")
    parent_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_parent_id = str(parent_manifest.get("meeting_id", ""))
    if actual_parent_id != expected_parent_meeting_id:
        raise ValueError(
            "parent meeting ID does not match the selected source directory: "
            f"expected {expected_parent_meeting_id}, found {actual_parent_id or 'missing'}"
        )
    archive_record = _verify_archived_parent_integrity(parent_root, actual_parent_id)

    records: list[dict[str, object]] = []

    def copy_one(source: Path, destination: Path, *, asset_kind: str) -> None:
        if source.is_symlink():
            raise ValueError(f"parent meeting contains unsupported symlink: {source}")
        _, byte_count, digest = repo.docs.copy_once(destination, source)
        records.append(
            {
                "source_path": str(source.relative_to(parent_root)),
                "destination_path": str(destination),
                "asset_kind": asset_kind,
                "byte_count": byte_count,
                "sha256": digest,
            }
        )

    inherit_evidence = inheritance_mode in {InheritanceMode.EVIDENCE, InheritanceMode.BOTH}
    inherit_document = inheritance_mode in {
        InheritanceMode.FINAL_DOCUMENT,
        InheritanceMode.BOTH,
    }

    research_root = parent_root / "public/research"
    if inherit_evidence and research_root.is_dir():
        for source in sorted(path for path in research_root.rglob("*") if path.is_file()):
            relative = source.relative_to(research_root)
            copy_one(
                source,
                Path("public/research") / relative,
                asset_kind="INHERITED_RESEARCH_DESK_ASSET",
            )
    human_reference_root = parent_root / "public/human_references"
    if inherit_evidence and human_reference_root.is_dir():
        for source in sorted(path for path in human_reference_root.rglob("*") if path.is_file()):
            copy_one(
                source,
                Path("public/human_references") / source.relative_to(human_reference_root),
                asset_kind="INHERITED_HUMAN_REFERENCE",
            )

    for relative_text in _CONTINUATION_SOURCE_FILES if inherit_document else ():
        source = parent_root / relative_text
        if not source.is_file():
            continue
        relative = Path(relative_text)
        copy_one(
            source,
            Path("public/continuation/source_artifacts") / Path(*relative.parts[1:]),
            asset_kind="INHERITED_SOURCE_ARTIFACT",
        )

    provisional_text: str | None = None
    archived_uncertified = bool(
        archive_record and archive_record.get("source_certification_status")
        == "PROMOTED_COMPLETE_DRAFT_NOT_PROCEDURALLY_CERTIFIED"
    )
    if rendering_provisional_source:
        provisional_text = provisional_rendering_text(parent_root)
        source = parent_root / "audit_private/literature_report/publication_patches.json"
        destination = Path("public/continuation/source_artifacts/final/literature_review_report.md")
        raw = provisional_text.encode("utf-8")
        repo.docs.write_once(destination, raw)
        records.append(
            {
                "source_path": str(source.relative_to(parent_root)),
                "source_field": "final_text",
                "source_record_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "destination_path": str(destination),
                "asset_kind": "UNCERTIFIED_CHAIR_PATCHED_REPORT_DRAFT",
                "byte_count": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    advisory_source: Path | None = None
    if inherit_document:
        advisory_source = next(
            (
                parent_root / relative
                for relative in _ADVISORY_DOCUMENT_PRIORITY
                if (parent_root / relative).is_file()
            ),
            None,
        )
        if advisory_source is None and provisional_text is None:
            raise ValueError(
                "final-document inheritance was selected, but the parent has no completed textual deliverable"
            )
        advisory_text = provisional_text if provisional_text is not None else advisory_source.read_text(encoding="utf-8")
        advisory_label = (
            "audit_private/literature_report/publication_patches.json#final_text"
            if provisional_text is not None
            else str(advisory_source.relative_to(parent_root))
        )
        advisory_limit = 64_000
        bounded = advisory_text[:advisory_limit]
        truncation_notice = (
            "\n\n> 上下文视图已按 64,000 字符上限截断；完整冻结原文见 "
            "`public/continuation/source_artifacts/final/literature_review_report.md`。"
            if len(advisory_text) > advisory_limit
            else ""
        )
        advisory_context = (
            "# 前序会议建议性文书\n\n"
            f"> 来源会议：`{actual_parent_id}`；来源文件：`{advisory_label}`。\n"
            + (
                "> 来源状态：未通过原会议最终可读性认证；本次为 Human 明确授权的独立重绘，不代表原会议已完成。\n"
                if provisional_text is not None else ""
            )
            + (
                "> 来源状态：原会议未完成认证；所继承的是 Human 归档时提升的完整草稿，不能视为原会议正式结论。\n"
                if archived_uncertified else ""
            )
            + "> 权威边界：本文书是后续任务的建议性前置材料，不是 Constitution，"
            "不自动成为不可违背的规则。参与者可以基于新任务、证据或理由偏离；"
            "系统不要求逐条合规映射或严格 bookkeeping。\n\n"
            + bounded
            + truncation_notice
        )
        repo.docs.write_once("public/continuation/advisory_context.md", advisory_context)

    evidence_packet_count = sum(
        1
        for record in records
        if str(record["destination_path"]).startswith("public/research/evidence_packets/")
        and str(record["destination_path"]).endswith(".json")
    )
    if target_deliverable_type == DeliverableType.SCHOLARLY_RENDERING:
        private_manifest_path = parent_root / "identity_private/meeting_manifest.json"
        if private_manifest_path.is_file():
            private_manifest = json.loads(private_manifest_path.read_text(encoding="utf-8"))
            selected = {
                f"{provider}:{model}"
                for provider, model in private_manifest.get("selected_models", [])
            }
            if private_manifest.get("chair_model"):
                provider, model = private_manifest["chair_model"]
                selected.add(f"{provider}:{model}")
            if private_manifest.get("research_model"):
                provider, model = private_manifest["research_model"]
                selected.add(f"{provider}:{model}")
            runtime_summary = {
                "source_meeting_id": actual_parent_id,
                "models": sorted(selected),
                "representative_reasoning_effort": private_manifest.get(
                    "representative_reasoning_effort", "default"
                ),
                "chair_reasoning_effort": private_manifest.get(
                    "chair_reasoning_effort", "default"
                ),
                "research_reasoning_effort": private_manifest.get(
                    "research_reasoning_effort"
                ),
                "privacy_notice": (
                    "Model and reasoning controls are listed without persona-to-model mapping."
                ),
            }
            repo.docs.write_once(
                "public/continuation/source_runtime_summary.json",
                json.dumps(runtime_summary, indent=2, ensure_ascii=False),
            )
    lineage = {
        "meeting_id": repo.meeting_id,
        "parent_meeting_id": actual_parent_id,
        "parent_meeting_path": str(parent_root),
        "continuation_type": "DERIVED_DELIVERABLE",
        "target_deliverable_type": target_deliverable_type.value,
        "inheritance_mode": inheritance_mode.value,
        "source_status": (
            "PROVISIONAL_UNCERTIFIED" if provisional_text is not None
            else "ARCHIVED_UNCERTIFIED_DRAFT" if archived_uncertified
            else "COMPLETED"
        ),
        "copy_policy": {
            InheritanceMode.EVIDENCE: "PUBLIC_RESEARCH_ONLY",
            InheritanceMode.FINAL_DOCUMENT: "SELECTED_FINAL_DOCUMENTS_ONLY",
            InheritanceMode.BOTH: "PUBLIC_RESEARCH_AND_SELECTED_FINAL_DOCUMENTS",
        }[inheritance_mode],
        "inherited_evidence_packet_count": evidence_packet_count,
        "advisory_context_path": (
            "public/continuation/advisory_context.md"
            if advisory_source is not None or provisional_text is not None else None
        ),
        "files": records,
    }
    repo.docs.write_once(
        "public/continuation/lineage.json",
        json.dumps(lineage, indent=2, ensure_ascii=False),
    )
    repo.events.append(
        "PARENT_MEETING_PUBLIC_ASSETS_IMPORTED",
        {
            "meeting_id": repo.meeting_id,
            "parent_meeting_id": actual_parent_id,
            "file_count": len(records),
            "inherited_evidence_packet_count": evidence_packet_count,
            "inheritance_mode": inheritance_mode.value,
            "advisory_document_inherited": advisory_source is not None,
            "lineage_path": "public/continuation/lineage.json",
        },
        actor="orchestrator",
    )


def _write_root_literature_report_origin(repo: MeetingRepository) -> None:
    origin = {
        "meeting_id": repo.meeting_id,
        "origin_type": "FROM_SCRATCH",
        "parent_meeting_id": None,
        "target_deliverable_type": DeliverableType.LITERATURE_REVIEW.value,
        "inherited_evidence_packet_count": 0,
        "notice": "Research Desk starts with an empty meeting-local evidence database.",
    }
    repo.docs.write_once(
        "public/literature_report/origin.json",
        json.dumps(origin, indent=2, ensure_ascii=False),
    )
    repo.events.append(
        "LITERATURE_REPORT_ROOT_ORIGIN_FROZEN",
        {
            "meeting_id": repo.meeting_id,
            "origin_type": "FROM_SCRATCH",
            "inherited_evidence_packet_count": 0,
            "origin_path": "public/literature_report/origin.json",
        },
        actor="orchestrator",
    )
