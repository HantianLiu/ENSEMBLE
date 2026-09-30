from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.orchestration.clause_application import ClauseApplicationRunner
from project_ensemble.orchestration.clause_ballots import ClauseInitialBallotRunner
from project_ensemble.orchestration.clause_explanations import ClauseExplanationRunner
from project_ensemble.orchestration.clause_runoffs import ClauseTypeIIIRunoffRunner
from project_ensemble.orchestration.clause_type_i_followup import ClauseTypeIFollowupRunner
from project_ensemble.orchestration.final_reviews import FinalReviewRunner
from project_ensemble.orchestration.clause_options import ClauseOptionSetRunner
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.research.models import ResearchStage
from project_ensemble.research.rounds import ResearchRoundRunner
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.storage.meeting import MeetingRepository


TRIAL_POLICY_ID = "DETAILED_CLAUSE_DOCKET_TRIAL_2026-09-18"
_SECOND_LEVEL = re.compile(
    r"^(?P<clause_id>[0-9]+\.[0-9]+(?:-[A-Z]+)?)\s+(?P<title>.+)$"
)
_ARTICLE_HEADING = re.compile(r"^第\S+条(?:\s|$)")


class ClauseProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_clause_id: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    proposal_type: Literal["SUPPLEMENT", "REPLACE", "NEW_OPTION"]
    text: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SplitPart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ClauseSplitMotion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_clause_id: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    reason: str = Field(min_length=1)
    proposed_parts: list[SplitPart] = Field(min_length=2)


class SuspensionMotion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_clause_id: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    category: Literal[
        "EVIDENCE_INSUFFICIENT",
        "PROCEDURAL_CONTRADICTION",
        "UNSAFE_CONSEQUENCE",
        "UNRESOLVABLE_AMBIGUITY",
    ]
    reason: str = Field(min_length=1)


class ClauseReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposals: list[ClauseProposal] = Field(default_factory=list)
    split_motions: list[ClauseSplitMotion] = Field(default_factory=list)
    suspension_motions: list[SuspensionMotion] = Field(default_factory=list)


class MotionSupportAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported_motion_ids: list[str] = Field(default_factory=list)


class ClauseReviewResult(BaseModel):
    clause_count: int
    review_submission_count: int
    proposal_count: int
    split_motion_count: int
    suspension_motion_count: int
    adopted_split_motion_count: int
    adopted_suspension_motion_count: int
    option_set_count: int = 0
    type_i_option_set_count: int = 0
    type_ii_option_set_count: int = 0
    type_iii_option_set_count: int = 0
    initial_clause_ballot_count: int = 0
    completed_option_set_count: int = 0
    type_i_direct_adoption_count: int = 0
    type_i_followup_count: int = 0
    type_ii_direct_adoption_count: int = 0
    type_ii_followup_count: int = 0
    type_iii_runoff_count: int = 0
    completed_type_iii_runoff_count: int = 0
    type_iii_runoff_direct_adoption_count: int = 0
    type_iii_runoff_explanation_count: int = 0
    type_ii_explanation_submission_count: int = 0
    type_ii_second_ballot_count: int = 0
    type_ii_second_ballot_adoption_count: int = 0
    type_ii_contested_outcome_count: int = 0
    type_ii_new_option_count: int = 0
    applied_detailed_proposal_count: int = 0
    detailed_contested_proposal_count: int = 0
    current_detailed_draft_path: str | None = None
    chair_certification_status: str | None = None
    certified_resolution_path: str | None = None
    librarian_review_count: int = 0
    think_tank_material_finding_count: int = 0
    think_tank_reconvene_recommendation_count: int = 0
    chair_handoff_brief_path: str | None = None
    think_tank_execution_review_path: str | None = None
    think_tank_execution_review_count: int = 0
    human_review_packet_path: str | None = None
    chair_readability_status: str | None = None
    chair_readability_certification_path: str | None = None
    final_report_markdown_path: str | None = None
    final_report_pdf_path: str | None = None
    final_publication_manifest_path: str | None = None
    minority_report_count: int = 0
    audit_requester_count: int = 0
    audit_petition_count: int = 0
    audit_petition_bundle_path: str | None = None
    chair_accountability_report_path: str | None = None
    chair_accountability_cli_brief_path: str | None = None
    next_phase: MeetingPhase
    paused_reason: str | None = None


