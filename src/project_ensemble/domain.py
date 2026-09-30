from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Persona(str, Enum):
    SYSTEMS_INTEGRATOR = "systems_integrator"
    PRAGMATIC_MINIMALIST = "pragmatic_minimalist"
    EXPLORATORY_SYNTHESIST = "exploratory_synthesist"
    LIBRARIAN = "librarian"


class RenderingRole(str, Enum):
    SCIENCE_BOOKKEEPER = "science_bookkeeper"
    CITATION_BOOKKEEPER = "citation_bookkeeper"


class MeetingType(str, Enum):
    DELIBERATION = "deliberation"
    AUDIT = "audit"
    RESEARCH = "research"
    SCHOLARLY_RENDERING = "scholarly_rendering"


class DeliverableType(str, Enum):
    """The artifact a meeting is expected to produce, independent of procedure."""

    NORMATIVE_INSTRUMENT = "normative_instrument"
    LITERATURE_REVIEW = "literature_review"
    SCHOLARLY_RENDERING = "scholarly_rendering"


class InheritanceMode(str, Enum):
    """Public assets copied from a completed parent into a new meeting."""

    EVIDENCE = "evidence"
    FINAL_DOCUMENT = "final_document"
    BOTH = "both"


class ReasoningEffort(str, Enum):
    """Provider-neutral reasoning control frozen when a meeting is created."""

    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionRigor(str, Enum):
    """Human-selected high-threshold policy for this meeting."""

    STRICT = "strict"
    RELAXED = "relaxed"


class RepresentativeStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CONSULTATIVE = "CONSULTATIVE"
    UNAVAILABLE = "UNAVAILABLE"


class MeetingPhase(str, Enum):
    INIT = "INIT"
    INITIAL_DRAFT = "INITIAL_DRAFT"
    RESEARCH_ROUND = "RESEARCH_ROUND"
    RESEARCH_PLANNING = "RESEARCH_PLANNING"
    RESEARCH_OUTLINE_REVIEW = "RESEARCH_OUTLINE_REVIEW"
    LITERATURE_MODULE_RESEARCH = "LITERATURE_MODULE_RESEARCH"
    LITERATURE_MODULE_DRAFTING = "LITERATURE_MODULE_DRAFTING"
    LITERATURE_MODULE_REVIEW = "LITERATURE_MODULE_REVIEW"
    LITERATURE_MODULE_CONFIRMATION = "LITERATURE_MODULE_CONFIRMATION"
    LITERATURE_REPORT_SYNTHESIS = "LITERATURE_REPORT_SYNTHESIS"
    LITERATURE_REPORT_REVIEW = "LITERATURE_REPORT_REVIEW"
    LITERATURE_REPORT_PUBLICATION = "LITERATURE_REPORT_PUBLICATION"
    SCHOLARLY_RENDERING_PLAN = "SCHOLARLY_RENDERING_PLAN"
    SCHOLARLY_RENDERING_DRAFT = "SCHOLARLY_RENDERING_DRAFT"
    SCHOLARLY_SCIENCE_REVIEW = "SCHOLARLY_SCIENCE_REVIEW"
    SCHOLARLY_CITATION_REVIEW = "SCHOLARLY_CITATION_REVIEW"
    SCHOLARLY_RENDERING_PUBLICATION = "SCHOLARLY_RENDERING_PUBLICATION"
    GENERAL_POSITION = "GENERAL_POSITION"
    AMENDMENT_SUBMISSION = "AMENDMENT_SUBMISSION"
    COSPONSORSHIP = "COSPONSORSHIP"
    BALLOT = "BALLOT"
    GENERAL_RATIFICATION = "GENERAL_RATIFICATION"
    STATUS_TRANSITION = "STATUS_TRANSITION"
    DETAILED_DRAFTING = "DETAILED_DRAFTING"
    CLAUSE_REVIEW = "CLAUSE_REVIEW"
    RESOLUTION_FROZEN = "RESOLUTION_FROZEN"
    CHAIR_REVIEW = "CHAIR_REVIEW"
    THINK_TANK_REVIEW = "THINK_TANK_REVIEW"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    HANDOFF_READY = "HANDOFF_READY"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_id: str
    model_id: str
    persona: Persona | None = None
    rendering_role: RenderingRole | None = None
    harness: str | None = None
    reasoning_setting: str | None = None
    prompt_version: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_role(self) -> "RuntimeConfig":
        if (self.persona is None) == (self.rendering_role is None):
            raise ValueError("runtime requires exactly one deliberative persona or rendering role")
        return self


class PrivateRepresentative(BaseModel):
    model_config = ConfigDict(frozen=True)
    representative_id: str
    runtime: RuntimeConfig
    status: RepresentativeStatus = RepresentativeStatus.ACTIVE


class PrivateAuditMember(BaseModel):
    """Fresh audit identity. It intentionally has no deliberation persona."""

    model_config = ConfigDict(frozen=True)
    audit_member_id: str
    provider_id: str
    model_id: str
    harness: str | None = None
    reasoning_setting: str | None = None
    prompt_version: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class PublicRepresentative(BaseModel):
    model_config = ConfigDict(frozen=True)
    representative_id: str
    status: RepresentativeStatus = RepresentativeStatus.ACTIVE


class ModelDescriptor(BaseModel):
    provider_id: str
    model_id: str
    owned_by: str | None = None
    input_token_limit: int | None = None
    output_token_limit: int | None = None
    max_concurrent_requests: int | None = Field(default=None, ge=1)
    supported_methods: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class GenerationRequest(BaseModel):
    model_id: str
    system_text: str
    user_text: str
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT
    extra: dict[str, Any] = Field(default_factory=dict)


class GenerationResponse(BaseModel):
    text: str
    provider_id: str
    model_id: str
    request_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
