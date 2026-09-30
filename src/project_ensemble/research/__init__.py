"""Shared, non-voting external-evidence infrastructure."""

from project_ensemble.research.desk import ResearchDesk
from project_ensemble.research.citations import validate_evidence_citations
from project_ensemble.research.cache import ResearchPacketCache
from project_ensemble.research.models import (
    ClaimReuseAssessment,
    ClaimScopeRelation,
    EvidencePacket,
    EvidenceCitation,
    EvidenceUseClass,
    FreshnessClass,
    KnowledgeStatus,
    ResearchRequest,
    ResearchRoundDedupGroup,
    ResearchRoundReleaseGate,
    ResearchRoundSubmission,
    ResearchStage,
)
from project_ensemble.research.retrievers import CompositeRetriever, TavilyRetriever
from project_ensemble.research.rounds import ResearchRoundResult, ResearchRoundRunner

__all__ = [
    "ClaimReuseAssessment",
    "ClaimScopeRelation",
    "EvidencePacket",
    "EvidenceCitation",
    "EvidenceUseClass",
    "FreshnessClass",
    "KnowledgeStatus",
    "ResearchDesk",
    "ResearchRequest",
    "ResearchRoundDedupGroup",
    "ResearchPacketCache",
    "ResearchRoundReleaseGate",
    "ResearchRoundSubmission",
    "ResearchRoundResult",
    "ResearchRoundRunner",
    "ResearchStage",
    "CompositeRetriever",
    "TavilyRetriever",
    "validate_evidence_citations",
]
