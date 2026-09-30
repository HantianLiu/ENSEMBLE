from __future__ import annotations

import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.governance_private.thresholds import high_threshold
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.clause_type_ii_reconstruction import (
    TypeIINewOptionRunner,
)
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.storage.meeting import MeetingRepository


class ClauseChoiceExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_set_id: str = Field(min_length=1)
    preferred_proposal_id: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ClauseExplanationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanations: list[ClauseChoiceExplanation]


class GenuinelyNewClauseOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    substantive_gain: str = Field(min_length=1)


class ClauseSecondVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_set_id: str = Field(min_length=1)
    choice: str | None = None
    new_option: GenuinelyNewClauseOption | None = None

    @model_validator(mode="after")
    def exactly_one_disposition(self) -> "ClauseSecondVote":
        if (self.choice is None) == (self.new_option is None):
            raise ValueError("exactly one of choice or new_option is required")
        return self


class ClauseSecondBallotAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    votes: list[ClauseSecondVote]


class ClauseExplanationResult(BaseModel):
    explanation_set_count: int
    explanation_submission_count: int
    second_ballot_count: int
    adopted_count: int
    contested_count: int
    new_option_count: int
    effective_option_sets_path: str
    second_outcomes_path: str
    next_phase: MeetingPhase
    paused_reason: str


