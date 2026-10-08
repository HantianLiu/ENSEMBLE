from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchStage(str, Enum):
    PROPOSAL = "PROPOSAL"
    CHALLENGE = "CHALLENGE"
    VETO = "VETO"
    AUDIT = "AUDIT"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    LITERATURE_REPORT = "LITERATURE_REPORT"
    SCHOLARLY_RENDERING = "SCHOLARLY_RENDERING"


class KnowledgeStatus(str, Enum):
    MODEL_PRIOR = "MODEL_PRIOR"
    SOURCE_BACKED = "SOURCE_BACKED"
    QUALIFIED = "QUALIFIED"
    INFERENCE = "INFERENCE"
    ASSUMPTION = "ASSUMPTION"
    UNRESOLVED = "UNRESOLVED"


class EvidenceDirection(str, Enum):
    SUPPORTING = "SUPPORTING"
    CONTRADICTORY = "CONTRADICTORY"
    LIMITATION = "LIMITATION"
    ALTERNATIVE = "ALTERNATIVE"


class LiteratureConsensus(str, Enum):
    CLEAR = "CLEAR"
    QUALIFIED = "QUALIFIED"
    MIXED = "MIXED"
    INSUFFICIENT = "INSUFFICIENT"


class ConfidenceLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FreshnessClass(str, Enum):
    VOLATILE = "VOLATILE"
    VERSIONED = "VERSIONED"
    STABLE = "STABLE"


class ClaimSourceDomain(str, Enum):
    ACADEMIC = "ACADEMIC"
    STANDARD_METHOD = "STANDARD_METHOD"
    SOFTWARE_API = "SOFTWARE_API"
    CURRENT_FACT = "CURRENT_FACT"
    GENERAL = "GENERAL"


class CacheInvalidationAuthority(str, Enum):
    RESEARCH_DESK = "RESEARCH_DESK"
    HUMAN = "HUMAN"


class CacheInvalidationReason(str, Enum):
    RETRACTION = "RETRACTION"
    CORRECTION = "CORRECTION"
    OFFICIAL_UPDATE = "OFFICIAL_UPDATE"
    VERSION_CHANGE = "VERSION_CHANGE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    HUMAN_DIRECTIVE = "HUMAN_DIRECTIVE"


class ResearchCacheInvalidation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    packet_id: str = Field(min_length=1)
    invalidated_at: datetime
    authority: CacheInvalidationAuthority
    reason: CacheInvalidationReason
    rationale: str = Field(min_length=1)
    evidence_url: str | None = None

    @model_validator(mode="after")
    def automatic_invalidation_requires_traceable_evidence(self) -> "ResearchCacheInvalidation":
        if self.authority == CacheInvalidationAuthority.RESEARCH_DESK:
            if self.reason == CacheInvalidationReason.HUMAN_DIRECTIVE:
                raise ValueError("Research Desk cannot issue HUMAN_DIRECTIVE invalidation")
            if not self.evidence_url:
                raise ValueError("Research Desk cache invalidation requires evidence_url")
        return self


class EvidenceUseClass(str, Enum):
    """How a source may be used for this claim, not a global source ranking."""

    AUTHORITATIVE = "AUTHORITATIVE"
    PRIMARY = "PRIMARY"
    REVIEW = "REVIEW"
    PROVISIONAL = "PROVISIONAL"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"


class DocumentArchiveStatus(str, Enum):
    ARCHIVED = "ARCHIVED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"


class ClaimScopeRelation(str, Enum):
    EXACT_EQUIVALENT = "EXACT_EQUIVALENT"
    NARROWER = "NARROWER"
    BROADER = "BROADER"
    OVERLAPPING = "OVERLAPPING"
    DISTINCT = "DISTINCT"


