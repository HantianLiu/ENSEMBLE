from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.orchestration.clause_review import ClauseReviewRunner
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.research.rounds import ResearchRoundRunner
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.storage.meeting import MeetingRepository


class DetailedDraftingResult(BaseModel):
    draft_path: str
    primary_drafter_id: str
    next_phase: MeetingPhase
    paused_reason: str | None = None
    clause_count: int | None = None
    review_submission_count: int | None = None
    clause_proposal_count: int | None = None
    split_motion_count: int | None = None
    suspension_motion_count: int | None = None
    adopted_split_motion_count: int | None = None
    adopted_suspension_motion_count: int | None = None
    option_set_count: int | None = None
    type_i_option_set_count: int | None = None
    type_ii_option_set_count: int | None = None
    type_iii_option_set_count: int | None = None
    initial_clause_ballot_count: int | None = None
    completed_option_set_count: int | None = None
    type_i_direct_adoption_count: int | None = None
    type_i_followup_count: int | None = None
    type_ii_direct_adoption_count: int | None = None
    type_ii_followup_count: int | None = None
    type_iii_runoff_count: int | None = None
    completed_type_iii_runoff_count: int | None = None
    type_iii_runoff_direct_adoption_count: int | None = None
    type_iii_runoff_explanation_count: int | None = None
    type_ii_explanation_submission_count: int | None = None
    type_ii_second_ballot_count: int | None = None
    type_ii_second_ballot_adoption_count: int | None = None
    type_ii_contested_outcome_count: int | None = None
    type_ii_new_option_count: int | None = None
    applied_detailed_proposal_count: int | None = None
    detailed_contested_proposal_count: int | None = None
    current_detailed_draft_path: str | None = None
    chair_certification_status: str | None = None
    certified_resolution_path: str | None = None
    librarian_review_count: int | None = None
    think_tank_material_finding_count: int | None = None
    think_tank_reconvene_recommendation_count: int | None = None
    chair_handoff_brief_path: str | None = None
    think_tank_execution_review_path: str | None = None
    think_tank_execution_review_count: int | None = None
    human_review_packet_path: str | None = None
    chair_readability_status: str | None = None
    chair_readability_certification_path: str | None = None
    final_report_markdown_path: str | None = None
    final_report_pdf_path: str | None = None
    final_publication_manifest_path: str | None = None
    minority_report_count: int | None = None
    audit_requester_count: int | None = None
    audit_petition_count: int | None = None
    audit_petition_bundle_path: str | None = None
    chair_accountability_report_path: str | None = None
    chair_accountability_cli_brief_path: str | None = None


