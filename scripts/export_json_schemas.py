from __future__ import annotations

import json
from pathlib import Path
import sys

# When this script is run from a source checkout, import that checkout rather
# than an older Project_ENSEMBLE installation that may share the environment.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from project_ensemble.audit.strategic import GamingRisk, StrategicBehaviorEvent
from project_ensemble.domain import GenerationRequest, GenerationResponse, ModelDescriptor, PrivateAuditMember, PrivateRepresentative
from project_ensemble.handoff import ChairHandoffBrief, ThinkTankReview, HumanHandoffBundle
from project_ensemble.governance_private.drafting import AtomicDraftItem, DraftingAlignmentResult
from project_ensemble.orchestration.amendments import AmendmentSubmission, ChairRuling, ConflictEdge, RandomizedAmendmentOrder
from project_ensemble.orchestration.budgets import MeetingTokenBudgetSnapshot, ModelTokenBudget
from project_ensemble.orchestration.clause_review import (
    ClauseProposal,
    ClauseReviewAction,
    ClauseReviewResult,
    ClauseSplitMotion,
    MotionSupportAction,
    SuspensionMotion,
)
from project_ensemble.orchestration.clause_options import (
    ChairClauseOptionPlan,
    ChairProposalGrouping,
    ClauseOptionSetResult,
)
from project_ensemble.orchestration.clause_ballots import (
    ClauseInitialVote,
    ClauseInitialBallotAction,
    ClauseInitialBallotResult,
)
from project_ensemble.orchestration.clause_runoffs import (
    ClauseRunoffVote,
    ClauseRunoffAction,
    ClauseRunoffResult,
)
from project_ensemble.orchestration.clause_explanations import (
    ClauseChoiceExplanation,
    ClauseExplanationAction,
    GenuinelyNewClauseOption,
    ClauseSecondVote,
    ClauseSecondBallotAction,
    ClauseExplanationResult,
)
from project_ensemble.orchestration.clause_application import (
    AppliedClauseProposal,
    ChairClauseApplication,
    ClauseApplicationResult,
)
from project_ensemble.orchestration.clause_type_ii_reconstruction import (
    ChairTieDecision,
    NewOptionObjectionBallot,
    NewOptionObjectionVote,
    NewOptionProposerDisposition,
    NewOptionReviewItem,
    NewOptionReviewPlan,
    ReconstructedBallot,
    ReconstructedChoice,
    ReconstructedExplanation,
    ReconstructedExplanationSet,
    TypeIINewOptionResult,
)
from project_ensemble.orchestration.primary_drafter_selection import (
    PrimaryDrafterChairDecision,
    PrimaryDrafterChoice,
    PrimaryDrafterRanking,
)
from project_ensemble.orchestration.final_reviews import (
    ProceduralCheck,
    ChairProceduralCertification,
    ThinkTankFinding,
    LibrarianEpistemicReview,
    FinalReviewResult,
)
from project_ensemble.orchestration.general_principle import GeneralPositionAction, GeneralPrincipleRunResult, ProposedAmendment
from project_ensemble.orchestration.consultations import (
    HumanConsultationIssue,
    HumanConsultationResolution,
)
from project_ensemble.orchestration.scholarly_rendering import (
    CitationAnchorRepair,
    ChairCitationApplication,
    ChairScienceRevision,
    CitationDocket,
    CitationReview,
    CitationVoteSet,
    RenderedSection,
    RenderingPlan,
    ScholarlyRenderingResult,
    ScienceReview,
)
from project_ensemble.storage.meeting import (
    EscalationContact,
    PrivateMeetingManifest,
    PublicMeetingManifest,
    PublicTask,
    SessionConfigurationReference,
)
from project_ensemble.runtime.telemetry import TokenTelemetry

MODELS = [
    ModelDescriptor,
    GenerationRequest,
    GenerationResponse,
    PrivateRepresentative,
    PrivateAuditMember,
    AtomicDraftItem,
    DraftingAlignmentResult,
    AmendmentSubmission,
    ChairRuling,
    ConflictEdge,
    RandomizedAmendmentOrder,
    ProposedAmendment,
    GeneralPositionAction,
    GeneralPrincipleRunResult,
    ClauseProposal,
    ClauseSplitMotion,
    SuspensionMotion,
    ClauseReviewAction,
    MotionSupportAction,
    ClauseReviewResult,
    ChairProposalGrouping,
    ChairClauseOptionPlan,
    ClauseOptionSetResult,
    ClauseInitialVote,
    ClauseInitialBallotAction,
    ClauseInitialBallotResult,
    ClauseRunoffVote,
    ClauseRunoffAction,
    ClauseRunoffResult,
    ClauseChoiceExplanation,
    ClauseExplanationAction,
    GenuinelyNewClauseOption,
    ClauseSecondVote,
    ClauseSecondBallotAction,
    ClauseExplanationResult,
    AppliedClauseProposal,
    ChairClauseApplication,
    ClauseApplicationResult,
    NewOptionReviewItem,
    NewOptionReviewPlan,
    NewOptionProposerDisposition,
    NewOptionObjectionVote,
    NewOptionObjectionBallot,
    ReconstructedChoice,
    ReconstructedBallot,
    ReconstructedExplanation,
    ReconstructedExplanationSet,
    ChairTieDecision,
    TypeIINewOptionResult,
    PrimaryDrafterRanking,
    PrimaryDrafterChoice,
    PrimaryDrafterChairDecision,
    ProceduralCheck,
    ChairProceduralCertification,
    ThinkTankFinding,
    LibrarianEpistemicReview,
    FinalReviewResult,
    ModelTokenBudget,
    MeetingTokenBudgetSnapshot,
    StrategicBehaviorEvent,
    GamingRisk,
    ChairHandoffBrief,
    ThinkTankReview,
    HumanHandoffBundle,
    HumanConsultationIssue,
    HumanConsultationResolution,
    CitationAnchorRepair,
    RenderingPlan,
    RenderedSection,
    ScienceReview,
    ChairScienceRevision,
    CitationReview,
    CitationDocket,
    CitationVoteSet,
    ChairCitationApplication,
    ScholarlyRenderingResult,
    PublicMeetingManifest,
    PrivateMeetingManifest,
    PublicTask,
    EscalationContact,
    SessionConfigurationReference,
    TokenTelemetry,
]

out = Path("generated_schemas")
out.mkdir(exist_ok=True)
for model in MODELS:
    (out / f"{model.__name__}.schema.json").write_text(
        json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
print(f"exported {len(MODELS)} schemas to {out}")
