from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from project_ensemble.domain import MeetingPhase, Persona
from project_ensemble.governance_private.thresholds import high_threshold, high_threshold_formula
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.runtime.model_lanes import run_bounded_model_lanes
from project_ensemble.storage.meeting import MeetingRepository


_ActionT = TypeVar("_ActionT")


class TypeIRevisionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_set_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    action: Literal["RETAIN", "REVISE"]
    final_text: str = Field(min_length=1)
    revision_summary: str = Field(min_length=1, max_length=300)


class TypeIRevisionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[TypeIRevisionDecision]


class TypeISecondVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_set_id: str = Field(min_length=1)
    choice: Literal["SUPPORT", "OPPOSE"]


class TypeISecondBallotAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    votes: list[TypeISecondVote]


class TypeIFollowupResult(BaseModel):
    option_set_count: int
    proposer_submission_count: int
    voter_submission_count: int
    adopted_count: int
    contested_count: int
    rejected_count: int
    revisions_path: str
    outcomes_path: str


class ClauseTypeIFollowupRunner:
    """Run the missing proposer-response and second-vote branch for Type I sets."""

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
    ) -> TypeIFollowupResult:
        unresolved = self._unresolved_sets(
            option_sets_path=option_sets_path,
            initial_outcomes_path=initial_outcomes_path,
        )
        if not unresolved:
            raise ValueError("Type I follow-up requested without an unresolved Type I set")
        revisions_path, revisions = self._collect_revisions(
            unresolved=unresolved,
            draft_path=draft_path,
            option_sets_path=option_sets_path,
            initial_outcomes_path=initial_outcomes_path,
        )
        active_records = self._active_records()
        frozen = self._collect_second_ballots(
            active_records=active_records,
            revisions=revisions,
            draft_path=draft_path,
            revisions_path=revisions_path,
        )
        outcomes_path, outcomes = self._freeze_outcomes(
            frozen=frozen,
            revisions=revisions,
            eligible_count=len(active_records),
        )
        records = outcomes["outcomes"]
        return TypeIFollowupResult(
            option_set_count=len(unresolved),
            proposer_submission_count=len({item["proposer_id"] for item in unresolved}),
            voter_submission_count=len(active_records),
            adopted_count=sum(item.get("adopted_proposal_id") is not None for item in records),
            contested_count=sum(bool(item.get("contested")) for item in records),
            rejected_count=sum(item["status"] == "TYPE_I_REJECTED_SECOND_ROUND" for item in records),
            revisions_path=str(revisions_path.relative_to(self.repo.root)),
            outcomes_path=str(outcomes_path.relative_to(self.repo.root)),
        )

    def _unresolved_sets(
        self,
        *,
        option_sets_path: Path,
        initial_outcomes_path: Path,
    ) -> list[dict]:
        option_sets = {
            item["option_set_id"]: item
            for item in json.loads(option_sets_path.read_text(encoding="utf-8"))["option_sets"]
        }
        outcomes = json.loads(initial_outcomes_path.read_text(encoding="utf-8"))["outcomes"]
        result = []
        for outcome in outcomes:
            if outcome["status"] != "TYPE_I_SECOND_ROUND_REQUIRED":
                continue
            option_set = option_sets[outcome["option_set_id"]]
            if option_set["option_set_type"] != "TYPE_I" or len(option_set["options"]) != 1:
                raise ValueError("Type I follow-up source must contain exactly one proposal")
            proposal = option_set["options"][0]
            result.append(
                {
                    "option_set_id": option_set["option_set_id"],
                    "proposal_id": proposal["proposal_id"],
                    "proposer_id": proposal["proposer_id"],
                    "target_clause_id": option_set["target_clause_id"],
                    "proposal_type": proposal["proposal_type"],
                    "original_text": proposal["text"],
                    "first_round_tally": outcome["tally"],
                }
            )
        return result

    def _collect_revisions(
        self,
        *,
        unresolved: list[dict],
        draft_path: Path,
        option_sets_path: Path,
        initial_outcomes_path: Path,
    ) -> tuple[Path, dict]:
        public_relative = Path("public/detailed_clauses/type_i_revisions.json")
        public_path = self.repo.root / public_relative
        if public_path.exists():
            record = json.loads(public_path.read_text(encoding="utf-8"))
            self._validate_frozen_revisions(record, unresolved)
            return public_path, record

        initial_ballots = json.loads(
            self.repo.docs.read_text(
                "governance_private/detailed_clauses/initial_ballots/frozen.json"
            )
        )
        by_proposer: dict[str, list[dict]] = {}
        for item in unresolved:
            opposition_reasons = []
            for submission in initial_ballots["submissions"]:
                vote = next(
                    vote
                    for vote in submission["votes"]
                    if vote["option_set_id"] == item["option_set_id"]
                )
                if vote["choice"] == "OPPOSE":
                    opposition_reasons.append(
                        {
                            "reason_number": len(opposition_reasons) + 1,
                            "reason": vote["reason"],
                        }
                    )
            by_proposer.setdefault(item["proposer_id"], []).append(
                {**item, "opposition_reasons": opposition_reasons}
            )

        registry = {
            item["representative_id"]: item
            for item in json.loads(
                self.repo.docs.read_text("identity_private/representative_registry.json")
            )
        }
        self.engine.status.phase = MeetingPhase.CLAUSE_REVIEW
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.CLAUSE_REVIEW,
            f"收集 {len(by_proposer)} 名 proposer 对 {len(unresolved)} 个未决 Type I 提案的保留/修订决定",
        )
        work = [
            {
                "participant_id": proposer_id,
                "record": registry[proposer_id],
                "items": items,
            }
            for proposer_id, items in by_proposer.items()
        ]
        actions = self._run_model_lanes(
            work,
            lambda item: self._collect_one_revision(
                proposer_id=item["participant_id"],
                record=item["record"],
                items=item["items"],
                draft_path=draft_path,
                option_sets_path=option_sets_path,
                initial_outcomes_path=initial_outcomes_path,
            ),
            concurrency_limit=self.engine.model_concurrency_limit,
        )
        decisions = [
            decision.model_dump(mode="json")
            for proposer_id in by_proposer
            for decision in actions[proposer_id].decisions
        ]

        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "option_set_count": len(unresolved),
            "decisions": decisions,
        }
        self._validate_frozen_revisions(record, unresolved)
        self.repo.docs.write_once(
            public_relative,
            json.dumps(record, indent=2, ensure_ascii=False),
        )
        self.repo.events.append(
            "TYPE_I_REVISIONS_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "option_set_count": len(unresolved),
                "record_path": str(public_relative),
            },
            actor="orchestrator",
        )
        self.engine.progress.speech(
            "ASSEMBLY",
            "Type I 提案人决定（已冻结）",
            json.dumps(record, indent=2, ensure_ascii=False),
        )
        return public_path, record

    def _collect_one_revision(
        self,
        *,
        proposer_id: str,
        record: dict,
        items: list[dict],
        draft_path: Path,
        option_sets_path: Path,
        initial_outcomes_path: Path,
    ) -> TypeIRevisionAction:
        input_relative = (
            Path("governance_private/detailed_clauses/type_i_revision_inputs")
            / f"{proposer_id}.json"
        )
        input_record = {
            "meeting_id": self.repo.meeting_id,
            "proposer_id": proposer_id,
            "items": items,
        }
        input_path = self.repo.root / input_relative
        if input_path.exists():
            if json.loads(input_path.read_text(encoding="utf-8")) != input_record:
                raise ValueError("frozen Type I proposer input does not match first-round ballots")
        else:
            self.repo.docs.write_once(
                input_relative,
                json.dumps(input_record, indent=2, ensure_ascii=False),
            )
        submission_relative = (
            Path("governance_private/detailed_clauses/type_i_revisions/submissions")
            / f"{proposer_id}.json"
        )
        submission_path = self.repo.root / submission_relative
        if submission_path.exists():
            action = TypeIRevisionAction.model_validate_json(
                submission_path.read_text(encoding="utf-8")
            )
        else:
            spec = self.resolver.representative_context_spec(
                persona=Persona(record["runtime"]["persona"]),
                stage="type_i_revision",
                representative_id=proposer_id,
                public_state_files=(draft_path, option_sets_path, initial_outcomes_path),
                own_state_files=(input_path,),
            )
            system_text = self.assembler.assemble(spec)
            user_text = (
                "For every Type I proposal in YOUR STATE, decide after reading its first-round "
                "OPPOSE reasons whether to RETAIN the exact original text or REVISE it. Return "
                'exactly one JSON object {"decisions":[...]} with option_set_id, proposal_id, '
                "action RETAIN or REVISE, final_text, and a concise revision_summary. RETAIN must "
                "copy original_text exactly into final_text. REVISE must preserve the frozen target "
                "and proposal type, address the objections without creating another competing option, "
                "and put the complete replacement proposal wording in final_text. Cover every supplied "
                "item exactly once. Do not use Markdown."
            )
            response = self.engine.find_recorded_response(
                proposer_id,
                system_text=system_text,
                user_text=user_text,
                stage="type_i_proposer_revision",
            )
            if response is None:
                response = self.engine.invoke_participant(
                    proposer_id,
                    system_text=system_text,
                    user_text=user_text,
                    stage="type_i_proposer_revision",
                    max_output_tokens=self.max_output_tokens,
                )
            action = self.engine.validate_structured_response(
                proposer_id,
                response=response,
                schema_model=TypeIRevisionAction,
                stage="type_i_proposer_revision",
                max_output_tokens=self.max_output_tokens,
                semantic_requirement=(
                    "Preserve every frozen Type I ID/target/type; RETAIN copies the original text "
                    "and REVISE supplies one complete revised version after considering objections."
                ),
            )
            self._validate_revision_action(action, items)
            self.repo.docs.write_once(submission_relative, action.model_dump_json(indent=2))
            self.repo.events.append(
                "TYPE_I_PROPOSER_REVISION_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "proposer_id": proposer_id,
                    "option_set_count": len(items),
                    "record_path": str(submission_relative),
                },
                actor=proposer_id,
            )
        self._validate_revision_action(action, items)
        return action

    def _collect_second_ballots(
        self,
        *,
        active_records: list[dict],
        revisions: dict,
        draft_path: Path,
        revisions_path: Path,
    ) -> dict:
        frozen_relative = Path(
            "governance_private/detailed_clauses/type_i_second_ballots/frozen.json"
        )
        frozen_path = self.repo.root / frozen_relative
        if frozen_path.exists():
            return json.loads(frozen_path.read_text(encoding="utf-8"))
        self.engine.status.phase = MeetingPhase.BALLOT
        self.engine.progress.status(
            MeetingPhase.BALLOT,
            f"收集 {len(active_records)} 名 ACTIVE voter 对 {len(revisions['decisions'])} 个 Type I 最终文本的第二轮密封票",
        )
        actions = self._run_model_lanes(
            [
                {"participant_id": record["representative_id"], "record": record}
                for record in active_records
            ],
            lambda item: self._collect_one_second_ballot(
                record=item["record"],
                revisions=revisions,
                draft_path=draft_path,
                revisions_path=revisions_path,
            ),
            concurrency_limit=self.engine.model_concurrency_limit,
        )
        submissions = [
            {
                "representative_id": record["representative_id"],
                "votes": actions[record["representative_id"]].model_dump(mode="json")[
                    "votes"
                ],
            }
            for record in active_records
        ]
        frozen = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "submission_count": len(submissions),
            "submissions": submissions,
        }
        self.repo.docs.write_once(
            frozen_relative,
            json.dumps(frozen, indent=2, ensure_ascii=False),
        )
        self.repo.events.append(
            "TYPE_I_SECOND_BALLOT_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "submission_count": len(submissions),
                "option_set_count": len(revisions["decisions"]),
                "record_path": str(frozen_relative),
            },
            actor="orchestrator",
        )
        return frozen

    def _collect_one_second_ballot(
        self,
        *,
        record: dict,
        revisions: dict,
        draft_path: Path,
        revisions_path: Path,
    ) -> TypeISecondBallotAction:
        representative_id = record["representative_id"]
        relative = (
            Path("governance_private/detailed_clauses/type_i_second_ballots/submissions")
            / f"{representative_id}.json"
        )
        absolute = self.repo.root / relative
        if absolute.exists():
            action = TypeISecondBallotAction.model_validate_json(
                absolute.read_text(encoding="utf-8")
            )
        else:
            spec = self.resolver.representative_context_spec(
                persona=Persona(record["runtime"]["persona"]),
                stage="ballot",
                representative_id=representative_id,
                public_state_files=(draft_path, revisions_path),
                own_state_files=(
                    self.repo.root
                    / "representatives"
                    / representative_id
                    / "status_transition_001.json",
                ),
            )
            system_text = self.assembler.assemble(spec)
            user_text = (
                "Cast the second and final Type I ballot on every supplied option_set_id. "
                'Return exactly {"votes":[{"option_set_id":"OS-...","choice":"SUPPORT|OPPOSE"}]}. '
                "Cover every supplied set exactly once; do not add reasons, keys, abstentions, or Markdown."
            )
            response = self.engine.find_recorded_response(
                representative_id,
                system_text=system_text,
                user_text=user_text,
                stage="type_i_second_ballot",
            )
            if response is None:
                response = self.engine.invoke_participant(
                    representative_id,
                    system_text=system_text,
                    user_text=user_text,
                    stage="type_i_second_ballot",
                    max_output_tokens=self.max_output_tokens,
                )
            action = self.engine.validate_structured_response(
                representative_id,
                response=response,
                schema_model=TypeISecondBallotAction,
                stage="type_i_second_ballot",
                max_output_tokens=self.max_output_tokens,
                semantic_requirement="Preserve one SUPPORT or OPPOSE vote for every supplied Type I set.",
            )
            self._validate_second_ballot(action, revisions)
            self.repo.docs.write_once(relative, action.model_dump_json(indent=2))
            self.repo.events.append(
                "TYPE_I_SECOND_BALLOT_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "representative_id": representative_id,
                    "record_path": str(relative),
                },
                actor=representative_id,
            )
        self._validate_second_ballot(action, revisions)
        return action

    def _freeze_outcomes(
        self,
        *,
        frozen: dict,
        revisions: dict,
        eligible_count: int,
    ) -> tuple[Path, dict]:
        relative = Path("public/detailed_clauses/type_i_second_ballot_outcomes.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return absolute, json.loads(absolute.read_text(encoding="utf-8"))
        majority = math.floor(eligible_count / 2) + 1
        supermajority = high_threshold(self.repo, eligible_count)
        outcomes = []
        for decision in revisions["decisions"]:
            option_set_id = decision["option_set_id"]
            choices = [
                next(vote for vote in submission["votes"] if vote["option_set_id"] == option_set_id)["choice"]
                for submission in frozen["submissions"]
            ]
            tally = {choice: choices.count(choice) for choice in ("SUPPORT", "OPPOSE")}
            outcome = {
                "option_set_id": option_set_id,
                "option_set_type": "TYPE_I",
                "eligible_count": eligible_count,
                "protective_majority_required": majority,
                "supermajority_required": supermajority,
                "tally": tally,
            }
            if tally["SUPPORT"] >= majority:
                contested = tally["SUPPORT"] < supermajority
                outcome.update(
                    status=(
                        "TYPE_I_ADOPTED_SECOND_ROUND_CONTESTED"
                        if contested
                        else "TYPE_I_ADOPTED_SECOND_ROUND"
                    ),
                    adopted_proposal_id=decision["proposal_id"],
                    contested=contested,
                )
            else:
                outcome.update(
                    status="TYPE_I_REJECTED_SECOND_ROUND",
                    adopted_proposal_id=None,
                    contested=False,
                )
            outcomes.append(outcome)
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_count": eligible_count,
            "protective_majority_formula": "floor(N_ACTIVE/2)+1",
            "protective_majority_required": majority,
            "supermajority_formula": high_threshold_formula(self.repo),
            "supermajority_required": supermajority,
            "outcomes": outcomes,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "TYPE_I_SECOND_BALLOT_OUTCOMES_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "option_set_count": len(outcomes),
                "adopted_count": sum(item.get("adopted_proposal_id") is not None for item in outcomes),
                "rejected_count": sum(item["status"] == "TYPE_I_REJECTED_SECOND_ROUND" for item in outcomes),
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        self.engine.progress.speech(
            "ASSEMBLY",
            "Type I 第二轮表决结果（密封窗口已冻结）",
            json.dumps(record, indent=2, ensure_ascii=False),
        )
        return absolute, record

    def _active_records(self) -> list[dict]:
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        transition = json.loads(
            self.repo.docs.read_text("identity_private/general_principle/status_transition.json")
        )
        return [
            item
            for item in registry
            if transition["representative_statuses"][item["representative_id"]] == "ACTIVE"
        ]

    @staticmethod
    def _run_model_lanes(
        work: list[dict[str, Any]],
        worker: Callable[[dict[str, Any]], _ActionT],
        concurrency_limit: Callable[[str, str], int] | None = None,
    ) -> dict[str, _ActionT]:
        """Parallelize model lanes up to each model's concurrent-request cap."""

        lanes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        persona_order = {persona.value: index for index, persona in enumerate(Persona)}
        for item in work:
            runtime = item["record"]["runtime"]
            lanes.setdefault((runtime["provider_id"], runtime["model_id"]), []).append(item)
        for lane in lanes.values():
            lane.sort(
                key=lambda item: (
                    persona_order.get(
                        item["record"]["runtime"]["persona"], len(persona_order)
                    ),
                    item["participant_id"],
                )
            )

        limit = concurrency_limit or (lambda _provider_id, _model_id: 1)
        completed = run_bounded_model_lanes(
            lanes,
            lambda item: (item["participant_id"], worker(item)),
            limit,
        )
        return dict(completed)

    @staticmethod
    def _validate_revision_action(action: TypeIRevisionAction, items: list[dict]) -> None:
        expected = {item["option_set_id"]: item for item in items}
        actual = [item.option_set_id for item in action.decisions]
        if len(actual) != len(set(actual)) or set(actual) != set(expected):
            raise ValueError("Type I proposer decision must cover every assigned set exactly once")
        for decision in action.decisions:
            source = expected[decision.option_set_id]
            if decision.proposal_id != source["proposal_id"]:
                raise ValueError("Type I proposer decision changed the frozen proposal ID")
            same = decision.final_text.strip() == source["original_text"].strip()
            if decision.action == "RETAIN" and not same:
                raise ValueError("RETAIN must preserve the exact original proposal text")
            if decision.action == "REVISE" and same:
                raise ValueError("REVISE must supply changed proposal text")

    @classmethod
    def _validate_frozen_revisions(cls, record: dict, unresolved: list[dict]) -> None:
        grouped: dict[str, list[dict]] = {}
        for item in unresolved:
            grouped.setdefault(item["proposer_id"], []).append(item)
        decisions = [TypeIRevisionDecision.model_validate(item) for item in record["decisions"]]
        for proposer_id, items in grouped.items():
            option_set_ids = {item["option_set_id"] for item in items}
            cls._validate_revision_action(
                TypeIRevisionAction(
                    decisions=[
                        decision
                        for decision in decisions
                        if decision.option_set_id in option_set_ids
                    ]
                ),
                items,
            )
        expected = {item["option_set_id"] for item in unresolved}
        actual = [item.option_set_id for item in decisions]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("frozen Type I revisions do not cover every unresolved set exactly once")

    @staticmethod
    def _validate_second_ballot(action: TypeISecondBallotAction, revisions: dict) -> None:
        expected = {item["option_set_id"] for item in revisions["decisions"]}
        actual = [item.option_set_id for item in action.votes]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("Type I second ballot must cover every unresolved set exactly once")