class DetailedDraftingRunner:
    """Freeze C1: the Primary Drafter's independent first detailed-clause draft."""

    BOUNDARY_REASON = "DETAILED_CLAUSE_DOCKET_POLICY_NOT_CONFIGURED"

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
        continue_into_clause_review: bool = False,
        research_round_runner: ResearchRoundRunner | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.max_output_tokens = max_output_tokens
        self.continue_into_clause_review = continue_into_clause_review
        self.research_round_runner = research_round_runner
        self.resolver = GovernanceDocumentResolver(governance_docs)
        self.assembler = RepresentativeContextAssembler()

    def run(self, *, general_principle_path: Path) -> DetailedDraftingResult:
        transition_path = self.repo.root / "identity_private/general_principle/status_transition.json"
        if not transition_path.exists():
            raise ValueError("detailed drafting requires a frozen representative status transition")
        transition = json.loads(transition_path.read_text(encoding="utf-8"))
        primary_drafter_id = str(transition["primary_drafter_id"])
        status = transition["representative_statuses"].get(primary_drafter_id)
        if status != "ACTIVE":
            raise ValueError("Primary Drafter must remain ACTIVE for detailed drafting")

        draft_relative = Path("public/detailed_clauses/C0.md")
        draft_path = self.repo.root / draft_relative
        self.engine.status.phase = MeetingPhase.DETAILED_DRAFTING
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.DETAILED_DRAFTING,
            f"Primary Drafter {primary_drafter_id} 正在依据已批准 D3 独立形成细则第一稿 C0",
        )
        if not draft_path.exists():
            registry = json.loads(
                self.repo.docs.read_text("identity_private/representative_registry.json")
            )
            record = next(
                item for item in registry if item["representative_id"] == primary_drafter_id
            )
            own_status = (
                self.repo.root
                / "representatives"
                / primary_drafter_id
                / "status_transition_001.json"
            )
            public_transition = self.repo.root / "public/general_principle/status_transition.json"
            spec = self.resolver.representative_context_spec(
                persona=Persona(record["runtime"]["persona"]),
                stage="primary_drafter",
                representative_id=primary_drafter_id,
                public_state_files=(
                    self.repo.root / "public/task.json",
                    general_principle_path,
                    public_transition,
                ),
                own_state_files=(own_status,),
            )
            response = self.engine.invoke_participant(
                primary_drafter_id,
                system_text=self.assembler.assemble(spec),
                user_text=(
                    "Produce the first detailed-clause draft implementing the ratified general principle. "
                    "Translate every operative requirement into concrete, ordered, independently reviewable "
                    "clauses. Preserve uncertainty, evidence requirements, dissent-sensitive safeguards, units, "
                    "acceptance criteria, and provenance references where relevant. Do not silently weaken or "
                    "expand the ratified principle. Return only the complete draft text, without commentary or "
                    "Markdown fences."
                ),
                stage="primary_drafter",
                max_output_tokens=self.max_output_tokens,
            )
            text = response.text.strip() + "\n"
            self.repo.docs.write_once(draft_relative, text)
            provenance_relative = Path("public/detailed_clauses/C0.provenance.json")
            self.repo.docs.write_once(
                provenance_relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "draft": "C0",
                        "primary_drafter_id": primary_drafter_id,
                        "general_principle_path": str(general_principle_path.relative_to(self.repo.root)),
                        "general_principle_sha256": self._sha256(
                            general_principle_path.read_text(encoding="utf-8")
                        ),
                        "draft_sha256": self._sha256(text),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "DETAILED_CLAUSES_INITIAL_DRAFT_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "draft": "C0",
                    "primary_drafter_id": primary_drafter_id,
                    "record_path": str(draft_relative),
                    "provenance_path": str(provenance_relative),
                },
                actor=primary_drafter_id,
            )
        self.engine.progress.speech(
            primary_drafter_id,
            "细则第一稿 C0（已冻结）",
            draft_path.read_text(encoding="utf-8"),
        )
        if self.continue_into_clause_review:
            review = ClauseReviewRunner(
                repo=self.repo,
                engine=self.engine,
                governance_docs=self.resolver.root,
                max_output_tokens=self.max_output_tokens,
                research_round_runner=self.research_round_runner,
            ).run(draft_path=draft_path, general_principle_path=general_principle_path)
            return DetailedDraftingResult(
                draft_path=str(draft_relative),
                primary_drafter_id=primary_drafter_id,
                next_phase=review.next_phase,
                paused_reason=review.paused_reason,
                clause_count=review.clause_count,
                review_submission_count=review.review_submission_count,
                clause_proposal_count=review.proposal_count,
                split_motion_count=review.split_motion_count,
                suspension_motion_count=review.suspension_motion_count,
                adopted_split_motion_count=review.adopted_split_motion_count,
                adopted_suspension_motion_count=review.adopted_suspension_motion_count,
                option_set_count=review.option_set_count,
                type_i_option_set_count=review.type_i_option_set_count,
                type_ii_option_set_count=review.type_ii_option_set_count,
                type_iii_option_set_count=review.type_iii_option_set_count,
                initial_clause_ballot_count=review.initial_clause_ballot_count,
                completed_option_set_count=review.completed_option_set_count,
                type_i_direct_adoption_count=review.type_i_direct_adoption_count,
                type_i_followup_count=review.type_i_followup_count,
                type_ii_direct_adoption_count=review.type_ii_direct_adoption_count,
                type_ii_followup_count=review.type_ii_followup_count,
                type_iii_runoff_count=review.type_iii_runoff_count,
                completed_type_iii_runoff_count=review.completed_type_iii_runoff_count,
                type_iii_runoff_direct_adoption_count=(
                    review.type_iii_runoff_direct_adoption_count
                ),
                type_iii_runoff_explanation_count=review.type_iii_runoff_explanation_count,
                type_ii_explanation_submission_count=(
                    review.type_ii_explanation_submission_count
                ),
                type_ii_second_ballot_count=review.type_ii_second_ballot_count,
                type_ii_second_ballot_adoption_count=(
                    review.type_ii_second_ballot_adoption_count
                ),
                type_ii_contested_outcome_count=review.type_ii_contested_outcome_count,
                type_ii_new_option_count=review.type_ii_new_option_count,
                applied_detailed_proposal_count=review.applied_detailed_proposal_count,
                detailed_contested_proposal_count=review.detailed_contested_proposal_count,
                current_detailed_draft_path=review.current_detailed_draft_path,
                chair_certification_status=review.chair_certification_status,
                certified_resolution_path=review.certified_resolution_path,
                librarian_review_count=review.librarian_review_count,
                think_tank_material_finding_count=review.think_tank_material_finding_count,
                think_tank_reconvene_recommendation_count=(
                    review.think_tank_reconvene_recommendation_count
                ),
                chair_handoff_brief_path=review.chair_handoff_brief_path,
                think_tank_execution_review_path=review.think_tank_execution_review_path,
                think_tank_execution_review_count=review.think_tank_execution_review_count,
                human_review_packet_path=review.human_review_packet_path,
                chair_readability_status=review.chair_readability_status,
                chair_readability_certification_path=(
                    review.chair_readability_certification_path
                ),
                final_report_markdown_path=review.final_report_markdown_path,
                final_report_pdf_path=review.final_report_pdf_path,
                final_publication_manifest_path=review.final_publication_manifest_path,
                minority_report_count=review.minority_report_count,
                audit_requester_count=review.audit_requester_count,
                audit_petition_count=review.audit_petition_count,
                audit_petition_bundle_path=review.audit_petition_bundle_path,
                chair_accountability_report_path=review.chair_accountability_report_path,
                chair_accountability_cli_brief_path=(
                    review.chair_accountability_cli_brief_path
                ),
            )
        self._freeze_next_boundary(primary_drafter_id=primary_drafter_id, draft_path=draft_path)
        return DetailedDraftingResult(
            draft_path=str(draft_relative),
            primary_drafter_id=primary_drafter_id,
            next_phase=MeetingPhase.PAUSED,
            paused_reason=self.BOUNDARY_REASON,
        )

    def _freeze_next_boundary(self, *, primary_drafter_id: str, draft_path: Path) -> None:
        relative = Path("governance_private/detailed_clauses/clause_docket_boundary.json")
        absolute = self.repo.root / relative
        if not absolute.exists():
            self.repo.docs.write_once(
                relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "reason_code": self.BOUNDARY_REASON,
                        "completed_stage": "C1_PRIMARY_DRAFTER_INITIAL_DRAFT",
                        "next_stage": "C2_ACTIVE_REPRESENTATIVE_CLAUSE_REVIEW",
                        "primary_drafter_id": primary_drafter_id,
                        "draft_path": str(draft_path.relative_to(self.repo.root)),
                        "unconfigured_items": [
                            "how C0 is deterministically divided into the initial clause docket",
                            "how ACTIVE additions/replacements/new options are grouped by clause",
                            "Clause Split formal support threshold",
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            event = self.repo.events.append(
                "MEETING_PAUSED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "reason_code": self.BOUNDARY_REASON,
                    "completed_stage": "C1_PRIMARY_DRAFTER_INITIAL_DRAFT",
                    "record_path": str(relative),
                },
                actor="orchestrator",
            )
            self.engine.escalation.request(
                reason_code=self.BOUNDARY_REASON,
                summary=(
                    "C0 is frozen. Human policy is required for deterministic clause-docket construction "
                    "and the Clause Split support threshold before ACTIVE review."
                ),
                related_event_hash=event["event_hash"],
            )
        self.engine.status.phase = MeetingPhase.PAUSED
        self.engine.status.paused_reason = self.BOUNDARY_REASON
        self.engine.progress.status(
            MeetingPhase.PAUSED,
            f"{self.BOUNDARY_REASON} · C0 已冻结；进入 ACTIVE clause review 前需要制度决定",
        )

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
