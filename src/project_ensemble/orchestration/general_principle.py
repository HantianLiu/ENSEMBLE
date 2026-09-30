from __future__ import annotations

import hashlib
import json
import secrets
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project_ensemble.domain import MeetingPhase, MeetingType, Persona
from project_ensemble.orchestration.amendments import AmendmentDocket, AmendmentSubmission, AmendmentType
from project_ensemble.orchestration.cosponsorship import SealedCosponsorshipLedger
from project_ensemble.orchestration.drafting_transition import DraftingTransitionRunner
from project_ensemble.orchestration.detailed_drafting import DetailedDraftingRunner
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.general_voting import GeneralRatificationRunner, GeneralVotingRunner
from project_ensemble.research.models import ResearchStage
from project_ensemble.research.rounds import ResearchRoundRunner
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.storage.meeting import MeetingRepository


class ProposedAmendment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1)
    declared_type: AmendmentType
    declared_impact_scope: list[str] = Field(min_length=1)

    @field_validator("declared_impact_scope")
    @classmethod
    def impact_scope_is_substantive(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("declared impact scope entries cannot be empty")
        return normalized


class GeneralPositionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["SUPPORT", "OPPOSE"]
    amendment: ProposedAmendment | None = None

    @model_validator(mode="after")
    def action_matches_amendment(self) -> "GeneralPositionAction":
        if self.action == "SUPPORT" and self.amendment is not None:
            raise ValueError("SUPPORT must not include an amendment")
        if self.action == "OPPOSE" and self.amendment is None:
            raise ValueError("OPPOSE requires one concrete amendment")
        return self


class CosponsorshipAction(BaseModel):
    """One Representative's sealed selection for the frozen proposal window."""

    model_config = ConfigDict(extra="forbid")
    cosponsor_item_ids: list[str] = Field(default_factory=list)

    @field_validator("cosponsor_item_ids")
    @classmethod
    def item_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("cosponsorship item IDs must be unique")
        return value


class GeneralPrincipleRunResult(BaseModel):
    meeting_id: str
    initial_drafter_id: str
    draft_path: str
    position_count: int
    amendment_count: int
    amendment_order: list[str]
    cosponsorship_submission_count: int
    ballot_count: int = 0
    adopted_amendment_count: int = 0
    rejected_amendment_count: int = 0
    deferred_amendment_count: int = 0
    current_draft_path: str | None = None
    completed_window_count: int = 0
    ratified: bool | None = None
    ratification_yes_votes: int | None = None
    ratification_required_yes_votes: int | None = None
    atomic_item_count: int | None = None
    retained_atomic_item_count: int | None = None
    primary_drafter_id: str | None = None
    primary_drafter_candidate_count: int | None = None
    consultative_count: int | None = None
    atomic_item_policy_status: str | None = None
    atomic_item_review_status: str | None = None
    detailed_draft_path: str | None = None
    detailed_clause_count: int | None = None
    detailed_review_submission_count: int | None = None
    detailed_clause_proposal_count: int | None = None
    detailed_split_motion_count: int | None = None
    detailed_suspension_motion_count: int | None = None
    adopted_detailed_split_motion_count: int | None = None
    adopted_detailed_suspension_motion_count: int | None = None
    detailed_option_set_count: int | None = None
    detailed_type_i_option_set_count: int | None = None
    detailed_type_ii_option_set_count: int | None = None
    detailed_type_iii_option_set_count: int | None = None
    detailed_initial_clause_ballot_count: int | None = None
    detailed_completed_option_set_count: int | None = None
    detailed_type_i_direct_adoption_count: int | None = None
    detailed_type_i_followup_count: int | None = None
    detailed_type_ii_direct_adoption_count: int | None = None
    detailed_type_ii_followup_count: int | None = None
    detailed_type_iii_runoff_count: int | None = None
    detailed_completed_type_iii_runoff_count: int | None = None
    detailed_type_iii_runoff_direct_adoption_count: int | None = None
    detailed_type_iii_runoff_explanation_count: int | None = None
    detailed_type_ii_explanation_submission_count: int | None = None
    detailed_type_ii_second_ballot_count: int | None = None
    detailed_type_ii_second_ballot_adoption_count: int | None = None
    detailed_type_ii_contested_outcome_count: int | None = None
    detailed_type_ii_new_option_count: int | None = None
    detailed_applied_proposal_count: int | None = None
    detailed_contested_proposal_count: int | None = None
    current_detailed_draft_path: str | None = None
    detailed_chair_certification_status: str | None = None
    detailed_certified_resolution_path: str | None = None
    detailed_librarian_review_count: int | None = None
    detailed_think_tank_material_finding_count: int | None = None
    detailed_think_tank_reconvene_recommendation_count: int | None = None
    detailed_chair_handoff_brief_path: str | None = None
    detailed_think_tank_execution_review_path: str | None = None
    detailed_think_tank_execution_review_count: int | None = None
    detailed_human_review_packet_path: str | None = None
    detailed_chair_readability_status: str | None = None
    detailed_chair_readability_certification_path: str | None = None
    detailed_final_report_markdown_path: str | None = None
    detailed_final_report_pdf_path: str | None = None
    detailed_final_publication_manifest_path: str | None = None
    detailed_minority_report_count: int | None = None
    detailed_audit_requester_count: int | None = None
    detailed_audit_petition_count: int | None = None
    detailed_audit_petition_bundle_path: str | None = None
    detailed_chair_accountability_report_path: str | None = None
    detailed_chair_accountability_cli_brief_path: str | None = None
    research_round_count: int = 0
    research_evidence_packet_count: int = 0
    next_phase: MeetingPhase
    paused_reason: str | None = None


class GeneralPrincipleRunner:
    """Run the policy-set portion of the deliberation general-principle stage."""

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
        continue_into_detailed: bool = False,
        continue_into_clause_review: bool = False,
        research_round_runner: ResearchRoundRunner | None = None,
    ):
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.repo = repo
        self.engine = engine
        self.governance_docs = Path(governance_docs)
        self.max_output_tokens = max_output_tokens
        self.continue_into_detailed = continue_into_detailed
        self.continue_into_clause_review = continue_into_clause_review
        self.research_round_runner = research_round_runner
        self.resolver = GovernanceDocumentResolver(self.governance_docs)
        self.assembler = RepresentativeContextAssembler()

    def run(self) -> GeneralPrincipleRunResult:
        manifest = json.loads(self.repo.docs.read_text("public/meeting_manifest.json"))
        if manifest.get("meeting_type") != MeetingType.DELIBERATION.value:
            raise ValueError("general-principle runner requires a deliberation meeting")
        task_path = self.repo.root / "public" / "task.json"
        if not task_path.exists():
            raise ValueError("meeting has no durable public task")
        registry = self._registry()
        if not registry:
            raise ValueError("deliberation meeting has no Representatives")

        self.engine.status.phase = MeetingPhase.INITIAL_DRAFT
        self.engine.progress.status(MeetingPhase.INITIAL_DRAFT, "选择初始起草人并生成 D0 草案")
        initial_drafter_id = self._select_initial_drafter([x["representative_id"] for x in registry])
        self.engine.progress.info(f"初始起草人：{initial_drafter_id}")
        draft_path = self._ensure_initial_draft(initial_drafter_id, registry, task_path)
        self.engine.progress.speech(initial_drafter_id, "D0 初始草案", draft_path.read_text(encoding="utf-8"))
        current_draft = draft_path
        first_positions: list[dict] = []
        first_order: list[str] = []
        position_count = amendment_count = cosponsorship_submission_count = 0
        ballot_count = adopted = rejected = deferred = completed_windows = 0
        paused_reason = None
        next_phase = MeetingPhase.GENERAL_POSITION

        for window_number in range(1, 4):
            base_name = f"D{window_number - 1}"
            target_name = f"D{window_number}"
            if self.research_round_runner is not None and window_number == 1:
                self.research_round_runner.run(
                    round_id=f"general-position-{window_number:03d}-pre",
                    stage=ResearchStage.PROPOSAL,
                    subject_files=(task_path, current_draft),
                    representative_records=registry,
                    max_claims_per_representative=4,
                )
            self.engine.status.phase = MeetingPhase.GENERAL_POSITION
            self.engine.progress.status(
                MeetingPhase.GENERAL_POSITION,
                f"第 {window_number}/3 轮（{base_name}→{target_name}）：收集 {len(registry)} 名 Representative 的独立立场；全部完成前不公开正文",
            )
            positions = self._collect_positions(
                registry,
                task_path,
                current_draft,
                window_number=window_number,
            )
            for position in positions:
                content = json.dumps(
                    {"action": position["action"], "amendment": position["amendment"]},
                    indent=2,
                    ensure_ascii=False,
                )
                self.engine.progress.speech(
                    position["representative_id"],
                    f"第 {window_number} 轮总则立场（窗口已冻结）",
                    content,
                )
            order = self._freeze_and_order_amendments(positions, window_number=window_number)
            if self.research_round_runner is not None and order:
                self.research_round_runner.run(
                    round_id=f"general-position-{window_number:03d}-post",
                    stage=ResearchStage.CHALLENGE,
                    subject_files=(
                        task_path,
                        current_draft,
                        self.repo.root / self._public_positions_relative(window_number),
                        self.repo.root / self._docket_relative(window_number),
                    ),
                    representative_records=registry,
                    max_claims_per_representative=2,
                )
            self.engine.status.phase = MeetingPhase.COSPONSORSHIP
            self.engine.progress.status(
                MeetingPhase.COSPONSORSHIP,
                f"第 {window_number} 轮立场窗口已冻结；修正案随机顺序为 {order or '（无修正案）'}",
            )
            cosponsor_count = self._collect_cosponsorship(
                registry,
                task_path,
                current_draft,
                order,
                window_number=window_number,
                base_item_id=base_name,
            )
            voting = GeneralVotingRunner(
                repo=self.repo,
                engine=self.engine,
                governance_docs=self.governance_docs,
                max_output_tokens=self.max_output_tokens,
            ).run(
                draft_path=current_draft,
                docket_path=self.repo.root / self._docket_relative(window_number),
                window_number=window_number,
                target_draft_name=target_name,
            )
            if window_number == 1:
                first_positions, first_order = positions, order
            position_count += len(positions)
            amendment_count += sum(1 for item in positions if item.get("amendment") is not None)
            cosponsorship_submission_count += cosponsor_count
            ballot_count += voting.ballot_count
            adopted += voting.adopted_amendment_count
            rejected += voting.rejected_amendment_count
            deferred += voting.deferred_amendment_count
            current_draft = self.repo.root / voting.draft_path
            next_phase = voting.next_phase
            paused_reason = voting.paused_reason
            if voting.next_phase == MeetingPhase.PAUSED:
                break
            completed_windows += 1

        ratified = None
        ratification_yes_votes = None
        ratification_required_yes_votes = None
        atomic_item_count = None
        retained_atomic_item_count = None
        primary_drafter_id = None
        primary_drafter_candidate_count = None
        consultative_count = None
        atomic_item_policy_status = None
        atomic_item_review_status = None
        detailed_draft_path = None
        detailed_clause_count = None
        detailed_review_submission_count = None
        detailed_clause_proposal_count = None
        detailed_split_motion_count = None
        detailed_suspension_motion_count = None
        adopted_detailed_split_motion_count = None
        adopted_detailed_suspension_motion_count = None
        detailed_option_set_count = None
        detailed_type_i_option_set_count = None
        detailed_type_ii_option_set_count = None
        detailed_type_iii_option_set_count = None
        detailed_initial_clause_ballot_count = None
        detailed_completed_option_set_count = None
        detailed_type_i_direct_adoption_count = None
        detailed_type_i_followup_count = None
        detailed_type_ii_direct_adoption_count = None
        detailed_type_ii_followup_count = None
        detailed_type_iii_runoff_count = None
        detailed_completed_type_iii_runoff_count = None
        detailed_type_iii_runoff_direct_adoption_count = None
        detailed_type_iii_runoff_explanation_count = None
        detailed_type_ii_explanation_submission_count = None
        detailed_type_ii_second_ballot_count = None
        detailed_type_ii_second_ballot_adoption_count = None
        detailed_type_ii_contested_outcome_count = None
        detailed_type_ii_new_option_count = None
        detailed_applied_proposal_count = None
        detailed_contested_proposal_count = None
        current_detailed_draft_path = None
        detailed_chair_certification_status = None
        detailed_certified_resolution_path = None
        detailed_librarian_review_count = None
        detailed_think_tank_material_finding_count = None
        detailed_think_tank_reconvene_recommendation_count = None
        detailed_chair_handoff_brief_path = None
        detailed_think_tank_execution_review_path = None
        detailed_think_tank_execution_review_count = None
        detailed_human_review_packet_path = None
        detailed_chair_readability_status = None
        detailed_chair_readability_certification_path = None
        detailed_final_report_markdown_path = None
        detailed_final_report_pdf_path = None
        detailed_final_publication_manifest_path = None
        detailed_minority_report_count = None
        detailed_audit_requester_count = None
        detailed_audit_petition_count = None
        detailed_audit_petition_bundle_path = None
        detailed_chair_accountability_report_path = None
        detailed_chair_accountability_cli_brief_path = None
        if completed_windows == 3:
            if self.research_round_runner is not None:
                self.research_round_runner.run(
                    round_id="general-ratification-001",
                    stage=ResearchStage.VETO,
                    subject_files=(task_path, current_draft),
                    representative_records=registry,
                    max_claims_per_representative=2,
                )
            ratification = GeneralRatificationRunner(
                repo=self.repo,
                engine=self.engine,
                governance_docs=self.governance_docs,
                max_output_tokens=self.max_output_tokens,
            ).run(draft_path=current_draft)
            ballot_count += 1
            ratified = ratification.passed
            ratification_yes_votes = ratification.yes_votes
            ratification_required_yes_votes = ratification.required_yes_votes
            next_phase = ratification.next_phase
            paused_reason = ratification.paused_reason
            if ratification.passed:
                transition = DraftingTransitionRunner(repo=self.repo, engine=self.engine).run(
                    final_draft=current_draft
                )
                atomic_item_count = transition.atomic_item_count
                retained_atomic_item_count = transition.retained_atomic_item_count
                primary_drafter_id = transition.primary_drafter_id
                primary_drafter_candidate_count = transition.primary_drafter_candidate_count
                consultative_count = transition.consultative_count
                atomic_item_policy_status = "TRIAL"
                atomic_item_review_status = transition.trial_review_status
                next_phase = transition.next_phase
                paused_reason = transition.paused_reason
                if self.continue_into_detailed and transition.next_phase == MeetingPhase.DETAILED_DRAFTING:
                    detailed = DetailedDraftingRunner(
                        repo=self.repo,
                        engine=self.engine,
                        governance_docs=self.governance_docs,
                        max_output_tokens=self.max_output_tokens,
                        continue_into_clause_review=self.continue_into_clause_review,
                        research_round_runner=self.research_round_runner,
                    ).run(general_principle_path=current_draft)
                    detailed_draft_path = detailed.draft_path
                    detailed_clause_count = detailed.clause_count
                    detailed_review_submission_count = detailed.review_submission_count
                    detailed_clause_proposal_count = detailed.clause_proposal_count
                    detailed_split_motion_count = detailed.split_motion_count
                    detailed_suspension_motion_count = detailed.suspension_motion_count
                    adopted_detailed_split_motion_count = detailed.adopted_split_motion_count
                    adopted_detailed_suspension_motion_count = (
                        detailed.adopted_suspension_motion_count
                    )
                    detailed_option_set_count = detailed.option_set_count
                    detailed_type_i_option_set_count = detailed.type_i_option_set_count
                    detailed_type_ii_option_set_count = detailed.type_ii_option_set_count
                    detailed_type_iii_option_set_count = detailed.type_iii_option_set_count
                    detailed_initial_clause_ballot_count = detailed.initial_clause_ballot_count
                    detailed_completed_option_set_count = detailed.completed_option_set_count
                    detailed_type_i_direct_adoption_count = detailed.type_i_direct_adoption_count
                    detailed_type_i_followup_count = detailed.type_i_followup_count
                    detailed_type_ii_direct_adoption_count = detailed.type_ii_direct_adoption_count
                    detailed_type_ii_followup_count = detailed.type_ii_followup_count
                    detailed_type_iii_runoff_count = detailed.type_iii_runoff_count
                    detailed_completed_type_iii_runoff_count = (
                        detailed.completed_type_iii_runoff_count
                    )
                    detailed_type_iii_runoff_direct_adoption_count = (
                        detailed.type_iii_runoff_direct_adoption_count
                    )
                    detailed_type_iii_runoff_explanation_count = (
                        detailed.type_iii_runoff_explanation_count
                    )
                    detailed_type_ii_explanation_submission_count = (
                        detailed.type_ii_explanation_submission_count
                    )
                    detailed_type_ii_second_ballot_count = (
                        detailed.type_ii_second_ballot_count
                    )
                    detailed_type_ii_second_ballot_adoption_count = (
                        detailed.type_ii_second_ballot_adoption_count
                    )
                    detailed_type_ii_contested_outcome_count = (
                        detailed.type_ii_contested_outcome_count
                    )
                    detailed_type_ii_new_option_count = detailed.type_ii_new_option_count
                    detailed_applied_proposal_count = detailed.applied_detailed_proposal_count
                    detailed_contested_proposal_count = (
                        detailed.detailed_contested_proposal_count
                    )
                    current_detailed_draft_path = detailed.current_detailed_draft_path
                    detailed_chair_certification_status = detailed.chair_certification_status
                    detailed_certified_resolution_path = detailed.certified_resolution_path
                    detailed_librarian_review_count = detailed.librarian_review_count
                    detailed_think_tank_material_finding_count = (
                        detailed.think_tank_material_finding_count
                    )
                    detailed_think_tank_reconvene_recommendation_count = (
                        detailed.think_tank_reconvene_recommendation_count
                    )
                    detailed_chair_handoff_brief_path = detailed.chair_handoff_brief_path
                    detailed_think_tank_execution_review_path = (
                        detailed.think_tank_execution_review_path
                    )
                    detailed_think_tank_execution_review_count = (
                        detailed.think_tank_execution_review_count
                    )
                    detailed_human_review_packet_path = detailed.human_review_packet_path
                    detailed_chair_readability_status = detailed.chair_readability_status
                    detailed_chair_readability_certification_path = (
                        detailed.chair_readability_certification_path
                    )
                    detailed_final_report_markdown_path = (
                        detailed.final_report_markdown_path
                    )
                    detailed_final_report_pdf_path = detailed.final_report_pdf_path
                    detailed_final_publication_manifest_path = (
                        detailed.final_publication_manifest_path
                    )
                    detailed_minority_report_count = detailed.minority_report_count
                    detailed_audit_requester_count = detailed.audit_requester_count
                    detailed_audit_petition_count = detailed.audit_petition_count
                    detailed_audit_petition_bundle_path = (
                        detailed.audit_petition_bundle_path
                    )
                    detailed_chair_accountability_report_path = (
                        detailed.chair_accountability_report_path
                    )
                    detailed_chair_accountability_cli_brief_path = (
                        detailed.chair_accountability_cli_brief_path
                    )
                    next_phase = detailed.next_phase
                    paused_reason = detailed.paused_reason
        research_round_count, research_evidence_packet_count = (
            ResearchRoundRunner.released_summary(self.repo)
        )
        return GeneralPrincipleRunResult(
            meeting_id=self.repo.meeting_id,
            initial_drafter_id=initial_drafter_id,
            draft_path=str(draft_path.relative_to(self.repo.root)),
            position_count=position_count,
            amendment_count=amendment_count,
            amendment_order=first_order,
            cosponsorship_submission_count=cosponsorship_submission_count,
            ballot_count=ballot_count,
            adopted_amendment_count=adopted,
            rejected_amendment_count=rejected,
            deferred_amendment_count=deferred,
            current_draft_path=str(current_draft.relative_to(self.repo.root)),
            completed_window_count=completed_windows,
            ratified=ratified,
            ratification_yes_votes=ratification_yes_votes,
            ratification_required_yes_votes=ratification_required_yes_votes,
            atomic_item_count=atomic_item_count,
            retained_atomic_item_count=retained_atomic_item_count,
            primary_drafter_id=primary_drafter_id,
            primary_drafter_candidate_count=primary_drafter_candidate_count,
            consultative_count=consultative_count,
            atomic_item_policy_status=atomic_item_policy_status,
            atomic_item_review_status=atomic_item_review_status,
            detailed_draft_path=detailed_draft_path,
            detailed_clause_count=detailed_clause_count,
            detailed_review_submission_count=detailed_review_submission_count,
            detailed_clause_proposal_count=detailed_clause_proposal_count,
            detailed_split_motion_count=detailed_split_motion_count,
            detailed_suspension_motion_count=detailed_suspension_motion_count,
            adopted_detailed_split_motion_count=adopted_detailed_split_motion_count,
            adopted_detailed_suspension_motion_count=adopted_detailed_suspension_motion_count,
            detailed_option_set_count=detailed_option_set_count,
            detailed_type_i_option_set_count=detailed_type_i_option_set_count,
            detailed_type_ii_option_set_count=detailed_type_ii_option_set_count,
            detailed_type_iii_option_set_count=detailed_type_iii_option_set_count,
            detailed_initial_clause_ballot_count=detailed_initial_clause_ballot_count,
            detailed_completed_option_set_count=detailed_completed_option_set_count,
            detailed_type_i_direct_adoption_count=detailed_type_i_direct_adoption_count,
            detailed_type_i_followup_count=detailed_type_i_followup_count,
            detailed_type_ii_direct_adoption_count=detailed_type_ii_direct_adoption_count,
            detailed_type_ii_followup_count=detailed_type_ii_followup_count,
            detailed_type_iii_runoff_count=detailed_type_iii_runoff_count,
            detailed_completed_type_iii_runoff_count=detailed_completed_type_iii_runoff_count,
            detailed_type_iii_runoff_direct_adoption_count=(
                detailed_type_iii_runoff_direct_adoption_count
            ),
            detailed_type_iii_runoff_explanation_count=(
                detailed_type_iii_runoff_explanation_count
            ),
            detailed_type_ii_explanation_submission_count=(
                detailed_type_ii_explanation_submission_count
            ),
            detailed_type_ii_second_ballot_count=detailed_type_ii_second_ballot_count,
            detailed_type_ii_second_ballot_adoption_count=(
                detailed_type_ii_second_ballot_adoption_count
            ),
            detailed_type_ii_contested_outcome_count=(
                detailed_type_ii_contested_outcome_count
            ),
            detailed_type_ii_new_option_count=detailed_type_ii_new_option_count,
            detailed_applied_proposal_count=detailed_applied_proposal_count,
            detailed_contested_proposal_count=detailed_contested_proposal_count,
            current_detailed_draft_path=current_detailed_draft_path,
            detailed_chair_certification_status=detailed_chair_certification_status,
            detailed_certified_resolution_path=detailed_certified_resolution_path,
            detailed_librarian_review_count=detailed_librarian_review_count,
            detailed_think_tank_material_finding_count=(
                detailed_think_tank_material_finding_count
            ),
            detailed_think_tank_reconvene_recommendation_count=(
                detailed_think_tank_reconvene_recommendation_count
            ),
            detailed_chair_handoff_brief_path=detailed_chair_handoff_brief_path,
            detailed_think_tank_execution_review_path=(
                detailed_think_tank_execution_review_path
            ),
            detailed_think_tank_execution_review_count=(
                detailed_think_tank_execution_review_count
            ),
            detailed_human_review_packet_path=detailed_human_review_packet_path,
            detailed_chair_readability_status=detailed_chair_readability_status,
            detailed_chair_readability_certification_path=(
                detailed_chair_readability_certification_path
            ),
            detailed_final_report_markdown_path=detailed_final_report_markdown_path,
            detailed_final_report_pdf_path=detailed_final_report_pdf_path,
            detailed_final_publication_manifest_path=(
                detailed_final_publication_manifest_path
            ),
            detailed_minority_report_count=detailed_minority_report_count,
            detailed_audit_requester_count=detailed_audit_requester_count,
            detailed_audit_petition_count=detailed_audit_petition_count,
            detailed_audit_petition_bundle_path=detailed_audit_petition_bundle_path,
            detailed_chair_accountability_report_path=(
                detailed_chair_accountability_report_path
            ),
            detailed_chair_accountability_cli_brief_path=(
                detailed_chair_accountability_cli_brief_path
            ),
            research_round_count=research_round_count,
            research_evidence_packet_count=research_evidence_packet_count,
            next_phase=next_phase,
            paused_reason=paused_reason,
        )

    def _registry(self) -> list[dict]:
        return json.loads(self.repo.docs.read_text("identity_private/representative_registry.json"))

    def _select_initial_drafter(self, representative_ids: list[str]) -> str:
        relative = Path("governance_private/general_principle/initial_drafter_selection.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return str(json.loads(absolute.read_text(encoding="utf-8"))["representative_id"])
        seed = secrets.token_hex(32)
        seed_bytes = bytes.fromhex(seed)
        ordered = sorted(
            representative_ids,
            key=lambda rid: (hashlib.sha256(seed_bytes + b"\0" + rid.encode("utf-8")).digest(), rid),
        )
        selected = ordered[0]
        record = {
            "seed": seed,
            "algorithm": "sha256(seed_bytes || NUL || representative_id), ascending digest; select first",
            "representative_id": selected,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2))
        self.repo.events.append(
            "INITIAL_DRAFTER_SELECTED",
            {"meeting_id": self.repo.meeting_id, "representative_id": selected, "record_path": str(relative)},
            actor="orchestrator",
        )
        return selected

    def _ensure_initial_draft(self, drafter_id: str, registry: list[dict], task_path: Path) -> Path:
        relative = Path("public/general_principle/D0.md")
        absolute = self.repo.root / relative
        if absolute.exists():
            return absolute
        record = next(x for x in registry if x["representative_id"] == drafter_id)
        persona = Persona(record["runtime"]["persona"])
        spec = self.resolver.representative_context_spec(
            persona=persona,
            stage="initial_draft",
            representative_id=drafter_id,
            public_state_files=(task_path,),
        )
        response = self.engine.invoke_participant(
            drafter_id,
            system_text=self.assembler.assemble(spec),
            user_text="Produce the initial general-principle draft. Return only the draft text, without commentary.",
            stage="initial_draft",
            max_output_tokens=self.max_output_tokens,
        )
        self.repo.docs.write_once(relative, response.text.strip() + "\n")
        self.repo.docs.write_once(
            "public/general_principle/D0.provenance.json",
            json.dumps({"draft": "D0", "author_id": drafter_id}, indent=2),
        )
        self.repo.events.append(
            "INITIAL_DRAFT_FROZEN",
            {"meeting_id": self.repo.meeting_id, "draft": "D0", "author_id": drafter_id},
            actor="orchestrator",
        )
        return absolute

    def _collect_positions(
        self,
        registry: list[dict],
        task_path: Path,
        draft_path: Path,
        *,
        window_number: int = 1,
    ) -> list[dict]:
        positions_by_id: dict[str, dict] = {}
        missing_records: list[dict] = []
        total = len(registry)
        for record in registry:
            representative_id = record["representative_id"]
            relative = self._position_relative(window_number, representative_id)
            absolute = self.repo.root / relative
            if absolute.exists():
                positions_by_id[representative_id] = json.loads(
                    absolute.read_text(encoding="utf-8")
                )
                self.engine.progress.info(
                    f"已恢复 {len(positions_by_id)}/{total} 份密封立场"
                )
            else:
                missing_records.append(record)

        persistence_lock = threading.Lock()

        def persist_position(result: tuple[str, dict, Path]) -> None:
            representative_id, position, relative = result
            # Multiple model lanes may finish together. Keep each sealed write,
            # hash-chain event, and progress count as one short critical section.
            with persistence_lock:
                self.repo.docs.write_once(
                    relative, json.dumps(position, indent=2, ensure_ascii=False)
                )
                self.repo.events.append(
                    "GENERAL_POSITION_SUBMITTED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "representative_id": representative_id,
                        "window_id": f"general-principle-{window_number:03d}",
                        "has_amendment": position["amendment"] is not None,
                        "record_path": str(relative),
                    },
                    actor=representative_id,
                )
                positions_by_id[representative_id] = position
                self.engine.progress.info(
                    f"已收到 {len(positions_by_id)}/{total} 份立场；正文继续密封"
                )

        run_bounded_representative_lanes(
            missing_records,
            lambda record: self._collect_one_position(
                record=record,
                task_path=task_path,
                draft_path=draft_path,
                window_number=window_number,
            ),
            getattr(self.engine, "model_concurrency_limit", lambda *_args: 1),
            on_result=persist_position,
            progress=self.engine.progress,
            progress_records=registry,
            completed_participant_ids=positions_by_id,
        )
        positions = [
            positions_by_id[record["representative_id"]] for record in registry
        ]

        public_relative = self._public_positions_relative(window_number)
        if not (self.repo.root / public_relative).exists():
            self.repo.docs.write_once(public_relative, json.dumps(positions, indent=2, ensure_ascii=False))
            self.repo.events.append(
                "GENERAL_POSITION_WINDOW_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "window_id": f"general-principle-{window_number:03d}",
                    "submission_count": len(positions),
                },
                actor="orchestrator",
            )
        return positions

    def _collect_one_position(
        self,
        *,
        record: dict,
        task_path: Path,
        draft_path: Path,
        window_number: int,
    ) -> tuple[str, dict, Path]:
        representative_id = record["representative_id"]
        persona = Persona(record["runtime"]["persona"])
        spec = self.resolver.representative_context_spec(
            persona=persona,
            stage="general_position",
            representative_id=representative_id,
            public_state_files=(task_path, draft_path),
        )
        system_text = self.assembler.assemble(spec)
        user_text = (
            "Return exactly one JSON object. Legal forms are "
            '{"action":"SUPPORT"} or '
            '{"action":"OPPOSE","amendment":{"text":"...",'
            '"declared_type":"OBJECTION|SUPPLEMENTARY",'
            '"declared_impact_scope":["target clause or item"]}}. '
            "Do not use Markdown fences or add other keys."
        )
        response = self.engine.find_recorded_response(
            representative_id,
            system_text=system_text,
            user_text=user_text,
            stage="general_position",
        )
        if response is None:
            response = self.engine.invoke_participant(
                representative_id,
                system_text=system_text,
                user_text=user_text,
                stage="general_position",
                max_output_tokens=self.max_output_tokens,
            )
        else:
            self.engine.progress.info(
                f"{representative_id} · 恢复上次已返回但未通过 schema 的原始响应"
            )
        parsed = self.engine.validate_structured_response(
            representative_id,
            response=response,
            schema_model=GeneralPositionAction,
            stage="general_position",
            max_output_tokens=self.max_output_tokens,
            semantic_requirement=(
                "Preserve whether the participant chose SUPPORT or OPPOSE. For OPPOSE, preserve the "
                "complete amendment text, declared type, and every declared impact-scope item."
            ),
        )
        position: dict = {
            "representative_id": representative_id,
            "action": parsed.action,
            "amendment": None,
        }
        if parsed.amendment is not None:
            amendment = AmendmentSubmission(
                amendment_id="A-" + secrets.token_hex(4).upper(),
                proposer_id=representative_id,
                text=parsed.amendment.text,
                declared_type=parsed.amendment.declared_type,
                declared_impact_scope=parsed.amendment.declared_impact_scope,
            )
            position["amendment"] = amendment.model_dump(mode="json")
        return (
            representative_id,
            position,
            self._position_relative(window_number, representative_id),
        )

    def _freeze_and_order_amendments(
        self,
        positions: list[dict],
        *,
        window_number: int = 1,
    ) -> list[str]:
        public_relative = self._docket_relative(window_number)
        public_absolute = self.repo.root / public_relative
        if public_absolute.exists():
            return list(json.loads(public_absolute.read_text(encoding="utf-8"))["random_order"]["order"])
        amendments = [AmendmentSubmission.model_validate(x["amendment"]) for x in positions if x["amendment"]]
        randomized = AmendmentDocket(amendments).randomized()
        record = {
            "window_id": f"general-principle-{window_number:03d}",
            "window_status": "FROZEN",
            "amendments": [x.model_dump(mode="json") for x in amendments],
            "random_order": randomized.model_dump(mode="json"),
        }
        self.repo.docs.write_once(public_relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "AMENDMENT_DOCKET_RANDOMIZED",
            {
                "meeting_id": self.repo.meeting_id,
                "window_id": f"general-principle-{window_number:03d}",
                "amendment_count": len(amendments),
                "order": randomized.order,
                "record_path": str(public_relative),
            },
            actor="orchestrator",
        )
        self.repo.events.append(
            "MEETING_PHASE_CHANGED",
            {
                "meeting_id": self.repo.meeting_id,
                "from_phase": MeetingPhase.GENERAL_POSITION.value,
                "to_phase": MeetingPhase.COSPONSORSHIP.value,
                "cause": "GENERAL_POSITION_WINDOW_FROZEN",
            },
            actor="orchestrator",
        )
        return randomized.order

    def _collect_cosponsorship(
        self,
        registry: list[dict],
        task_path: Path,
        draft_path: Path,
        amendment_order: list[str],
        *,
        window_number: int = 1,
        base_item_id: str = "D0",
    ) -> int:
        frozen_relative = self._cosponsorship_frozen_relative(window_number)
        frozen_absolute = self.repo.root / frozen_relative
        if frozen_absolute.exists():
            frozen = json.loads(frozen_absolute.read_text(encoding="utf-8"))
            return int(frozen["submission_count"])

        public_positions = self.repo.root / self._public_positions_relative(window_number)
        public_docket = self.repo.root / self._docket_relative(window_number)
        item_ids = [base_item_id, *amendment_order]
        allowed_item_ids = set(item_ids)
        ledger = SealedCosponsorshipLedger(
            {record["representative_id"] for record in registry},
            allowed_item_ids,
        )
        total = len(registry)

        for index, record in enumerate(registry, start=1):
            representative_id = record["representative_id"]
            relative = (
                self._cosponsorship_submissions_root(window_number)
                / f"{representative_id}.json"
            )
            absolute = self.repo.root / relative
            if absolute.exists():
                submission = CosponsorshipAction.model_validate_json(absolute.read_text(encoding="utf-8"))
                self.engine.progress.info(f"已恢复 {index}/{total} 份密封联署选择")
            else:
                persona = Persona(record["runtime"]["persona"])
                spec = self.resolver.representative_context_spec(
                    persona=persona,
                    stage="cosponsorship",
                    representative_id=representative_id,
                    public_state_files=(task_path, draft_path, public_positions, public_docket),
                )
                response = self.engine.invoke_participant(
                    representative_id,
                    system_text=self.assembler.assemble(spec),
                    user_text=(
                        "Return exactly one JSON object of the form "
                        '{"cosponsor_item_ids":["D0","A-..."]}. '
                        f"Choose zero or more unique IDs only from {item_ids!r}. "
                        "Use an empty list to co-sponsor nothing. Do not use Markdown fences "
                        "or add other keys."
                    ),
                    stage="cosponsorship",
                    max_output_tokens=self.max_output_tokens,
                )
                try:
                    submission = CosponsorshipAction.model_validate(json.loads(response.text))
                except (json.JSONDecodeError, ValidationError, TypeError):
                    self.engine.pause_for_unconfigured_policy(
                        participant_id=representative_id,
                        reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                    )
                self.repo.docs.write_once(
                    relative,
                    submission.model_dump_json(indent=2),
                )
                self.repo.events.append(
                    "COSPONSORSHIP_SUBMITTED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "representative_id": representative_id,
                        "record_path": str(relative),
                    },
                    actor=representative_id,
                )
                self.engine.progress.info(f"已收到 {index}/{total} 份密封联署选择；内容继续保密")

            unknown = set(submission.cosponsor_item_ids) - allowed_item_ids
            if unknown:
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            for item_id in submission.cosponsor_item_ids:
                ledger.add(representative_id, item_id)

        ledger.freeze()
        frozen = {
            "window_id": f"general-principle-{window_number:03d}",
            "status": "FROZEN",
            "item_ids": item_ids,
            "submission_count": total,
            "support": ledger.governance_private_snapshot(),
        }
        self.repo.docs.write_once(frozen_relative, json.dumps(frozen, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "COSPONSORSHIP_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "window_id": f"general-principle-{window_number:03d}",
                "submission_count": total,
                "record_path": str(frozen_relative),
            },
            actor="orchestrator",
        )
        return total

    @staticmethod
    def _position_relative(window_number: int, representative_id: str) -> Path:
        if window_number == 1:
            return Path("governance_private/general_principle/positions") / f"{representative_id}.json"
        return (
            Path("governance_private/general_principle/windows")
            / f"window_{window_number:03d}"
            / "positions"
            / f"{representative_id}.json"
        )

    @staticmethod
    def _public_positions_relative(window_number: int) -> Path:
        name = "general_positions.json" if window_number == 1 else f"general_positions_window_{window_number:03d}.json"
        return Path("public/general_principle") / name

    @staticmethod
    def _docket_relative(window_number: int) -> Path:
        return Path("public/general_principle") / f"amendment_docket_window_{window_number:03d}.json"

    @staticmethod
    def _cosponsorship_submissions_root(window_number: int) -> Path:
        base = Path("governance_private/general_principle/cosponsorship")
        return base / "submissions" if window_number == 1 else base / f"window_{window_number:03d}" / "submissions"

    @staticmethod
    def _cosponsorship_frozen_relative(window_number: int) -> Path:
        base = Path("governance_private/general_principle/cosponsorship")
        return base / "frozen_ledger.json" if window_number == 1 else base / f"window_{window_number:03d}" / "frozen_ledger.json"
