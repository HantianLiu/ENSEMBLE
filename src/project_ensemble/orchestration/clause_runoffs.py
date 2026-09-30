from __future__ import annotations

import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.governance_private.thresholds import high_threshold, high_threshold_formula
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.storage.meeting import MeetingRepository


class ClauseRunoffVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_set_id: str = Field(min_length=1)
    choice: str = Field(min_length=1)


class ClauseRunoffAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    votes: list[ClauseRunoffVote]


class ClauseTieChairDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class ClauseRunoffResult(BaseModel):
    runoff_ballot_count: int
    direct_adoption_count: int
    explanation_round_count: int
    next_phase: MeetingPhase
    paused_reason: str


class ClauseTypeIIIRunoffRunner:
    """Reduce frozen Type III top-two pairs through their first Type II ballot."""

    EXPLANATION_BOUNDARY = "DETAILED_CLAUSE_EXPLANATION_ROUND_NOT_IMPLEMENTED"
    APPLICATION_BOUNDARY = "ADOPTED_CLAUSE_APPLICATION_NOT_IMPLEMENTED"
    TIE_BOUNDARY = "TYPE_III_TOP_TWO_TIE_REQUIRES_HUMAN"
    TIEBREAK_DEADLOCK_BOUNDARY = "TYPE_III_CUTOFF_TIEBREAK_DEADLOCK_REQUIRES_HUMAN"
    COMPLEX_TIE_BOUNDARY = "TYPE_III_COMPLEX_CUTOFF_TIE_REQUIRES_HUMAN"

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.max_output_tokens = max_output_tokens
        self.resolver = GovernanceDocumentResolver(governance_docs)
        self.assembler = RepresentativeContextAssembler()

    def run(
        self,
        *,
        draft_path: Path,
        option_sets_path: Path,
        initial_outcomes_path: Path,
    ) -> ClauseRunoffResult:
        self._ensure_resume_record()
        option_record = json.loads(option_sets_path.read_text(encoding="utf-8"))
        option_sets = {item["option_set_id"]: item for item in option_record["option_sets"]}
        initial = json.loads(initial_outcomes_path.read_text(encoding="utf-8"))
        tied = [
            item
            for item in initial["outcomes"]
            if item["status"] == "TYPE_III_TOP_TWO_TIE_REQUIRES_HUMAN"
        ]
        active_records = self._active_records()
        resolved_ties: dict[str, list[str]] = {}
        if tied:
            resolved_ties = self._resolve_cutoff_ties(
                tied=tied,
                option_sets=option_sets,
                active_records=active_records,
                draft_path=draft_path,
                option_sets_path=option_sets_path,
                initial_outcomes_path=initial_outcomes_path,
            )
            if not resolved_ties:
                return ClauseRunoffResult(
                    runoff_ballot_count=0,
                    direct_adoption_count=0,
                    explanation_round_count=0,
                    next_phase=MeetingPhase.PAUSED,
                    paused_reason=self.engine.status.paused_reason or self.TIE_BOUNDARY,
                )
        runoffs = [
            {
                "option_set_id": item["option_set_id"],
                "top_two_proposal_ids": (
                    item["top_two_proposal_ids"]
                    if item["status"] == "TYPE_III_TOP_TWO_READY"
                    else resolved_ties[item["option_set_id"]]
                ),
                "options": [
                    option
                    for option in option_sets[item["option_set_id"]]["options"]
                    if option["proposal_id"]
                    in (
                        item["top_two_proposal_ids"]
                        if item["status"] == "TYPE_III_TOP_TWO_READY"
                        else resolved_ties[item["option_set_id"]]
                    )
                ],
            }
            for item in initial["outcomes"]
            if item["status"]
            in {"TYPE_III_TOP_TWO_READY", "TYPE_III_TOP_TWO_TIE_REQUIRES_HUMAN"}
        ]
        self.engine.status.phase = MeetingPhase.BALLOT
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.BALLOT,
            f"收集 {len(active_records)} 名 ACTIVE voter 对 {len(runoffs)} 个 Type III 前二方案的密封决选票；全部完成前不公开票数",
        )
        frozen = self._collect(
            active_records=active_records,
            runoffs=runoffs,
            draft_path=draft_path,
            option_sets_path=option_sets_path,
            initial_outcomes_path=initial_outcomes_path,
        )
        outcomes = self._freeze_outcomes(
            frozen=frozen,
            runoffs=runoffs,
            eligible_count=len(active_records),
        )
        explanation_count = sum(
            item["status"] == "TYPE_II_EXPLANATION_ROUND_REQUIRED"
            for item in outcomes["outcomes"]
        )
        reason = self.EXPLANATION_BOUNDARY if explanation_count else self.APPLICATION_BOUNDARY
        self._pause(reason, outcomes_path="public/detailed_clauses/type_iii_runoff_outcomes.json")
        return ClauseRunoffResult(
            runoff_ballot_count=len(runoffs),
            direct_adoption_count=len(runoffs) - explanation_count,
            explanation_round_count=explanation_count,
            next_phase=MeetingPhase.PAUSED,
            paused_reason=reason,
        )

    def _ensure_resume_record(self) -> None:
        relative = Path("governance_private/detailed_clauses/type_iii_runoff_resume.json")
        if (self.repo.root / relative).exists():
            return
        record = {
            "meeting_id": self.repo.meeting_id,
            "prior_reason_code": "DETAILED_CLAUSE_FOLLOWUP_BALLOTS_NOT_IMPLEMENTED",
            "resume_stage": MeetingPhase.BALLOT.value,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append("MEETING_RESUMED", record, actor="orchestrator")

    def _active_records(self) -> list[dict]:
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        transition = json.loads(
            self.repo.docs.read_text("identity_private/general_principle/status_transition.json")
        )
        return [
            record
            for record in registry
            if transition["representative_statuses"][record["representative_id"]] == "ACTIVE"
        ]

    def _collect(
        self,
        *,
        active_records: list[dict],
        runoffs: list[dict],
        draft_path: Path,
        option_sets_path: Path,
        initial_outcomes_path: Path,
    ) -> dict:
        frozen_relative = Path("governance_private/detailed_clauses/type_iii_runoffs/frozen.json")
        frozen_path = self.repo.root / frozen_relative
        if frozen_path.exists():
            return json.loads(frozen_path.read_text(encoding="utf-8"))
        actions_by_id: dict[str, ClauseRunoffAction] = {}
        missing_records = []
        for index, record in enumerate(active_records, start=1):
            representative_id = record["representative_id"]
            relative = (
                Path("governance_private/detailed_clauses/type_iii_runoffs/submissions")
                / f"{representative_id}.json"
            )
            absolute = self.repo.root / relative
            if absolute.exists():
                action = ClauseRunoffAction.model_validate_json(absolute.read_text(encoding="utf-8"))
                self._validate_action(action, runoffs)
                self.engine.progress.info(
                    f"已恢复 {index}/{len(active_records)} 份完整 Type III 决选密封票"
                )
                actions_by_id[representative_id] = action
            else:
                missing_records.append(record)

        def collect_ballot(record: dict) -> tuple[str, ClauseRunoffAction, Path]:
            representative_id = record["representative_id"]
            relative = (
                Path("governance_private/detailed_clauses/type_iii_runoffs/submissions")
                / f"{representative_id}.json"
            )
            spec = self.resolver.representative_context_spec(
                persona=Persona(record["runtime"]["persona"]),
                stage="ballot",
                representative_id=representative_id,
                public_state_files=(draft_path, option_sets_path, initial_outcomes_path),
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
                    "Cast one complete sealed top-two runoff ballot covering every supplied runoff "
                    "option_set_id exactly once. Each runoff is now a Type II choice between exactly two "
                    "proposal IDs. Return exactly one JSON object with key votes. Each vote has only "
                    "option_set_id and choice; choice must exactly equal one top_two_proposal_id. "
                    "Abstention is forbidden. Evaluate each runoff independently. Do not add keys or use "
                    "Markdown. RUNOFFS:\n" + json.dumps(runoffs, indent=2, ensure_ascii=False)
                ),
                stage="detailed_clause_type_iii_runoff",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = ClauseRunoffAction.model_validate(json.loads(response.text))
                self._validate_action(action, runoffs)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            return representative_id, action, relative

        def persist_ballot(result: tuple[str, ClauseRunoffAction, Path]) -> None:
            representative_id, action, relative = result
            self.repo.docs.write_once(relative, action.model_dump_json(indent=2))
            self.repo.events.append(
                "DETAILED_CLAUSE_TYPE_III_RUNOFF_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "representative_id": representative_id,
                    "runoff_count": len(runoffs),
                    "record_path": str(relative),
                },
                actor=representative_id,
            )

        completed = run_bounded_representative_lanes(
            missing_records,
            collect_ballot,
            getattr(self.engine, "model_concurrency_limit", lambda *_args: 1),
            on_result=persist_ballot,
            progress=self.engine.progress,
            progress_records=active_records,
            completed_participant_ids=actions_by_id,
        )
        actions_by_id.update(
            {representative_id: action for representative_id, action, _ in completed}
        )
        submissions = [
            {
                "representative_id": record["representative_id"],
                "votes": actions_by_id[record["representative_id"]].model_dump(mode="json")["votes"],
            }
            for record in active_records
        ]
        frozen = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_voter_ids": [item["representative_id"] for item in active_records],
            "submission_count": len(submissions),
            "runoff_option_set_ids": [item["option_set_id"] for item in runoffs],
            "submissions": submissions,
        }
        self.repo.docs.write_once(frozen_relative, json.dumps(frozen, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_TYPE_III_RUNOFF_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "submission_count": len(submissions),
                "runoff_count": len(runoffs),
                "record_path": str(frozen_relative),
            },
            actor="orchestrator",
        )
        return frozen

    @staticmethod
    def _validate_action(action: ClauseRunoffAction, runoffs: list[dict]) -> None:
        by_id = {item["option_set_id"]: item for item in runoffs}
        actual = [vote.option_set_id for vote in action.votes]
        if len(actual) != len(set(actual)) or set(actual) != set(by_id):
            raise ValueError("runoff ballot must cover every runoff exactly once")
        for vote in action.votes:
            if vote.choice not in by_id[vote.option_set_id]["top_two_proposal_ids"]:
                raise ValueError("runoff choice must reference one frozen top-two proposal")

    def _freeze_outcomes(
        self,
        *,
        frozen: dict,
        runoffs: list[dict],
        eligible_count: int,
    ) -> dict:
        relative = Path("public/detailed_clauses/type_iii_runoff_outcomes.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return json.loads(absolute.read_text(encoding="utf-8"))
        required = high_threshold(self.repo, eligible_count)
        outcomes = []
        for runoff in runoffs:
            option_set_id = runoff["option_set_id"]
            choices = runoff["top_two_proposal_ids"]
            votes = [
                next(item for item in submission["votes"] if item["option_set_id"] == option_set_id)
                for submission in frozen["submissions"]
            ]
            tally = {choice: sum(vote["choice"] == choice for vote in votes) for choice in choices}
            winner = max(choices, key=lambda choice: tally[choice])
            outcome = {
                "option_set_id": option_set_id,
                "source_type": "TYPE_III_TOP_TWO",
                "eligible_count": eligible_count,
                "supermajority_required": required,
                "tally": tally,
            }
            if tally[winner] >= required:
                outcome.update(
                    status="TYPE_II_ADOPTED_FIRST_ROUND",
                    adopted_proposal_id=winner,
                )
            else:
                outcome.update(status="TYPE_II_EXPLANATION_ROUND_REQUIRED")
            outcomes.append(outcome)
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_count": eligible_count,
            "supermajority_formula": high_threshold_formula(self.repo),
            "supermajority_required": required,
            "outcomes": outcomes,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_TYPE_III_RUNOFF_OUTCOMES_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "eligible_count": eligible_count,
                "supermajority_required": required,
                "runoff_count": len(outcomes),
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        self.engine.progress.speech(
            "ASSEMBLY",
            "Type III 前二决选结果（密封窗口已冻结）",
            json.dumps(record, indent=2, ensure_ascii=False),
        )
        return record

    def _resolve_cutoff_ties(
        self,
        *,
        tied: list[dict],
        option_sets: dict[str, dict],
        active_records: list[dict],
        draft_path: Path,
        option_sets_path: Path,
        initial_outcomes_path: Path,
    ) -> dict[str, list[str]]:
        """Fill tied advancement seats through a voter runoff and Chair fallback."""

        self._ensure_cutoff_tiebreak_policy_record()
        tiebreaks = []
        for outcome in tied:
            tied_ids = list(outcome.get("tied_proposal_ids", []))
            tally = outcome.get("tally", {})
            if len(tied_ids) < 2 or any(item not in tally for item in tied_ids):
                raise ValueError("invalid frozen Type III cutoff tie metadata")
            cutoff = tally[tied_ids[0]]
            secured = sorted(
                proposal_id
                for proposal_id, votes in tally.items()
                if votes > cutoff
            )
            seats = 2 - len(secured)
            if seats < 1 or seats > 2 or len(tied_ids) < seats:
                raise ValueError("Type III tie cannot produce exactly two advancement seats")
            option_set = option_sets[outcome["option_set_id"]]
            tiebreaks.append(
                {
                    "option_set_id": outcome["option_set_id"],
                    "secured_proposal_ids": secured,
                    "advancement_seat_count": seats,
                    "tied_proposal_ids": tied_ids,
                    "options": [
                        option
                        for option in option_set["options"]
                        if option["proposal_id"] in tied_ids
                    ],
                }
            )

        self.engine.status.phase = MeetingPhase.BALLOT
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.BALLOT,
            f"Type III 晋级席位加赛 · {len(active_records)} 名 ACTIVE voter 对 "
            f"{len(tiebreaks)} 个并列集合提交密封票",
        )
        frozen = self._collect_cutoff_tiebreaks(
            active_records=active_records,
            tiebreaks=tiebreaks,
            draft_path=draft_path,
            option_sets_path=option_sets_path,
            initial_outcomes_path=initial_outcomes_path,
        )
        outcomes = self._freeze_cutoff_tiebreak_outcomes(
            frozen=frozen,
            tiebreaks=tiebreaks,
            eligible_count=len(active_records),
        )
        return {
            item["option_set_id"]: item["top_two_proposal_ids"]
            for item in outcomes["outcomes"]
        }

    def _ensure_cutoff_tiebreak_policy_record(self) -> None:
        relative = Path(
            "governance_private/detailed_clauses/type_iii_cutoff_tiebreak_policy.json"
        )
        if (self.repo.root / relative).exists():
            return
        record = {
            "meeting_id": self.repo.meeting_id,
            "policy_status": "CONFIRMED",
            "policy_version": "TYPE_III_GENERIC_CUTOFF_TIEBREAK_V2",
            "scope": "ANY_TYPE_III_ADVANCEMENT_CUTOFF_TIE",
            "procedure": "SEALED_TIED_CANDIDATE_RUNOFF_BY_ALL_ACTIVE_VOTERS",
            "winning_rule": "TOP_REMAINING_SEATS_THEN_PUBLIC_CHAIR_DECIDING_VOTE",
            "human_substantive_selection": False,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "TYPE_III_CUTOFF_TIEBREAK_POLICY_ACTIVATED",
            {**record, "record_path": str(relative)},
            actor="HUMAN",
        )

    def _collect_cutoff_tiebreaks(
        self,
        *,
        active_records: list[dict],
        tiebreaks: list[dict],
        draft_path: Path,
        option_sets_path: Path,
        initial_outcomes_path: Path,
    ) -> dict:
        root = Path("governance_private/detailed_clauses/type_iii_cutoff_tiebreaks")
        frozen_relative = root / "frozen.json"
        frozen_path = self.repo.root / frozen_relative
        if frozen_path.exists():
            return json.loads(frozen_path.read_text(encoding="utf-8"))
        actions_by_id: dict[str, ClauseRunoffAction] = {}
        missing_records = []
        for record in active_records:
            representative_id = record["representative_id"]
            relative = root / "submissions" / f"{representative_id}.json"
            absolute = self.repo.root / relative
            if absolute.exists():
                action = ClauseRunoffAction.model_validate_json(
                    absolute.read_text(encoding="utf-8")
                )
                self._validate_cutoff_tiebreak_action(action, tiebreaks)
                actions_by_id[representative_id] = action
                self.engine.progress.info(
                    f"已恢复 {representative_id} 的完整 Type III 第二席位加赛密封票"
                )
            else:
                missing_records.append(record)

        def collect(record: dict) -> tuple[str, ClauseRunoffAction, Path]:
            representative_id = record["representative_id"]
            relative = root / "submissions" / f"{representative_id}.json"
            spec = self.resolver.representative_context_spec(
                persona=Persona(record["runtime"]["persona"]),
                stage="ballot",
                representative_id=representative_id,
                public_state_files=(draft_path, option_sets_path, initial_outcomes_path),
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
                    "Cast one complete sealed procedural cutoff-tiebreak ballot covering every "
                    "supplied option_set_id exactly once. Choose exactly one of its tied_proposal_ids; "
                    "the tally fills the recorded number of remaining advancement seats. Abstention "
                    "is forbidden. Return exactly one JSON object with key votes; "
                    "each vote has only option_set_id and choice. Do not add reasons, keys, or "
                    "Markdown. TIEBREAKS:\n"
                    + json.dumps(tiebreaks, indent=2, ensure_ascii=False)
                ),
                stage="detailed_clause_type_iii_cutoff_tiebreak",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = ClauseRunoffAction.model_validate(json.loads(response.text))
                self._validate_cutoff_tiebreak_action(action, tiebreaks)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            return representative_id, action, relative

        def persist(result: tuple[str, ClauseRunoffAction, Path]) -> None:
            representative_id, action, relative = result
            self.repo.docs.write_once(relative, action.model_dump_json(indent=2))
            self.repo.events.append(
                "DETAILED_CLAUSE_TYPE_III_CUTOFF_TIEBREAK_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "representative_id": representative_id,
                    "tiebreak_count": len(tiebreaks),
                    "record_path": str(relative),
                },
                actor=representative_id,
            )

        completed = run_bounded_representative_lanes(
            missing_records,
            collect,
            getattr(self.engine, "model_concurrency_limit", lambda *_args: 1),
            on_result=persist,
            progress=self.engine.progress,
            progress_records=active_records,
            completed_participant_ids=actions_by_id,
        )
        actions_by_id.update(
            {representative_id: action for representative_id, action, _ in completed}
        )
        submissions = [
            {
                "representative_id": record["representative_id"],
                "votes": actions_by_id[record["representative_id"]].model_dump(mode="json")[
                    "votes"
                ],
            }
            for record in active_records
        ]
        frozen = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_voter_ids": [item["representative_id"] for item in active_records],
            "submission_count": len(submissions),
            "tiebreak_option_set_ids": [item["option_set_id"] for item in tiebreaks],
            "submissions": submissions,
        }
        self.repo.docs.write_once(
            frozen_relative, json.dumps(frozen, indent=2, ensure_ascii=False)
        )
        self.repo.events.append(
            "DETAILED_CLAUSE_TYPE_III_CUTOFF_TIEBREAK_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "submission_count": len(submissions),
                "tiebreak_count": len(tiebreaks),
                "record_path": str(frozen_relative),
            },
            actor="orchestrator",
        )
        return frozen

    @staticmethod
    def _validate_cutoff_tiebreak_action(
        action: ClauseRunoffAction, tiebreaks: list[dict]
    ) -> None:
        by_id = {item["option_set_id"]: item for item in tiebreaks}
        actual = [vote.option_set_id for vote in action.votes]
        if len(actual) != len(set(actual)) or set(actual) != set(by_id):
            raise ValueError("cutoff-tiebreak ballot must cover every tie exactly once")
        for vote in action.votes:
            if vote.choice not in by_id[vote.option_set_id]["tied_proposal_ids"]:
                raise ValueError("cutoff-tiebreak choice must reference one tied proposal")

    def _freeze_cutoff_tiebreak_outcomes(
        self,
        *,
        frozen: dict,
        tiebreaks: list[dict],
        eligible_count: int,
    ) -> dict:
        relative = Path("public/detailed_clauses/type_iii_cutoff_tiebreak_outcomes.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return json.loads(absolute.read_text(encoding="utf-8"))
        required = math.floor(eligible_count / 2) + 1
        outcomes = []
        for tiebreak in tiebreaks:
            option_set_id = tiebreak["option_set_id"]
            choices = tiebreak["tied_proposal_ids"]
            votes = [
                next(
                    vote
                    for vote in submission["votes"]
                    if vote["option_set_id"] == option_set_id
                )
                for submission in frozen["submissions"]
            ]
            tally = {
                choice: sum(vote["choice"] == choice for vote in votes)
                for choice in choices
            }
            seats = int(tiebreak["advancement_seat_count"])
            ordered = sorted(choices, key=lambda choice: (-tally[choice], choice))
            boundary = tally[ordered[seats - 1]]
            selected = [choice for choice in ordered if tally[choice] > boundary]
            boundary_tied = [choice for choice in ordered if tally[choice] == boundary]
            chair_decisions = []
            while len(selected) < seats:
                remaining = seats - len(selected)
                if len(boundary_tied) <= remaining:
                    selected.extend(boundary_tied)
                    boundary_tied = []
                    break
                decision = self._chair_cutoff_decision(
                    option_set_id=option_set_id,
                    candidates=boundary_tied,
                    seat_number=len(selected) + 1,
                )
                selected.append(decision.candidate_id)
                boundary_tied.remove(decision.candidate_id)
                chair_decisions.append(decision.model_dump(mode="json"))
            outcome = {
                "option_set_id": option_set_id,
                "eligible_count": eligible_count,
                "protective_majority_formula": "floor(N_ACTIVE/2)+1",
                "protective_majority_required": required,
                "secured_proposal_ids": tiebreak["secured_proposal_ids"],
                "tied_proposal_ids": choices,
                "tally": tally,
                "advancement_seat_count": seats,
                "chair_decisions": chair_decisions,
                "status": "TYPE_III_CUTOFF_TIEBREAK_RESOLVED",
                "top_two_proposal_ids": [
                    *tiebreak["secured_proposal_ids"],
                    *selected,
                ],
            }
            if seats == 1:
                outcome["secured_first_proposal_id"] = tiebreak["secured_proposal_ids"][0]
                outcome["tiebreak_winner_proposal_id"] = selected[0]
            outcomes.append(outcome)
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_count": eligible_count,
            "protective_majority_formula": "floor(N_ACTIVE/2)+1",
            "protective_majority_required": required,
            "outcomes": outcomes,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_TYPE_III_CUTOFF_TIEBREAK_OUTCOMES_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "eligible_count": eligible_count,
                "protective_majority_required": required,
                "resolved_count": sum(
                    item["status"] == "TYPE_III_CUTOFF_TIEBREAK_RESOLVED"
                    for item in outcomes
                ),
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        self.engine.progress.speech(
            "ASSEMBLY",
            "Type III 第二晋级席位加赛结果（密封窗口已冻结）",
            json.dumps(record, indent=2, ensure_ascii=False),
        )
        return record

    def _chair_cutoff_decision(
        self, *, option_set_id: str, candidates: list[str], seat_number: int
    ) -> ClauseTieChairDecision:
        stage = f"chair_type_iii_cutoff_tiebreak_{option_set_id}_{seat_number}"
        system_text = (
            "Task: cast the Chair's public procedural deciding vote after an Assembly cutoff "
            "runoff remained tied. Select exactly one supplied proposal ID and give one concise "
            "public reason. Do not alter, merge, or add a proposal."
        )
        user_text = (
            f"OPTION SET: {option_set_id}\nTIED CANDIDATES: "
            + json.dumps(candidates, ensure_ascii=False)
        )
        response = self.engine.find_recorded_response(
            "CHAIR", system_text=system_text, user_text=user_text, stage=stage
        )
        if response is None:
            response = self.engine.invoke_participant(
                "CHAIR",
                system_text=system_text,
                user_text=user_text,
                stage=stage,
                max_output_tokens=self.max_output_tokens,
            )
        decision = self.engine.validate_structured_response(
            "CHAIR",
            response=response,
            schema_model=ClauseTieChairDecision,
            stage=stage,
            max_output_tokens=self.max_output_tokens,
        )
        if decision.candidate_id not in candidates:
            raise ValueError("Chair cutoff decision selected an ineligible proposal")
        return decision

    def _pause(self, reason: str, *, outcomes_path: str) -> None:
        relative = Path("governance_private/detailed_clauses") / f"pause_{reason.lower()}.json"
        if not (self.repo.root / relative).exists():
            self.repo.docs.write_once(
                relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "reason_code": reason,
                        "outcomes_path": outcomes_path,
                    },
                    indent=2,
                ),
            )
            self.repo.events.append(
                "MEETING_PAUSED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "reason_code": reason,
                    "record_path": str(relative),
                },
                actor="orchestrator",
            )
        self.engine.status.phase = MeetingPhase.PAUSED
        self.engine.status.paused_reason = reason
        explanations = {
            self.EXPLANATION_BOUNDARY: (
                "至少一个前二决选未达到本场高门槛（"
                + high_threshold_formula(self.repo)
                + "）；下一步开启解释窗口"
            ),
            self.APPLICATION_BOUNDARY: "所有投票已产生结果；下一步把通过提案应用到细则文本",
            self.TIE_BOUNDARY: "Type III 第二晋级席位并列；须按确认程序加赛",
            self.TIEBREAK_DEADLOCK_BOUNDARY: "第二晋级席位加赛未产生严格多数；需要 Human 进一步裁定",
            self.COMPLEX_TIE_BOUNDARY: "并列形态超出已确认的二选一第二席位加赛范围；需要 Human 裁定",
        }
        explanation = explanations.get(reason, "程序需要 Human 介入")
        self.engine.progress.status(MeetingPhase.PAUSED, f"{reason} · {explanation}")