class ClauseReviewRunner:
    """Freeze the one-pass ACTIVE review and any trial-threshold motion support."""

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
        research_round_runner: ResearchRoundRunner | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.max_output_tokens = max_output_tokens
        self.research_round_runner = research_round_runner
        self.resolver = GovernanceDocumentResolver(governance_docs)
        self.assembler = RepresentativeContextAssembler()

    def run(self, *, draft_path: Path, general_principle_path: Path) -> ClauseReviewResult:
        self._ensure_trial_activation()
        docket_path, clauses = self._ensure_docket(draft_path)
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        transition = json.loads(
            self.repo.docs.read_text("identity_private/general_principle/status_transition.json")
        )
        active_records = [
            record
            for record in registry
            if transition["representative_statuses"][record["representative_id"]] == "ACTIVE"
        ]
        if self.research_round_runner is not None:
            self.research_round_runner.run(
                round_id="detailed-clause-review-001-pre",
                stage=ResearchStage.PROPOSAL,
                subject_files=(
                    self.repo.root / "public/task.json",
                    general_principle_path,
                    draft_path,
                    docket_path,
                ),
                representative_records=registry,
                max_claims_per_representative=4,
            )
        self.engine.status.phase = MeetingPhase.CLAUSE_REVIEW
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.CLAUSE_REVIEW,
            f"收集 {len(active_records)} 名 ACTIVE Representative 对 {len(clauses)} 个二级条款的一次性隔离审阅",
        )
        public_review_path, frozen = self._collect_reviews(
            active_records=active_records,
            clauses=clauses,
            docket_path=docket_path,
            draft_path=draft_path,
            general_principle_path=general_principle_path,
        )
        motions = [*frozen["split_motions"], *frozen["suspension_motions"]]
        if self.research_round_runner is not None and (
            frozen["proposals"] or motions
        ):
            self.research_round_runner.run(
                round_id="detailed-clause-review-001-post",
                stage=ResearchStage.CHALLENGE,
                subject_files=(draft_path, docket_path, public_review_path),
                representative_records=registry,
                max_claims_per_representative=2,
            )
        support = self._collect_motion_support(
            active_records=active_records,
            motions=motions,
            public_review_path=public_review_path,
            draft_path=draft_path,
        )
        adopted_split = [
            item
            for item in frozen["split_motions"]
            if support.get(item["motion_id"], 0) >= math.ceil(len(active_records) / 4)
        ]
        adopted_suspension = [
            item
            for item in frozen["suspension_motions"]
            if support.get(item["motion_id"], 0) >= math.ceil(len(active_records) / 3)
        ]
        outcome_relative = Path("public/detailed_clauses/motion_outcomes.json")
        if not (self.repo.root / outcome_relative).exists():
            self.repo.docs.write_once(
                outcome_relative,
                json.dumps(
                    {
                        "active_count": len(active_records),
                        "split_threshold": math.ceil(len(active_records) / 4),
                        "suspension_threshold": math.ceil(len(active_records) / 3),
                        "support_tally": support,
                        "adopted_split_motion_ids": [item["motion_id"] for item in adopted_split],
                        "adopted_suspension_motion_ids": [
                            item["motion_id"] for item in adopted_suspension
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "DETAILED_CLAUSE_MOTION_SUPPORT_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "split_threshold": math.ceil(len(active_records) / 4),
                    "suspension_threshold": math.ceil(len(active_records) / 3),
                    "adopted_split_count": len(adopted_split),
                    "adopted_suspension_count": len(adopted_suspension),
                    "record_path": str(outcome_relative),
                },
                actor="orchestrator",
            )
        if adopted_suspension:
            reason = "CLAUSE_REVIEW_SUSPENDED_BY_ACTIVE_MOTION"
            self._pause(reason, "至少一项 Motion to Suspend 达到试行支持门槛")
            next_phase = MeetingPhase.PAUSED
            option_set_count = type_i_count = type_ii_count = type_iii_count = 0
            ballot_count = completed_set_count = 0
            type_i_adopted = type_i_followup = type_ii_adopted = type_ii_followup = 0
            type_iii_runoff = 0
            completed_runoff = runoff_adopted = runoff_explanation = 0
            explanation_submissions = second_ballots = second_adopted = contested = new_options = 0
            applied_proposals = applied_contested = 0
            current_detailed_draft = None
            chair_certification = certified_resolution = None
            librarian_reviews = material_findings = reconvene_recommendations = 0
            chair_handoff = execution_review_path = human_review_packet = None
            execution_review_count = 0
            readability_status = readability_certification = None
            final_report_markdown = final_report_pdf = final_publication_manifest = None
            minority_report_count = audit_requester_count = audit_petition_count = 0
            audit_petition_bundle = chair_accountability_report = None
            chair_accountability_cli_brief = None
        else:
            if adopted_split:
                draft_path, public_review_path, clauses = self._ensure_split_application(
                    draft_path=draft_path,
                    docket_path=docket_path,
                    public_review_path=public_review_path,
                    frozen_reviews=frozen,
                    adopted_splits=adopted_split,
                )
            option_result = ClauseOptionSetRunner(
                repo=self.repo,
                engine=self.engine,
                governance_docs=self.resolver.root,
                max_output_tokens=self.max_output_tokens,
                continue_into_ballots=True,
            ).run(
                draft_path=draft_path,
                active_reviews_path=public_review_path,
            )
            ballot_result = ClauseInitialBallotRunner(
                repo=self.repo,
                engine=self.engine,
                governance_docs=self.resolver.root,
                max_output_tokens=self.max_output_tokens,
                continue_into_runoffs=True,
            ).run(
                draft_path=draft_path,
                option_sets_path=self.repo.root / option_result.record_path,
            )
            if ballot_result.type_i_followup_count:
                ClauseTypeIFollowupRunner(
                    repo=self.repo,
                    engine=self.engine,
                    governance_docs=self.resolver.root,
                    max_output_tokens=self.max_output_tokens,
                ).run(
                    draft_path=draft_path,
                    option_sets_path=self.repo.root / option_result.record_path,
                    initial_outcomes_path=(
                        self.repo.root / "public/detailed_clauses/initial_ballot_outcomes.json"
                    ),
                )
            runoff_result = ClauseTypeIIIRunoffRunner(
                repo=self.repo,
                engine=self.engine,
                governance_docs=self.resolver.root,
                max_output_tokens=self.max_output_tokens,
            ).run(
                draft_path=draft_path,
                option_sets_path=self.repo.root / option_result.record_path,
                initial_outcomes_path=(
                    self.repo.root / "public/detailed_clauses/initial_ballot_outcomes.json"
                ),
            )
            application_option_sets_path = self.repo.root / option_result.record_path
            application_second_outcomes_path = (
                self.repo.root / "public/detailed_clauses/type_ii_second_ballot_outcomes.json"
            )
            if ballot_result.type_ii_followup_count or runoff_result.explanation_round_count:
                explanation_result = ClauseExplanationRunner(
                    repo=self.repo,
                    engine=self.engine,
                    governance_docs=self.resolver.root,
                    max_output_tokens=self.max_output_tokens,
                ).run(
                    draft_path=draft_path,
                    option_sets_path=self.repo.root / option_result.record_path,
                    initial_outcomes_path=(
                        self.repo.root / "public/detailed_clauses/initial_ballot_outcomes.json"
                    ),
                    runoff_outcomes_path=(
                        self.repo.root / "public/detailed_clauses/type_iii_runoff_outcomes.json"
                    ),
                )
                reason = explanation_result.paused_reason
                next_phase = explanation_result.next_phase
                explanation_submissions = explanation_result.explanation_submission_count
                second_ballots = explanation_result.second_ballot_count
                second_adopted = explanation_result.adopted_count
                contested = explanation_result.contested_count
                new_options = explanation_result.new_option_count
                application_option_sets_path = (
                    self.repo.root / explanation_result.effective_option_sets_path
                )
                application_second_outcomes_path = (
                    self.repo.root / explanation_result.second_outcomes_path
                )
            else:
                reason = runoff_result.paused_reason
                next_phase = runoff_result.next_phase
                explanation_submissions = second_ballots = second_adopted = contested = new_options = 0
            if reason == "ADOPTED_CLAUSE_APPLICATION_NOT_IMPLEMENTED":
                second_outcomes_path = application_second_outcomes_path
                type_i_revisions_path = (
                    self.repo.root / "public/detailed_clauses/type_i_revisions.json"
                )
                type_i_second_outcomes_path = (
                    self.repo.root
                    / "public/detailed_clauses/type_i_second_ballot_outcomes.json"
                )
                application_result = ClauseApplicationRunner(
                    repo=self.repo,
                    engine=self.engine,
                    governance_docs=self.resolver.root,
                    max_output_tokens=self.max_output_tokens,
                    continue_into_reviews=True,
                ).run(
                    draft_path=draft_path,
                    option_sets_path=application_option_sets_path,
                    initial_outcomes_path=(
                        self.repo.root / "public/detailed_clauses/initial_ballot_outcomes.json"
                    ),
                    runoff_outcomes_path=(
                        self.repo.root / "public/detailed_clauses/type_iii_runoff_outcomes.json"
                    ),
                    second_outcomes_path=(
                        second_outcomes_path if second_outcomes_path.exists() else None
                    ),
                    type_i_revisions_path=(
                        type_i_revisions_path if type_i_revisions_path.exists() else None
                    ),
                    type_i_second_outcomes_path=(
                        type_i_second_outcomes_path
                        if type_i_second_outcomes_path.exists()
                        else None
                    ),
                )
                applied_proposals = application_result.applied_proposal_count
                applied_contested = application_result.contested_proposal_count
                current_detailed_draft = application_result.draft_path
                final_review = FinalReviewRunner(
                    repo=self.repo,
                    engine=self.engine,
                    governance_docs=self.resolver.root,
                    max_output_tokens=self.max_output_tokens,
                ).run(
                    provisional_draft_path=self.repo.root / application_result.draft_path,
                    provenance_path=self.repo.root / application_result.provenance_path,
                )
                reason = final_review.paused_reason
                next_phase = final_review.next_phase
                chair_certification = final_review.chair_certification_status
                certified_resolution = final_review.certified_resolution_path
                librarian_reviews = final_review.librarian_review_count
                material_findings = final_review.material_finding_count
                reconvene_recommendations = final_review.reconvene_recommendation_count
                chair_handoff = final_review.chair_handoff_brief_path
                execution_review_path = final_review.think_tank_execution_review_path
                execution_review_count = final_review.think_tank_execution_review_count
                human_review_packet = final_review.human_review_packet_path
                readability_status = final_review.chair_readability_status
                readability_certification = (
                    final_review.chair_readability_certification_path
                )
                final_report_markdown = final_review.final_report_markdown_path
                final_report_pdf = final_review.final_report_pdf_path
                final_publication_manifest = (
                    final_review.final_publication_manifest_path
                )
                minority_report_count = final_review.minority_report_count
                audit_requester_count = final_review.audit_requester_count
                audit_petition_count = final_review.audit_petition_count
                audit_petition_bundle = final_review.audit_petition_bundle_path
                chair_accountability_report = (
                    final_review.chair_accountability_report_path
                )
                chair_accountability_cli_brief = (
                    final_review.chair_accountability_cli_brief_path
                )
            else:
                applied_proposals = applied_contested = 0
                current_detailed_draft = None
                chair_certification = certified_resolution = None
                librarian_reviews = material_findings = reconvene_recommendations = 0
                chair_handoff = execution_review_path = human_review_packet = None
                execution_review_count = 0
                readability_status = readability_certification = None
                final_report_markdown = final_report_pdf = final_publication_manifest = None
                minority_report_count = audit_requester_count = audit_petition_count = 0
                audit_petition_bundle = chair_accountability_report = None
                chair_accountability_cli_brief = None
            option_set_count = option_result.option_set_count
            type_i_count = option_result.type_i_count
            type_ii_count = option_result.type_ii_count
            type_iii_count = option_result.type_iii_count
            ballot_count = ballot_result.ballot_count
            completed_set_count = ballot_result.completed_option_set_count
            type_i_adopted = ballot_result.type_i_direct_adoption_count
            type_i_followup = ballot_result.type_i_followup_count
            type_ii_adopted = ballot_result.type_ii_direct_adoption_count
            type_ii_followup = ballot_result.type_ii_followup_count
            type_iii_runoff = ballot_result.type_iii_runoff_count
            completed_runoff = runoff_result.runoff_ballot_count
            runoff_adopted = runoff_result.direct_adoption_count
            runoff_explanation = runoff_result.explanation_round_count
        return ClauseReviewResult(
            clause_count=len(clauses),
            review_submission_count=len(active_records),
            proposal_count=len(frozen["proposals"]),
            split_motion_count=len(frozen["split_motions"]),
            suspension_motion_count=len(frozen["suspension_motions"]),
            adopted_split_motion_count=len(adopted_split),
            adopted_suspension_motion_count=len(adopted_suspension),
            option_set_count=option_set_count,
            type_i_option_set_count=type_i_count,
            type_ii_option_set_count=type_ii_count,
            type_iii_option_set_count=type_iii_count,
            initial_clause_ballot_count=ballot_count,
            completed_option_set_count=completed_set_count,
            type_i_direct_adoption_count=type_i_adopted,
            type_i_followup_count=type_i_followup,
            type_ii_direct_adoption_count=type_ii_adopted,
            type_ii_followup_count=type_ii_followup,
            type_iii_runoff_count=type_iii_runoff,
            completed_type_iii_runoff_count=completed_runoff,
            type_iii_runoff_direct_adoption_count=runoff_adopted,
            type_iii_runoff_explanation_count=runoff_explanation,
            type_ii_explanation_submission_count=explanation_submissions,
            type_ii_second_ballot_count=second_ballots,
            type_ii_second_ballot_adoption_count=second_adopted,
            type_ii_contested_outcome_count=contested,
            type_ii_new_option_count=new_options,
            applied_detailed_proposal_count=applied_proposals,
            detailed_contested_proposal_count=applied_contested,
            current_detailed_draft_path=current_detailed_draft,
            chair_certification_status=chair_certification,
            certified_resolution_path=certified_resolution,
            librarian_review_count=librarian_reviews,
            think_tank_material_finding_count=material_findings,
            think_tank_reconvene_recommendation_count=reconvene_recommendations,
            chair_handoff_brief_path=chair_handoff,
            think_tank_execution_review_path=execution_review_path,
            think_tank_execution_review_count=execution_review_count,
            human_review_packet_path=human_review_packet,
            chair_readability_status=readability_status,
            chair_readability_certification_path=readability_certification,
            final_report_markdown_path=final_report_markdown,
            final_report_pdf_path=final_report_pdf,
            final_publication_manifest_path=final_publication_manifest,
            minority_report_count=minority_report_count,
            audit_requester_count=audit_requester_count,
            audit_petition_count=audit_petition_count,
            audit_petition_bundle_path=audit_petition_bundle,
            chair_accountability_report_path=chair_accountability_report,
            chair_accountability_cli_brief_path=chair_accountability_cli_brief,
            next_phase=next_phase,
            paused_reason=reason,
        )

    def _ensure_trial_activation(self) -> None:
        relative = Path("governance_private/detailed_clauses/trial_docket_policy_activation.json")
        if (self.repo.root / relative).exists():
            return
        record = {
            "meeting_id": self.repo.meeting_id,
            "policy_id": TRIAL_POLICY_ID,
            "status": "TRIAL",
            "review_status": "PENDING_THINK_TANK_REVIEW",
            "supersedes_pause_reason": "DETAILED_CLAUSE_DOCKET_POLICY_NOT_CONFIGURED",
            "docket_level": "SECOND_LEVEL_NUMERIC_CLAUSES",
            "split_threshold": "ceil(N_ACTIVE/4)",
            "suspension_threshold": "ceil(N_ACTIVE/3)",
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "TRIAL_DETAILED_CLAUSE_POLICY_ACTIVATED",
            {**record, "record_path": str(relative)},
            actor="Human",
        )
        self.repo.events.append(
            "MEETING_RESUMED",
            {
                "meeting_id": self.repo.meeting_id,
                "prior_reason_code": "DETAILED_CLAUSE_DOCKET_POLICY_NOT_CONFIGURED",
                "resume_stage": MeetingPhase.CLAUSE_REVIEW.value,
                "policy_id": TRIAL_POLICY_ID,
            },
            actor="orchestrator",
        )

    def _ensure_docket(self, draft_path: Path) -> tuple[Path, list[dict]]:
        relative = Path("public/detailed_clauses/clause_docket.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            frozen = json.loads(absolute.read_text(encoding="utf-8"))
            return absolute, frozen["clauses"]
        clauses = self._parse_second_level_clauses(draft_path.read_text(encoding="utf-8"))
        if not clauses:
            raise ValueError("C0 contains no second-level numeric clauses for the trial docket")
        record = {
            "meeting_id": self.repo.meeting_id,
            "policy_id": TRIAL_POLICY_ID,
            "status": "FROZEN",
            "source_draft_path": str(draft_path.relative_to(self.repo.root)),
            "source_draft_sha256": self._sha256(draft_path.read_text(encoding="utf-8")),
            "clause_count": len(clauses),
            "clauses": clauses,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_DOCKET_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "policy_id": TRIAL_POLICY_ID,
                "clause_count": len(clauses),
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        return absolute, clauses

    def _collect_reviews(
        self,
        *,
        active_records: list[dict],
        clauses: list[dict],
        docket_path: Path,
        draft_path: Path,
        general_principle_path: Path,
    ) -> tuple[Path, dict]:
        public_relative = Path("public/detailed_clauses/active_reviews.json")
        public_path = self.repo.root / public_relative
        if public_path.exists():
            return public_path, json.loads(public_path.read_text(encoding="utf-8"))
        legal_ids = {item["clause_id"] for item in clauses}
        reviews = []
        for index, record in enumerate(active_records, start=1):
            representative_id = record["representative_id"]
            relative = Path("governance_private/detailed_clauses/reviews") / f"{representative_id}.json"
            absolute = self.repo.root / relative
            if absolute.exists():
                review = ClauseReviewAction.model_validate_json(absolute.read_text(encoding="utf-8"))
                self.engine.progress.info(f"已恢复 {index}/{len(active_records)} 份 ACTIVE 细则审阅")
            else:
                spec = self.resolver.representative_context_spec(
                    persona=Persona(record["runtime"]["persona"]),
                    stage="active_detailed_drafting",
                    representative_id=representative_id,
                    public_state_files=(
                        self.repo.root / "public/task.json",
                        general_principle_path,
                        draft_path,
                        docket_path,
                    ),
                    own_state_files=(
                        self.repo.root
                        / "representatives"
                        / representative_id
                        / "status_transition_001.json",
                    ),
                )
                response = self.engine.invoke_participant(
                    representative_id,
                    system_text=self.assembler.assemble(spec),
                    user_text=(
                        "Review the complete C0 once. Return exactly one JSON object with keys proposals, "
                        "split_motions, suspension_motions; each value is a list and may be empty. A proposal "
                        "has target_clause_id, proposal_type (SUPPLEMENT, REPLACE, or NEW_OPTION), text, reason. "
                        "A split motion has target_clause_id, reason, proposed_parts (at least two objects with "
                        "label and text). A suspension motion has target_clause_id, category "
                        "(EVIDENCE_INSUFFICIENT, PROCEDURAL_CONTRADICTION, UNSAFE_CONSEQUENCE, or "
                        "UNRESOLVABLE_AMBIGUITY), and reason. Use only second-level target IDs from the frozen "
                        "docket. Submit only substantive concerns; empty lists mean no changes. Do not add keys "
                        "or use Markdown."
                    ),
                    stage="active_detailed_drafting",
                    max_output_tokens=self.max_output_tokens,
                )
                try:
                    review = ClauseReviewAction.model_validate(json.loads(response.text))
                    targets = {
                        item.target_clause_id
                        for item in [
                            *review.proposals,
                            *review.split_motions,
                            *review.suspension_motions,
                        ]
                    }
                    if not targets <= legal_ids:
                        raise ValueError("review references a clause outside the frozen docket")
                except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                    self.engine.pause_for_unconfigured_policy(
                        participant_id=representative_id,
                        reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                    )
                self.repo.docs.write_once(relative, review.model_dump_json(indent=2))
                self.repo.events.append(
                    "DETAILED_CLAUSE_REVIEW_SUBMITTED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "representative_id": representative_id,
                        "record_path": str(relative),
                    },
                    actor=representative_id,
                )
            reviews.append((representative_id, review))

        proposals = []
        split_motions = []
        suspension_motions = []
        for representative_id, review in reviews:
            for ordinal, item in enumerate(review.proposals, start=1):
                proposals.append(
                    {"proposal_id": f"CP-{representative_id}-{ordinal:03d}", "proposer_id": representative_id, **item.model_dump(mode="json")}
                )
            for ordinal, item in enumerate(review.split_motions, start=1):
                split_motions.append(
                    {"motion_id": f"CS-{representative_id}-{ordinal:03d}", "proposer_id": representative_id, **item.model_dump(mode="json")}
                )
            for ordinal, item in enumerate(review.suspension_motions, start=1):
                suspension_motions.append(
                    {"motion_id": f"MS-{representative_id}-{ordinal:03d}", "proposer_id": representative_id, **item.model_dump(mode="json")}
                )
        frozen = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "submission_count": len(reviews),
            "proposals": proposals,
            "proposal_groups": {
                clause_id: [item for item in proposals if item["target_clause_id"] == clause_id]
                for clause_id in sorted({item["target_clause_id"] for item in proposals})
            },
            "split_motions": split_motions,
            "suspension_motions": suspension_motions,
        }
        self.repo.docs.write_once(public_relative, json.dumps(frozen, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_REVIEW_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "submission_count": len(reviews),
                "proposal_count": len(proposals),
                "split_motion_count": len(split_motions),
                "suspension_motion_count": len(suspension_motions),
                "record_path": str(public_relative),
            },
            actor="orchestrator",
        )
        for representative_id, review in reviews:
            self.engine.progress.speech(
                representative_id,
                "ACTIVE 细则审阅（窗口已冻结）",
                review.model_dump_json(indent=2),
            )
        return public_path, frozen

    def _collect_motion_support(
        self,
        *,
        active_records: list[dict],
        motions: list[dict],
        public_review_path: Path,
        draft_path: Path,
    ) -> dict[str, int]:
        if not motions:
            return {}
        relative = Path("governance_private/detailed_clauses/motion_support/frozen.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return json.loads(absolute.read_text(encoding="utf-8"))["tally"]
        legal_ids = {item["motion_id"] for item in motions}
        tally = {motion_id: 0 for motion_id in sorted(legal_ids)}
        actions_by_id: dict[str, MotionSupportAction] = {}
        missing_records = []
        for record in active_records:
            representative_id = record["representative_id"]
            submission_relative = (
                Path("governance_private/detailed_clauses/motion_support/submissions")
                / f"{representative_id}.json"
            )
            submission_path = self.repo.root / submission_relative
            if submission_path.exists():
                action = MotionSupportAction.model_validate_json(
                    submission_path.read_text(encoding="utf-8")
                )
                actions_by_id[representative_id] = action
            else:
                missing_records.append(record)

        def collect_support(record: dict) -> tuple[str, MotionSupportAction, Path]:
            representative_id = record["representative_id"]
            submission_relative = (
                Path("governance_private/detailed_clauses/motion_support/submissions")
                / f"{representative_id}.json"
            )
            spec = self.resolver.representative_context_spec(
                persona=Persona(record["runtime"]["persona"]),
                stage="active_detailed_drafting",
                representative_id=representative_id,
                public_state_files=(draft_path, public_review_path),
                own_state_files=(),
            )
            response = self.engine.invoke_participant(
                representative_id,
                system_text=self.assembler.assemble(spec),
                user_text=(
                    "Independently choose which listed split or suspension motions you substantively support. "
                    "Return exactly {\"supported_motion_ids\":[\"CS-...\",\"MS-...\"]}; use an empty "
                    "list for none. Choose unique IDs only from "
                    + json.dumps(sorted(legal_ids), ensure_ascii=False)
                    + ". Do not add keys or use Markdown."
                ),
                stage="clause_motion_support",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = MotionSupportAction.model_validate(json.loads(response.text))
                if len(action.supported_motion_ids) != len(set(action.supported_motion_ids)):
                    raise ValueError("motion support IDs must be unique")
                if not set(action.supported_motion_ids) <= legal_ids:
                    raise ValueError("motion support references an unknown motion")
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            return representative_id, action, submission_relative

        def persist_support(result: tuple[str, MotionSupportAction, Path]) -> None:
            representative_id, action, submission_relative = result
            self.repo.docs.write_once(submission_relative, action.model_dump_json(indent=2))
            self.repo.events.append(
                "DETAILED_CLAUSE_MOTION_SUPPORT_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "representative_id": representative_id,
                    "record_path": str(submission_relative),
                },
                actor=representative_id,
            )

        completed = run_bounded_representative_lanes(
            missing_records,
            collect_support,
            getattr(self.engine, "model_concurrency_limit", lambda *_args: 1),
            on_result=persist_support,
            progress=self.engine.progress,
            progress_records=active_records,
            completed_participant_ids=actions_by_id,
        )
        actions_by_id.update(
            {representative_id: action for representative_id, action, _ in completed}
        )
        submissions = []
        for record in active_records:
            representative_id = record["representative_id"]
            action = actions_by_id[representative_id]
            for motion_id in action.supported_motion_ids:
                tally[motion_id] += 1
            submissions.append(representative_id)
        self.repo.docs.write_once(
            relative,
            json.dumps(
                {"status": "FROZEN", "submission_count": len(submissions), "tally": tally},
                indent=2,
            ),
        )
        return tally

    def _ensure_split_application(
        self,
        *,
        draft_path: Path,
        docket_path: Path,
        public_review_path: Path,
        frozen_reviews: dict,
        adopted_splits: list[dict],
    ) -> tuple[Path, Path, list[dict]]:
        """Apply adopted split motions once and return the rebased review inputs."""

        application_relative = Path(
            "governance_private/detailed_clauses/split_application_001.json"
        )
        application_path = self.repo.root / application_relative
        if application_path.exists():
            application = json.loads(application_path.read_text(encoding="utf-8"))
            rebased_draft = self.repo.root / application["rebased_draft_path"]
            rebased_reviews = self.repo.root / application["rebased_reviews_path"]
            rebased_docket = self.repo.root / application["rebased_docket_path"]
            clauses = json.loads(rebased_docket.read_text(encoding="utf-8"))["clauses"]
            return rebased_draft, rebased_reviews, clauses

        motions_by_target: dict[str, list[dict]] = {}
        for motion in adopted_splits:
            motions_by_target.setdefault(motion["target_clause_id"], []).append(motion)
        conflicting_targets = sorted(
            target for target, motions in motions_by_target.items() if len(motions) > 1
        )
        if conflicting_targets:
            self.engine.pause_for_unconfigured_policy(
                participant_id="CHAIR",
                reason_code="CONFLICTING_ADOPTED_CLAUSE_SPLITS_REQUIRE_RULING",
            )

        affected_targets = set(motions_by_target)
        affected_proposals = [
            item
            for item in frozen_reviews["proposals"]
            if item["target_clause_id"] in affected_targets
        ]
        if affected_proposals:
            # A proposal aimed at the former aggregate clause may affect one or several
            # new parts.  Do not silently guess or duplicate its normative effect.
            self.engine.pause_for_unconfigured_policy(
                participant_id="CHAIR",
                reason_code="CLAUSE_SPLIT_PROPOSAL_RETARGETING_REQUIRED",
            )

        original_docket = json.loads(docket_path.read_text(encoding="utf-8"))
        source_clauses = original_docket["clauses"]
        source_by_id = {item["clause_id"]: item for item in source_clauses}
        missing_targets = sorted(affected_targets - set(source_by_id))
        if missing_targets:
            raise ValueError(
                f"adopted split targets are absent from the docket: {missing_targets}"
            )

        source_text = draft_path.read_text(encoding="utf-8")
        rebased_text = source_text
        rebased_clauses: list[dict] = []
        split_records: list[dict] = []
        for clause in source_clauses:
            target = clause["clause_id"]
            motions = motions_by_target.get(target)
            if not motions:
                rebased_clauses.append(clause)
                continue
            motion = motions[0]
            parts = []
            for ordinal, part in enumerate(motion["proposed_parts"], start=1):
                suffix = self._alphabetic_suffix(ordinal)
                clause_id = f"{target}-{suffix}"
                label = self._strip_split_id(part["label"], clause_id)
                rendered = f"{clause_id} {label}\n{part['text']}".strip()
                parts.append(
                    {
                        "clause_id": clause_id,
                        "title": label,
                        "text": rendered,
                        "split_from_clause_id": target,
                        "split_motion_id": motion["motion_id"],
                    }
                )
            replacement = "\n\n".join(item["text"] for item in parts)
            rebased_text = self._replace_docket_clause(rebased_text, target, replacement)
            rebased_clauses.extend(parts)
            split_records.append(
                {
                    "motion_id": motion["motion_id"],
                    "target_clause_id": target,
                    "result_clause_ids": [item["clause_id"] for item in parts],
                    "support_record_path": "public/detailed_clauses/motion_outcomes.json",
                }
            )

        rebased_text = rebased_text.rstrip() + "\n"
        split_metadata = {
            item["clause_id"]: {
                "split_from_clause_id": item["split_from_clause_id"],
                "split_motion_id": item["split_motion_id"],
            }
            for item in rebased_clauses
            if "split_motion_id" in item
        }
        rebased_clauses = self._parse_second_level_clauses(rebased_text)
        for clause in rebased_clauses:
            clause.update(split_metadata.get(clause["clause_id"], {}))
        draft_relative = Path("public/detailed_clauses/C0R1.md")
        provenance_relative = Path("public/detailed_clauses/C0R1.provenance.json")
        docket_relative = Path("public/detailed_clauses/clause_docket_rebased_001.json")
        reviews_relative = Path("public/detailed_clauses/active_reviews_rebased_001.json")
        rebased_reviews_record = {
            **frozen_reviews,
            "status": "REBASED_FROZEN",
            "source_reviews_path": str(public_review_path.relative_to(self.repo.root)),
            "source_docket_path": str(docket_path.relative_to(self.repo.root)),
            "applied_split_motion_ids": [item["motion_id"] for item in adopted_splits],
            "proposal_groups": {
                clause_id: [
                    item
                    for item in frozen_reviews["proposals"]
                    if item["target_clause_id"] == clause_id
                ]
                for clause_id in sorted(
                    {item["target_clause_id"] for item in frozen_reviews["proposals"]}
                )
            },
        }
        rebased_docket_record = {
            **original_docket,
            "status": "REBASED_FROZEN",
            "source_docket_path": str(docket_path.relative_to(self.repo.root)),
            "source_draft_path": str(draft_relative),
            "source_draft_sha256": self._sha256(rebased_text),
            "clause_count": len(rebased_clauses),
            "applied_split_motion_ids": [item["motion_id"] for item in adopted_splits],
            "clauses": rebased_clauses,
        }
        provenance = {
            "meeting_id": self.repo.meeting_id,
            "draft": "C0R1",
            "status": "SPLIT_REBASED_STANDING_TEXT",
            "base_draft_path": str(draft_path.relative_to(self.repo.root)),
            "base_sha256": self._sha256(source_text),
            "result_sha256": self._sha256(rebased_text),
            "split_applications": split_records,
        }
        application = {
            "meeting_id": self.repo.meeting_id,
            "policy_id": TRIAL_POLICY_ID,
            "status": "FROZEN",
            "source_draft_path": str(draft_path.relative_to(self.repo.root)),
            "source_docket_path": str(docket_path.relative_to(self.repo.root)),
            "source_reviews_path": str(public_review_path.relative_to(self.repo.root)),
            "rebased_draft_path": str(draft_relative),
            "rebased_docket_path": str(docket_relative),
            "rebased_reviews_path": str(reviews_relative),
            "split_applications": split_records,
        }
        self.repo.docs.write_once(draft_relative, rebased_text)
        self.repo.docs.write_once(
            provenance_relative, json.dumps(provenance, indent=2, ensure_ascii=False)
        )
        self.repo.docs.write_once(
            docket_relative,
            json.dumps(rebased_docket_record, indent=2, ensure_ascii=False),
        )
        self.repo.docs.write_once(
            reviews_relative,
            json.dumps(rebased_reviews_record, indent=2, ensure_ascii=False),
        )
        self.repo.docs.write_once(
            application_relative, json.dumps(application, indent=2, ensure_ascii=False)
        )
        self.repo.events.append(
            "DETAILED_CLAUSE_SPLITS_APPLIED",
            {
                "meeting_id": self.repo.meeting_id,
                "applied_split_motion_ids": [item["motion_id"] for item in adopted_splits],
                "source_clause_count": len(source_clauses),
                "rebased_clause_count": len(rebased_clauses),
                "draft_path": str(draft_relative),
                "docket_path": str(docket_relative),
                "reviews_path": str(reviews_relative),
                "record_path": str(application_relative),
            },
            actor="orchestrator",
        )
        self.repo.events.append(
            "MEETING_RESUMED",
            {
                "meeting_id": self.repo.meeting_id,
                "prior_reason_code": "ADOPTED_CLAUSE_SPLIT_APPLICATION_REQUIRED",
                "resume_stage": MeetingPhase.CLAUSE_REVIEW.value,
                "record_path": str(application_relative),
            },
            actor="orchestrator",
        )
        self.engine.progress.status(
            MeetingPhase.CLAUSE_REVIEW,
            f"已应用 {len(adopted_splits)} 项 Clause Split；docket 由 "
            f"{len(source_clauses)} 项重基为 {len(rebased_clauses)} 项",
        )
        return self.repo.root / draft_relative, self.repo.root / reviews_relative, rebased_clauses

    @staticmethod
    def _alphabetic_suffix(ordinal: int) -> str:
        if ordinal < 1:
            raise ValueError("split part ordinal must be positive")
        result = ""
        value = ordinal
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(ord("A") + remainder) + result
        return result

    @staticmethod
    def _strip_split_id(label: str, clause_id: str) -> str:
        stripped = label.strip()
        if stripped.startswith(clause_id):
            stripped = stripped[len(clause_id) :].lstrip(" ：:-—")
        return stripped or clause_id

    @staticmethod
    def _replace_docket_clause(text: str, clause_id: str, replacement: str) -> str:
        pattern = re.compile(
            rf"^{re.escape(clause_id)}\s+.*?"
            rf"(?=^[0-9]+\.[0-9]+(?:-[A-Z]+)?\s+|^第\S+条(?:\s|$)|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError(
                f"source clause {clause_id} is not uniquely locatable in the detailed draft"
            )
        match = matches[0]
        prefix = text[: match.start()]
        suffix = text[match.end() :].lstrip("\n")
        return prefix + replacement.rstrip() + "\n\n" + suffix

    def _pause(self, reason: str, summary: str) -> None:
        relative = Path("governance_private/detailed_clauses") / f"pause_{reason.lower()}.json"
        if not (self.repo.root / relative).exists():
            self.repo.docs.write_once(
                relative,
                json.dumps(
                    {"meeting_id": self.repo.meeting_id, "reason_code": reason, "summary": summary},
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "MEETING_PAUSED",
                {"meeting_id": self.repo.meeting_id, "reason_code": reason, "record_path": str(relative)},
                actor="orchestrator",
            )
        self.engine.status.phase = MeetingPhase.PAUSED
        self.engine.status.paused_reason = reason
        self.engine.progress.status(MeetingPhase.PAUSED, f"{reason} · {summary}")

    @staticmethod
    def _parse_second_level_clauses(text: str) -> list[dict]:
        clauses: list[dict] = []
        current: dict | None = None
        for line in text.splitlines():
            match = _SECOND_LEVEL.match(line)
            if match:
                if current is not None:
                    current["text"] = "\n".join(current.pop("lines")).strip()
                    clauses.append(current)
                current = {
                    "clause_id": match.group("clause_id"),
                    "title": match.group("title"),
                    "lines": [line],
                }
            elif current is not None and _ARTICLE_HEADING.match(line):
                current["text"] = "\n".join(current.pop("lines")).strip()
                clauses.append(current)
                current = None
            elif current is not None:
                current["lines"].append(line)
        if current is not None:
            current["text"] = "\n".join(current.pop("lines")).strip()
            clauses.append(current)
        ids = [item["clause_id"] for item in clauses]
        if len(ids) != len(set(ids)):
            raise ValueError("C0 second-level clause IDs must be unique")
        return clauses

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