class ClaimReuseAssessment(BaseModel):
    """Explicit assessment required before considering a non-fingerprint cache candidate."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_packet_id: str = Field(min_length=1)
    relation: ClaimScopeRelation
    automatic_reuse_allowed: bool
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def only_exact_equivalence_allows_automatic_reuse(self) -> "ClaimReuseAssessment":
        expected = self.relation == ClaimScopeRelation.EXACT_EQUIVALENT
        if self.automatic_reuse_allowed != expected:
            raise ValueError("automatic cache reuse is allowed only for EXACT_EQUIVALENT claims")
        return self


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    requester_id: str = Field(min_length=1)
    stage: ResearchStage
    claim: str = Field(min_length=1)
    force_refresh: bool = False
    refresh_reason: str | None = None

    @model_validator(mode="after")
    def forced_refresh_has_reason(self) -> "ResearchRequest":
        if self.force_refresh and not self.refresh_reason:
            raise ValueError("forced Research Desk refresh requires refresh_reason")
        if not self.force_refresh and self.refresh_reason is not None:
            raise ValueError("refresh_reason is only allowed with force_refresh")
        return self


class ResearchRoundSubmission(BaseModel):
    """One Representative's sealed request batch for one Research Round."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    claims: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("claims")
    @classmethod
    def claims_are_nonempty_and_unique(cls, value: list[str]) -> list[str]:
        normalized = [claim.strip() for claim in value]
        if any(not claim for claim in normalized):
            raise ValueError("research claims cannot be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("research claims in one submission must be unique")
        return normalized


class ResearchRequestOrigin(BaseModel):
    """Audit-private origin of one claim in a sealed Research Round."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(min_length=1)
    requester_id: str = Field(min_length=1)


class ResearchRoundDedupGroup(BaseModel):
    """Audit-private exact-fingerprint group; never Representative-visible."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    group_id: str = Field(min_length=1)
    claim_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_claim: str = Field(min_length=1)
    origins: list[ResearchRequestOrigin] = Field(min_length=1)
    packet_id: str | None = None

    @model_validator(mode="after")
    def origin_requests_are_unique(self) -> "ResearchRoundDedupGroup":
        request_ids = [origin.request_id for origin in self.origins]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("deduplicated Research Round request IDs must be unique")
        return self


class ResearchClaimResolutionStatus(str, Enum):
    STAGED_PACKET = "STAGED_PACKET"
    REJECTED_NON_RESEARCHABLE = "REJECTED_NON_RESEARCHABLE"
    QC_FAILED = "QC_FAILED"
    PENDING = "PENDING"
    FAILED = "FAILED"


class ResearchClaimResolution(BaseModel):
    """Private per-claim state before a Research Round crosses its release barrier."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    claim_id: str = Field(min_length=1)
    status: ResearchClaimResolutionStatus
    packet_id: str | None = None
    failure_code: str | None = None
    failure_summary: str | None = None

    @model_validator(mode="after")
    def packet_id_matches_status(self) -> "ResearchClaimResolution":
        if self.status == ResearchClaimResolutionStatus.STAGED_PACKET and not self.packet_id:
            raise ValueError("STAGED_PACKET requires packet_id")
        if self.status != ResearchClaimResolutionStatus.STAGED_PACKET and self.packet_id is not None:
            raise ValueError("only STAGED_PACKET may carry packet_id")
        if self.status == ResearchClaimResolutionStatus.QC_FAILED:
            if not self.failure_code or not self.failure_summary:
                raise ValueError("QC_FAILED requires failure_code and failure_summary")
        elif self.failure_code is not None or self.failure_summary is not None:
            raise ValueError("only QC_FAILED may carry quality-control failure details")
        return self


class ResearchRoundReleaseGate(BaseModel):
    """Fail-closed barrier preventing rolling publication of a sealed Research Round."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_claim_ids: list[str] = Field(default_factory=list)
    resolutions: list[ResearchClaimResolution] = Field(default_factory=list)
    staged_document_ids: list[str] = Field(default_factory=list)
    released: bool = False
    published_packet_ids: list[str] = Field(default_factory=list)
    published_document_ids: list[str] = Field(default_factory=list)
    literature_bundle_published: bool = False

    @model_validator(mode="after")
    def release_is_complete_and_atomic(self) -> "ResearchRoundReleaseGate":
        expected = set(self.expected_claim_ids)
        if len(expected) != len(self.expected_claim_ids):
            raise ValueError("expected Research Round claim IDs must be unique")
        resolved_ids = [resolution.claim_id for resolution in self.resolutions]
        if len(resolved_ids) != len(set(resolved_ids)):
            raise ValueError("Research Round claim resolutions must be unique")
        if len(self.staged_document_ids) != len(set(self.staged_document_ids)):
            raise ValueError("staged Research Round document IDs must be unique")
        if len(self.published_document_ids) != len(set(self.published_document_ids)):
            raise ValueError("published Research Round document IDs must be unique")
        if not set(resolved_ids) <= expected:
            raise ValueError("Research Round contains a resolution for an unexpected claim")
        if not self.released:
            if (
                self.published_packet_ids
                or self.published_document_ids
                or self.literature_bundle_published
            ):
                raise ValueError("an unreleased Research Round cannot publish packets or documents")
            return self

        terminal = {
            ResearchClaimResolutionStatus.STAGED_PACKET,
            ResearchClaimResolutionStatus.REJECTED_NON_RESEARCHABLE,
            ResearchClaimResolutionStatus.QC_FAILED,
        }
        if set(resolved_ids) != expected or any(
            resolution.status not in terminal for resolution in self.resolutions
        ):
            raise ValueError("Research Round cannot release with missing, pending, or failed claims")
        staged_packet_ids = [
            resolution.packet_id
            for resolution in self.resolutions
            if resolution.status == ResearchClaimResolutionStatus.STAGED_PACKET
        ]
        if len(staged_packet_ids) != len(set(staged_packet_ids)):
            raise ValueError("staged Research Round packet IDs must be unique")
        if self.published_packet_ids != staged_packet_ids:
            raise ValueError("Research Round release must publish the complete staged packet batch")
        if self.published_document_ids != self.staged_document_ids:
            raise ValueError("Research Round release must publish the complete staged document batch")
        if not self.literature_bundle_published:
            raise ValueError("Research Round release must publish its meeting literature bundle")
        return self


class NormalizedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    is_researchable: bool
    normalized_claim: str = Field(min_length=1)
    verification_question: str = Field(min_length=1)
    supporting_query: str = Field(min_length=1)
    contradictory_query: str = Field(min_length=1)
    limitations_query: str = Field(min_length=1)
    alternatives_query: str = Field(min_length=1)
    scope_terms: list[str] = Field(default_factory=list)
    source_domain: ClaimSourceDomain
    source_domain_rationale: str = Field(min_length=1)
    freshness_class: FreshnessClass
    freshness_rationale: str = Field(min_length=1)
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejected_claim_has_reason(self) -> "NormalizedClaim":
        if not self.is_researchable and not self.rejection_reason:
            raise ValueError("a non-researchable request requires rejection_reason")
        return self


class EvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    publication_year: int | None = None
    doi: str | None = None
    url: str = Field(min_length=1)
    venue: str | None = None
    source_type: str = "scholarly_work"
    is_primary_source: bool | None = None
    evidence_use_class: EvidenceUseClass
    original_document_url: str | None = None
    access_basis: Literal["OPEN_ACCESS", "INSTITUTIONAL_SUBSCRIPTION", "HUMAN_SUPPLIED"] | None = None
    license: str | None = None
    archive_status: DocumentArchiveStatus = DocumentArchiveStatus.NOT_AVAILABLE
    archived_path: str | None = None
    archived_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    archived_media_type: str | None = None
    archived_size_bytes: int | None = Field(default=None, ge=0)
    archived_at: datetime | None = None

    @model_validator(mode="after")
    def archive_metadata_matches_status(self) -> "EvidenceSource":
        archived_fields = (
            self.archived_path,
            self.archived_sha256,
            self.archived_media_type,
            self.archived_size_bytes,
            self.archived_at,
        )
        if self.archive_status == DocumentArchiveStatus.ARCHIVED:
            if any(value is None for value in archived_fields):
                raise ValueError("ARCHIVED source requires complete archive metadata")
        elif any(value is not None for value in archived_fields):
            raise ValueError("non-archived source cannot carry archived file metadata")
        return self


class EvidenceCitationKind(str, Enum):
    DIRECT_SOURCE = "DIRECT_SOURCE"
    REPRESENTATIVE_INFERENCE = "REPRESENTATIVE_INFERENCE"


class EvidenceCitation(BaseModel):
    """Traceable citation attached to a substantive meeting statement."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    packet_id: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    kind: EvidenceCitationKind

    @field_validator("source_ids")
    @classmethod
    def citation_source_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("citation source IDs must be unique")
        return value


class EvidenceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1)
    direction: EvidenceDirection
    evidence_summary: str = Field(min_length=1)
    applicability: str = Field(min_length=1)
    limitations: str = Field(min_length=1)


class RetrievalConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    coverage: ConfidenceLevel = Field(
        description="Breadth and completeness of databases, queries, source types, and counter-search."
    )
    source_quality: ConfidenceLevel = Field(
        description="Authority, originality, review status, and accessibility of included sources."
    )
    literature_consistency: ConfidenceLevel = Field(
        description="Agreement among eligible sources within the same material scope."
    )
    rationale: str = Field(min_length=1)


class EvidencePacket(BaseModel):
    """Representative-visible result; confidence never means correctness probability."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    packet_id: str
    claim_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_claim: str
    normalized_claim: str
    verification_question: str = Field(min_length=1)
    scope_terms: list[str] = Field(default_factory=list)
    source_domain: ClaimSourceDomain = ClaimSourceDomain.GENERAL
    retrieval_backend_ids: list[str] = Field(default_factory=list)
    retrieved_at: datetime
    freshness_class: FreshnessClass = FreshnessClass.STABLE
    maximum_cache_reuse_age_days: int = Field(default=180, ge=0)
    cache_expires_at: datetime | None = None
    supersedes_packet_ids: list[str] = Field(default_factory=list)
    search_scope: str = Field(min_length=1)
    full_text_access_assessment: str = Field(
        default="Full-text archival has not yet been assessed.",
        min_length=1,
    )
    # Absent on packets frozen before original-source reading was introduced.
    source_reading_performed: bool = False
    # Older packets remain frozen, but only packets from the general source-
    # recovery path may satisfy new cache requests.
    source_recovery_performed: bool = False
    institutional_access_allowed: bool = False
    sources: list[EvidenceSource] = Field(default_factory=list)
    supporting_evidence: list[EvidenceFinding] = Field(default_factory=list)
    contradictory_evidence: list[EvidenceFinding] = Field(default_factory=list)
    scope_limitations: list[EvidenceFinding] = Field(default_factory=list)
    canonical_alternatives: list[EvidenceFinding] = Field(default_factory=list)
    counter_search_summary: str = Field(min_length=1)
    evidence_conflict_assessment: str = Field(min_length=1)
    consensus: LiteratureConsensus
    unresolved_questions: list[str] = Field(default_factory=list)
    confidence: RetrievalConfidence
    knowledge_status: KnowledgeStatus

    @model_validator(mode="after")
    def findings_reference_listed_sources(self) -> "EvidencePacket":
        if len(self.retrieval_backend_ids) != len(set(self.retrieval_backend_ids)):
            raise ValueError("retrieval backend IDs must be unique")
        if self.cache_expires_at is not None and self.cache_expires_at < self.retrieved_at:
            raise ValueError("cache expiry cannot precede packet retrieval")
        if len(self.supersedes_packet_ids) != len(set(self.supersedes_packet_ids)):
            raise ValueError("superseded packet IDs must be unique")
        if self.packet_id in self.supersedes_packet_ids:
            raise ValueError("an evidence packet cannot supersede itself")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("evidence packet source IDs must be unique")
        expected = (
            (self.supporting_evidence, EvidenceDirection.SUPPORTING),
            (self.contradictory_evidence, EvidenceDirection.CONTRADICTORY),
            (self.scope_limitations, EvidenceDirection.LIMITATION),
            (self.canonical_alternatives, EvidenceDirection.ALTERNATIVE),
        )
        for findings, direction in expected:
            for finding in findings:
                if finding.source_id not in source_ids:
                    raise ValueError(f"finding references unlisted source {finding.source_id}")
                if finding.direction != direction:
                    raise ValueError(f"finding in {direction.value} section has wrong direction")
        if self.knowledge_status == KnowledgeStatus.SOURCE_BACKED:
            source_backing_classes = {
                EvidenceUseClass.AUTHORITATIVE,
                EvidenceUseClass.PRIMARY,
                EvidenceUseClass.REVIEW,
            }
            if not self.supporting_evidence:
                raise ValueError("SOURCE_BACKED requires at least one supporting finding")
            supporting_source_ids = {
                finding.source_id for finding in self.supporting_evidence
            }
            if not any(
                source.source_id in supporting_source_ids
                and source.evidence_use_class in source_backing_classes
                for source in self.sources
            ):
                raise ValueError(
                    "SOURCE_BACKED requires at least one supporting authoritative, primary, or review source"
                )
        if self.knowledge_status == KnowledgeStatus.QUALIFIED:
            if not self.sources or not self.supporting_evidence:
                raise ValueError("QUALIFIED requires a source-backed supporting finding")
        if self.knowledge_status == KnowledgeStatus.UNRESOLVED and not self.unresolved_questions:
            raise ValueError("UNRESOLVED requires at least one unresolved question")
        if self.consensus == LiteratureConsensus.CLEAR:
            supporting_source_ids = {finding.source_id for finding in self.supporting_evidence}
            supporting_sources = [
                source for source in self.sources if source.source_id in supporting_source_ids
            ]
            has_consensus_source = any(
                source.evidence_use_class
                in {EvidenceUseClass.AUTHORITATIVE, EvidenceUseClass.REVIEW}
                for source in supporting_sources
            )
            eligible_support_count = sum(
                source.evidence_use_class
                in {
                    EvidenceUseClass.AUTHORITATIVE,
                    EvidenceUseClass.PRIMARY,
                    EvidenceUseClass.REVIEW,
                }
                for source in supporting_sources
            )
            if not has_consensus_source and eligible_support_count < 2:
                raise ValueError(
                    "CLEAR consensus requires a supporting authoritative/review source or "
                    "at least two eligible supporting sources"
                )
        if self.consensus == LiteratureConsensus.MIXED and (
            not self.supporting_evidence or not self.contradictory_evidence
        ):
            raise ValueError("MIXED consensus requires supporting and contradictory findings")
        return self


class ScreeningDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str
    included: bool
    rationale: str = Field(min_length=1)


class ScreeningDecisionSupplement(BaseModel):
    """Targeted completion for omitted or duplicated source-screening rows."""

    model_config = ConfigDict(extra="forbid")

    screening_decisions: list[ScreeningDecision] = Field(min_length=1)


class ResearchSynthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packet: EvidencePacket
    screening_decisions: list[ScreeningDecision] = Field(default_factory=list)