class ClauseExplanationRunner:
    """Run the public explanation window and sealed second Type II ballot."""

    NEW_OPTION_BOUNDARY = "TYPE_II_NEW_OPTION_RECONSTRUCTION_NOT_IMPLEMENTED"
    APPLICATION_BOUNDARY = "ADOPTED_CLAUSE_APPLICATION_NOT_IMPLEMENTED"

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
        runoff_outcomes_path: Path,
    ) -> ClauseExplanationResult:
        self._ensure_resume_record()
        unresolved = self._unresolved_sets(
            option_sets_path=option_sets_path,
            initial_outcomes_path=initial_outcomes_path,
            runoff_outcomes_path=runoff_outcomes_path,
        )
        active_records = self._active_records()
        public_explanations = self._collect_explanations(
            active_records=active_records,
            unresolved=unresolved,
            draft_path=draft_path,
            option_sets_path=option_sets_path,
        )
        frozen_ballots = self._collect_second_ballots(
            active_records=active_records,
            unresolved=unresolved,
            draft_path=draft_path,
            public_explanations_path=public_explanations,
        )
        new_options = self._new_options(frozen_ballots=frozen_ballots, unresolved=unresolved)
        effective_option_sets_path = str(option_sets_path.relative_to(self.repo.root))
        second_outcomes_path = "public/detailed_clauses/type_ii_second_ballot_outcomes.json"
        if new_options:
            self._freeze_new_options(new_options)
            reconstruction = TypeIINewOptionRunner(
                repo=self.repo,
                engine=self.engine,
                max_output_tokens=self.max_output_tokens,
            ).run(
                draft_path=draft_path,
                option_sets_path=option_sets_path,
                unresolved=unresolved,
                active_records=active_records,
                frozen_second_ballots=frozen_ballots,
                new_options=new_options,
            )
            effective_option_sets_path = reconstruction.effective_option_sets_path
            second_outcomes_path = reconstruction.outcomes_path
            outcomes = json.loads(
                (self.repo.root / reconstruction.outcomes_path).read_text(encoding="utf-8")
            )
            adopted_count = sum(
                item.get("adopted_proposal_id") is not None
                for item in outcomes["outcomes"]
            )
            contested_count = sum(
                bool(item.get("contested", False)) for item in outcomes["outcomes"]
            )
            reason = self.APPLICATION_BOUNDARY
        else:
            outcomes = self._freeze_second_outcomes(
                frozen_ballots=frozen_ballots,
                unresolved=unresolved,
                eligible_count=len(active_records),
            )
            adopted_count = len(outcomes["outcomes"])
            contested_count = sum(item["contested"] for item in outcomes["outcomes"])
            reason = self.APPLICATION_BOUNDARY
        self._pause(reason)
        return ClauseExplanationResult(
            explanation_set_count=len(unresolved),
            explanation_submission_count=len(active_records),
            second_ballot_count=(0 if new_options else len(unresolved)),
            adopted_count=adopted_count,
            contested_count=contested_count,
            new_option_count=len(new_options),
            effective_option_sets_path=effective_option_sets_path,
            second_outcomes_path=second_outcomes_path,
            next_phase=MeetingPhase.PAUSED,
            paused_reason=reason,
        )

    def _ensure_resume_record(self) -> None:
        relative = Path("governance_private/detailed_clauses/explanation_round_resume.json")
        if (self.repo.root / relative).exists():
            return
        record = {
            "meeting_id": self.repo.meeting_id,
            "prior_reason_code": "DETAILED_CLAUSE_EXPLANATION_ROUND_NOT_IMPLEMENTED",
            "resume_stage": MeetingPhase.BALLOT.value,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append("MEETING_RESUMED", record, actor="orchestrator")

    def _unresolved_sets(
        self,
        *,
        option_sets_path: Path,
        initial_outcomes_path: Path,
        runoff_outcomes_path: Path,
    ) -> list[dict]:
        options = {
            item["option_set_id"]: item
            for item in json.loads(option_sets_path.read_text(encoding="utf-8"))["option_sets"]
        }
        initial = json.loads(initial_outcomes_path.read_text(encoding="utf-8"))
        runoff = json.loads(runoff_outcomes_path.read_text(encoding="utf-8"))
        unresolved_outcomes = [
            item
            for item in [*initial["outcomes"], *runoff["outcomes"]]
            if item["status"] == "TYPE_II_EXPLANATION_ROUND_REQUIRED"
        ]
        result = []
        for outcome in unresolved_outcomes:
            source = options[outcome["option_set_id"]]
            legal_ids = list(outcome["tally"])
            if len(legal_ids) != 2:
                raise ValueError("a Type II explanation set must contain exactly two choices")
            result.append(
                {
                    "option_set_id": outcome["option_set_id"],
                    "proposal_ids": legal_ids,
                    "first_round_tally": outcome["tally"],
                    "options": [
                        item for item in source["options"] if item["proposal_id"] in legal_ids
                    ],
                }
            )
        if not result:
            raise ValueError("explanation round requested without an unresolved Type II set")
        return result

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

    def _collect_explanations(
        self,
        *,
        active_records: list[dict],
        unresolved: list[dict],
        draft_path: Path,
        option_sets_path: Path,
    ) -> Path:
        public_relative = Path("public/detailed_clauses/type_ii_explanations.json")
        public_path = self.repo.root / public_relative
        if public_path.exists():
            return public_path
        self.engine.status.phase = MeetingPhase.CLAUSE_REVIEW
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.CLAUSE_REVIEW,
            f"收集 {len(active_records)} 名 ACTIVE voter 对 {len(unresolved)} 个未决 Type II 选择的解释；全部完成前不公开",
        )
        actions_by_id: dict[str, ClauseExplanationAction] = {}
        missing_records = []
        for index, record in enumerate(active_records, start=1):
            representative_id = record["representative_id"]
            relative = (
                Path("governance_private/detailed_clauses/type_ii_explanations/submissions")
                / f"{representative_id}.json"
            )
            absolute = self.repo.root / relative
            if absolute.exists():
                action = ClauseExplanationAction.model_validate_json(
                    absolute.read_text(encoding="utf-8")
                )
                self._validate_explanations(action, unresolved)
                self.engine.progress.info(
                    f"已恢复 {index}/{len(active_records)} 份完整 Type II 解释"
                )
                actions_by_id[representative_id] = action
            else:
                missing_records.append(record)

        def collect_explanation(record: dict) -> tuple[str, ClauseExplanationAction, Path]:
            representative_id = record["representative_id"]
            relative = (
                Path("governance_private/detailed_clauses/type_ii_explanations/submissions")
                / f"{representative_id}.json"
            )
            response = self.engine.invoke_participant(
                representative_id,
                system_text=self._context(
                    record=record,
                    draft_path=draft_path,
                    public_paths=(option_sets_path,),
                ),
                user_text=(
                    "Use your one explanation opportunity for every unresolved Type II choice. Return "
                    "exactly one JSON object with key explanations. Each item has option_set_id, "
                    "preferred_proposal_id, and a substantive explanation addressing the comparative "
                    "merits and risks of the two frozen options. Cover every supplied option_set_id "
                    "exactly once. This is an explanation, not the second ballot; do not introduce or "
                    "rewrite options here. Do not add keys or use Markdown. UNRESOLVED SETS:\n"
                    + json.dumps(unresolved, indent=2, ensure_ascii=False)
                ),
                stage="detailed_clause_type_ii_explanation",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = ClauseExplanationAction.model_validate(json.loads(response.text))
                self._validate_explanations(action, unresolved)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            return representative_id, action, relative

        def persist_explanation(result: tuple[str, ClauseExplanationAction, Path]) -> None:
            _representative_id, action, relative = result
            self.repo.docs.write_once(relative, action.model_dump_json(indent=2))

        completed = run_bounded_representative_lanes(
            missing_records,
            collect_explanation,
            getattr(self.engine, "model_concurrency_limit", lambda *_args: 1),
            on_result=persist_explanation,
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
                "explanations": actions_by_id[record["representative_id"]].model_dump(mode="json")["explanations"],
            }
            for record in active_records
        ]
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "submission_count": len(submissions),
            "submissions": submissions,
        }
        self.repo.docs.write_once(public_relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_TYPE_II_EXPLANATIONS_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "submission_count": len(submissions),
                "option_set_count": len(unresolved),
                "record_path": str(public_relative),
            },
            actor="orchestrator",
        )
        self.engine.progress.speech(
            "ASSEMBLY", "Type II 解释窗口（已冻结）", json.dumps(record, indent=2, ensure_ascii=False)
        )
        return public_path

    def _collect_second_ballots(
        self,
        *,
        active_records: list[dict],
        unresolved: list[dict],
        draft_path: Path,
        public_explanations_path: Path,
    ) -> dict:
        frozen_relative = Path("governance_private/detailed_clauses/type_ii_second_ballots/frozen.json")
        frozen_path = self.repo.root / frozen_relative
        if frozen_path.exists():
            return json.loads(frozen_path.read_text(encoding="utf-8"))
        self.engine.status.phase = MeetingPhase.BALLOT
        self.engine.progress.status(
            MeetingPhase.BALLOT,
            f"解释窗口已冻结；收集 {len(active_records)} 名 ACTIVE voter 的第二轮密封票",
        )
        actions_by_id: dict[str, ClauseSecondBallotAction] = {}
        missing_records = []
        for index, record in enumerate(active_records, start=1):
            representative_id = record["representative_id"]
            relative = (
                Path("governance_private/detailed_clauses/type_ii_second_ballots/submissions")
                / f"{representative_id}.json"
            )
            absolute = self.repo.root / relative
            if absolute.exists():
                action = ClauseSecondBallotAction.model_validate_json(
                    absolute.read_text(encoding="utf-8")
                )
                self._validate_second_ballot(action, unresolved)
                self.engine.progress.info(
                    f"已恢复 {index}/{len(active_records)} 份完整 Type II 第二轮密封票"
                )
                actions_by_id[representative_id] = action
            else:
                missing_records.append(record)

        def collect_ballot(record: dict) -> tuple[str, ClauseSecondBallotAction, Path]:
            representative_id = record["representative_id"]
            relative = (
                Path("governance_private/detailed_clauses/type_ii_second_ballots/submissions")
                / f"{representative_id}.json"
            )
            response = self.engine.invoke_participant(
                representative_id,
                system_text=self._context(
                    record=record,
                    draft_path=draft_path,
                    public_paths=(public_explanations_path,),
                ),
                user_text=(
                    "Cast one complete second-round sealed ballot for every unresolved Type II set after "
                    "reading the frozen explanations. Return exactly one JSON object with key votes. Each "
                    "vote has option_set_id and exactly one of: (a) choice equal to one frozen proposal_id, "
                    "with new_option null; or (b) choice null and new_option containing text, reason, and "
                    "substantive_gain. A new option is allowed only if it is genuinely distinct, addresses "
                    "the same target question, and states its substantive gain over both existing options; "
                    "do not use it merely to rewrite or combine an existing option. Abstention is forbidden. "
                    "Cover every set exactly once. Do not add keys or use Markdown. UNRESOLVED SETS:\n"
                    + json.dumps(unresolved, indent=2, ensure_ascii=False)
                ),
                stage="detailed_clause_type_ii_second_ballot",
                max_output_tokens=self.max_output_tokens,
            )
            try:
                action = ClauseSecondBallotAction.model_validate(json.loads(response.text))
                self._validate_second_ballot(action, unresolved)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                self.engine.pause_for_unconfigured_policy(
                    participant_id=representative_id,
                    reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                )
            return representative_id, action, relative

        def persist_ballot(result: tuple[str, ClauseSecondBallotAction, Path]) -> None:
            _representative_id, action, relative = result
            self.repo.docs.write_once(relative, action.model_dump_json(indent=2))

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
            "submission_count": len(submissions),
            "submissions": submissions,
        }
        self.repo.docs.write_once(frozen_relative, json.dumps(frozen, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_TYPE_II_SECOND_BALLOT_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "submission_count": len(submissions),
                "option_set_count": len(unresolved),
                "record_path": str(frozen_relative),
            },
            actor="orchestrator",
        )
        return frozen

    def _context(
        self,
        *,
        record: dict,
        draft_path: Path,
        public_paths: tuple[Path, ...],
    ) -> str:
        representative_id = record["representative_id"]
        spec = self.resolver.representative_context_spec(
            persona=Persona(record["runtime"]["persona"]),
            stage="ballot",
            representative_id=representative_id,
            public_state_files=(draft_path, *public_paths),
            own_state_files=(
                self.repo.root
                / "representatives"
                / representative_id
                / "status_transition_001.json",
            ),
        )
        return self.assembler.assemble(spec)

    @staticmethod
    def _validate_explanations(action: ClauseExplanationAction, unresolved: list[dict]) -> None:
        by_id = {item["option_set_id"]: item for item in unresolved}
        actual = [item.option_set_id for item in action.explanations]
        if len(actual) != len(set(actual)) or set(actual) != set(by_id):
            raise ValueError("explanations must cover every unresolved set exactly once")
        for item in action.explanations:
            if item.preferred_proposal_id not in by_id[item.option_set_id]["proposal_ids"]:
                raise ValueError("preferred proposal must be one of the frozen Type II choices")

    @staticmethod
    def _validate_second_ballot(action: ClauseSecondBallotAction, unresolved: list[dict]) -> None:
        by_id = {item["option_set_id"]: item for item in unresolved}
        actual = [item.option_set_id for item in action.votes]
        if len(actual) != len(set(actual)) or set(actual) != set(by_id):
            raise ValueError("second ballot must cover every unresolved set exactly once")
        for item in action.votes:
            if item.choice is not None and item.choice not in by_id[item.option_set_id]["proposal_ids"]:
                raise ValueError("second-ballot choice must be one of the frozen Type II choices")

    @staticmethod
    def _new_options(*, frozen_ballots: dict, unresolved: list[dict]) -> list[dict]:
        targets = {item["option_set_id"]: item for item in unresolved}
        result = []
        counters: dict[str, int] = {}
        for submission in frozen_ballots["submissions"]:
            for vote in submission["votes"]:
                if vote["new_option"] is None:
                    continue
                option_set_id = vote["option_set_id"]
                counters[option_set_id] = counters.get(option_set_id, 0) + 1
                public_set_id = "".join(
                    character if character.isalnum() else "-"
                    for character in option_set_id
                ).strip("-")
                result.append(
                    {
                        "proposal_id": f"CP2-{public_set_id}-{counters[option_set_id]:03d}",
                        "proposer_id": submission["representative_id"],
                        "option_set_id": option_set_id,
                        "competes_with": targets[option_set_id]["proposal_ids"],
                        **vote["new_option"],
                    }
                )
        return result

    def _freeze_new_options(self, new_options: list[dict]) -> None:
        relative = Path(
            "governance_private/detailed_clauses/type_ii_reconstruction/raw_new_options.json"
        )
        if (self.repo.root / relative).exists():
            return
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "new_option_count": len(new_options),
            "new_options": new_options,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_TYPE_II_NEW_OPTIONS_FROZEN",
            {"meeting_id": self.repo.meeting_id, "new_option_count": len(new_options), "record_path": str(relative)},
            actor="orchestrator",
        )

    def _freeze_second_outcomes(
        self,
        *,
        frozen_ballots: dict,
        unresolved: list[dict],
        eligible_count: int,
    ) -> dict:
        relative = Path("public/detailed_clauses/type_ii_second_ballot_outcomes.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return json.loads(absolute.read_text(encoding="utf-8"))
        required = high_threshold(self.repo, eligible_count)
        outcomes = []
        for option_set in unresolved:
            option_set_id = option_set["option_set_id"]
            choices = option_set["proposal_ids"]
            votes = [
                next(item for item in submission["votes"] if item["option_set_id"] == option_set_id)
                for submission in frozen_ballots["submissions"]
            ]
            tally = {choice: sum(vote["choice"] == choice for vote in votes) for choice in choices}
            winner = max(choices, key=lambda choice: tally[choice])
            outcomes.append(
                {
                    "option_set_id": option_set_id,
                    "eligible_count": eligible_count,
                    "supermajority_required": required,
                    "tally": tally,
                    "adopted_proposal_id": winner,
                    "contested": tally[winner] < required,
                    "status": (
                        "TYPE_II_ADOPTED_SECOND_ROUND_CONTESTED"
                        if tally[winner] < required
                        else "TYPE_II_ADOPTED_SECOND_ROUND"
                    ),
                }
            )
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_count": eligible_count,
            "supermajority_required": required,
            "outcomes": outcomes,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_TYPE_II_SECOND_BALLOT_OUTCOMES_FROZEN",
            {"meeting_id": self.repo.meeting_id, "option_set_count": len(outcomes), "record_path": str(relative)},
            actor="orchestrator",
        )
        self.engine.progress.speech(
            "ASSEMBLY", "Type II 第二轮表决结果（密封窗口已冻结）", json.dumps(record, indent=2, ensure_ascii=False)
        )
        return record

    def _pause(self, reason: str) -> None:
        relative = Path("governance_private/detailed_clauses") / f"pause_{reason.lower()}.json"
        if not (self.repo.root / relative).exists():
            self.repo.docs.write_once(
                relative,
                json.dumps({"meeting_id": self.repo.meeting_id, "reason_code": reason}, indent=2),
            )
            self.repo.events.append(
                "MEETING_PAUSED",
                {"meeting_id": self.repo.meeting_id, "reason_code": reason, "record_path": str(relative)},
                actor="orchestrator",
            )
        self.engine.status.phase = MeetingPhase.PAUSED
        self.engine.status.paused_reason = reason
        message = (
            "第二轮出现 genuinely new option；须重构为 Type III option set"
            if reason == self.NEW_OPTION_BOUNDARY
            else "细则投票结果已冻结；下一步由 Chair 将通过提案写入新稿"
        )
        self.engine.progress.status(MeetingPhase.PAUSED, f"{reason} · {message}")
