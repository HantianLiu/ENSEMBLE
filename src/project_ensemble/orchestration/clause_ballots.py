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


class ClauseInitialVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_set_id: str = Field(min_length=1)
    choice: str = Field(min_length=1)
    reason: str | None = None


class ClauseInitialBallotAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    votes: list[ClauseInitialVote]


class ClauseInitialBallotResult(BaseModel):
    ballot_count: int
    completed_option_set_count: int
    type_i_direct_adoption_count: int
    type_i_followup_count: int
    type_ii_direct_adoption_count: int
    type_ii_followup_count: int
    type_iii_runoff_count: int
    next_phase: MeetingPhase
    paused_reason: str | None = None


class ClauseInitialBallotRunner:
    """Collect one sealed, complete first-round ballot from every ACTIVE voter."""

    BOUNDARY_REASON = "DETAILED_CLAUSE_FOLLOWUP_BALLOTS_NOT_IMPLEMENTED"
    MAX_OPTION_SETS_PER_CALL = 10
    MAX_OPPOSE_REASON_CHARACTERS = 240

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
        continue_into_runoffs: bool = False,
    ):
        self.repo = repo
        self.engine = engine
        self.max_output_tokens = max_output_tokens
        self.continue_into_runoffs = continue_into_runoffs
        self.resolver = GovernanceDocumentResolver(governance_docs)
        self.assembler = RepresentativeContextAssembler()

    def run(self, *, draft_path: Path, option_sets_path: Path) -> ClauseInitialBallotResult:
        self._ensure_resume_record()
        option_record = json.loads(option_sets_path.read_text(encoding="utf-8"))
        option_sets = option_record["option_sets"]
        active_records = self._active_records()
        self.engine.status.phase = MeetingPhase.BALLOT
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.BALLOT,
            f"收集 {len(active_records)} 名 ACTIVE voter 对 {len(option_sets)} 个 option set 的第一轮密封票；全部完成前不公开票数",
        )
        frozen = self._collect(
            active_records=active_records,
            option_sets=option_sets,
            draft_path=draft_path,
        )
        outcomes = self._freeze_outcomes(
            frozen=frozen,
            option_sets=option_sets,
            eligible_count=len(active_records),
        )
        if self.continue_into_runoffs:
            self.engine.status.phase = MeetingPhase.BALLOT
            self.engine.status.paused_reason = None
            next_phase = MeetingPhase.BALLOT
            paused_reason = None
        else:
            self._pause(outcomes)
            next_phase = MeetingPhase.PAUSED
            paused_reason = self.BOUNDARY_REASON
        statuses = [item["status"] for item in outcomes["outcomes"]]
        return ClauseInitialBallotResult(
            ballot_count=len(option_sets),
            completed_option_set_count=len(option_sets),
            type_i_direct_adoption_count=statuses.count("TYPE_I_ADOPTED_FIRST_ROUND"),
            type_i_followup_count=statuses.count("TYPE_I_SECOND_ROUND_REQUIRED"),
            type_ii_direct_adoption_count=statuses.count("TYPE_II_ADOPTED_FIRST_ROUND"),
            type_ii_followup_count=statuses.count("TYPE_II_EXPLANATION_ROUND_REQUIRED"),
            type_iii_runoff_count=(
                statuses.count("TYPE_III_TOP_TWO_READY")
                + statuses.count("TYPE_III_TOP_TWO_TIE_REQUIRES_HUMAN")
            ),
            next_phase=next_phase,
            paused_reason=paused_reason,
        )

    def _ensure_resume_record(self) -> None:
        """Record the one-time transition from the former implementation boundary."""
        relative = Path("governance_private/detailed_clauses/initial_ballot_resume.json")
        if (self.repo.root / relative).exists():
            return
        record = {
            "meeting_id": self.repo.meeting_id,
            "prior_reason_code": "DETAILED_CLAUSE_BALLOT_EXECUTION_NOT_IMPLEMENTED",
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
        option_sets: list[dict],
        draft_path: Path,
    ) -> dict:
        frozen_relative = Path("governance_private/detailed_clauses/initial_ballots/frozen.json")
        frozen_path = self.repo.root / frozen_relative
        if frozen_path.exists():
            return json.loads(frozen_path.read_text(encoding="utf-8"))
        expected_set_ids = {item["option_set_id"] for item in option_sets}
        ballot_batches = self._load_or_create_ballot_batches(option_sets)
        actions_by_id: dict[str, ClauseInitialBallotAction] = {}
        missing_records = []
        for index, record in enumerate(active_records, start=1):
            representative_id = record["representative_id"]
            relative = (
                Path("governance_private/detailed_clauses/initial_ballots/submissions")
                / f"{representative_id}.json"
            )
            absolute = self.repo.root / relative
            if absolute.exists():
                action = ClauseInitialBallotAction.model_validate_json(
                    absolute.read_text(encoding="utf-8")
                )
                self._validate_action(action, option_sets)
                self.engine.progress.info(
                    f"已恢复 {index}/{len(active_records)} 份完整细则第一轮密封票"
                )
                actions_by_id[representative_id] = action
            else:
                missing_records.append(record)

        def collect_ballot(record: dict) -> tuple[str, ClauseInitialBallotAction, Path]:
            representative_id = record["representative_id"]
            action = self._collect_batched_action(
                record=record,
                batches=ballot_batches,
                draft_path=draft_path,
            )
            relative = (
                Path("governance_private/detailed_clauses/initial_ballots/submissions")
                / f"{representative_id}.json"
            )
            return representative_id, action, relative

        def persist_ballot(result: tuple[str, ClauseInitialBallotAction, Path]) -> None:
            representative_id, action, relative = result
            self.repo.docs.write_once(relative, action.model_dump_json(indent=2))
            self.repo.events.append(
                "DETAILED_CLAUSE_INITIAL_BALLOT_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "representative_id": representative_id,
                    "option_set_count": len(option_sets),
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
            "option_set_ids": sorted(expected_set_ids),
            "submissions": submissions,
        }
        self.repo.docs.write_once(frozen_relative, json.dumps(frozen, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_INITIAL_BALLOT_WINDOW_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "submission_count": len(submissions),
                "option_set_count": len(option_sets),
                "record_path": str(frozen_relative),
            },
            actor="orchestrator",
        )
        return frozen

    def _load_or_create_ballot_batches(self, option_sets: list[dict]) -> list[dict]:
        root = Path("public/detailed_clauses/initial_ballot_batches")
        manifest_relative = root / "manifest.json"
        manifest_path = self.repo.root / manifest_relative
        by_id = {item["option_set_id"]: item for item in option_sets}
        expected_ids = [item["option_set_id"] for item in option_sets]
        created = False
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            batches = []
            for start in range(0, len(option_sets), self.MAX_OPTION_SETS_PER_CALL):
                number = len(batches) + 1
                batch_id = f"IB-{number:03d}"
                members = option_sets[start : start + self.MAX_OPTION_SETS_PER_CALL]
                batch_relative = root / f"{batch_id}.json"
                batch_record = {
                    "meeting_id": self.repo.meeting_id,
                    "status": "FROZEN",
                    "batch_id": batch_id,
                    "option_set_ids": [item["option_set_id"] for item in members],
                    "option_sets": members,
                }
                batch_path = self.repo.root / batch_relative
                if batch_path.exists():
                    if json.loads(batch_path.read_text(encoding="utf-8")) != batch_record:
                        raise ValueError(f"existing initial-ballot batch conflicts: {batch_id}")
                else:
                    self.repo.docs.write_once(
                        batch_relative,
                        json.dumps(batch_record, indent=2, ensure_ascii=False),
                    )
                batches.append(
                    {
                        "batch_id": batch_id,
                        "path": str(batch_relative),
                        "option_set_ids": batch_record["option_set_ids"],
                    }
                )
            manifest = {
                "meeting_id": self.repo.meeting_id,
                "status": "FROZEN",
                "max_option_sets_per_call": self.MAX_OPTION_SETS_PER_CALL,
                "option_set_ids": expected_ids,
                "batches": batches,
            }
            self.repo.docs.write_once(
                manifest_relative,
                json.dumps(manifest, indent=2, ensure_ascii=False),
            )
            created = True

        flattened_ids = [
            option_set_id
            for batch in manifest.get("batches", [])
            for option_set_id in batch.get("option_set_ids", [])
        ]
        if (
            manifest.get("meeting_id") != self.repo.meeting_id
            or manifest.get("status") != "FROZEN"
            or manifest.get("option_set_ids") != expected_ids
            or flattened_ids != expected_ids
            or len(flattened_ids) != len(set(flattened_ids))
        ):
            raise ValueError("frozen initial-ballot batch manifest does not match option sets")

        loaded_batches = []
        for batch in manifest["batches"]:
            expected_path = root / f"{batch['batch_id']}.json"
            if batch.get("path") != str(expected_path):
                raise ValueError(f"invalid frozen initial-ballot batch path {batch['batch_id']}")
            batch_path = self.repo.root / expected_path
            record = json.loads(batch_path.read_text(encoding="utf-8"))
            batch_ids = batch["option_set_ids"]
            if (
                record.get("meeting_id") != self.repo.meeting_id
                or record.get("status") != "FROZEN"
                or record.get("batch_id") != batch["batch_id"]
                or record.get("option_set_ids") != batch_ids
                or record.get("option_sets") != [by_id[item_id] for item_id in batch_ids]
                or len(batch_ids) > self.MAX_OPTION_SETS_PER_CALL
            ):
                raise ValueError(f"invalid frozen initial-ballot batch {batch['batch_id']}")
            loaded_batches.append({**record, "path": str(expected_path)})

        if created:
            self.repo.events.append(
                "DETAILED_CLAUSE_INITIAL_BALLOT_BATCHES_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "batch_count": len(loaded_batches),
                    "option_set_count": len(option_sets),
                    "max_option_sets_per_call": self.MAX_OPTION_SETS_PER_CALL,
                    "record_path": str(manifest_relative),
                },
                actor="orchestrator",
            )
        return loaded_batches

    def _collect_batched_action(
        self,
        *,
        record: dict,
        batches: list[dict],
        draft_path: Path,
    ) -> ClauseInitialBallotAction:
        representative_id = record["representative_id"]
        votes: list[ClauseInitialVote] = []
        for index, batch in enumerate(batches, start=1):
            partial_relative = (
                Path("governance_private/detailed_clauses/initial_ballots/partials")
                / representative_id
                / f"{batch['batch_id']}.json"
            )
            partial_path = self.repo.root / partial_relative
            if partial_path.exists():
                action = ClauseInitialBallotAction.model_validate_json(
                    partial_path.read_text(encoding="utf-8")
                )
                self._validate_action(action, batch["option_sets"])
                self._validate_concise_reasons(action, batch["option_sets"])
                self.engine.progress.info(
                    f"{representative_id} · 已恢复密封票分片 {index}/{len(batches)}"
                )
            else:
                self.engine.progress.info(
                    f"{representative_id} · 正在填写密封票分片 {index}/{len(batches)}；"
                    f"本片 {len(batch['option_sets'])} 项"
                )
                spec = self.resolver.representative_context_spec(
                    persona=Persona(record["runtime"]["persona"]),
                    stage="ballot",
                    representative_id=representative_id,
                    public_state_files=(draft_path, self.repo.root / batch["path"]),
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
                        "Cast one sealed ballot fragment covering every supplied option_set_id exactly once. "
                        "Return exactly one JSON object with key votes; each vote has option_set_id, choice, "
                        "and reason. For TYPE_I choose SUPPORT or OPPOSE. Set reason to null for SUPPORT. "
                        f"For OPPOSE, give exactly one concise sentence of at most "
                        f"{self.MAX_OPPOSE_REASON_CHARACTERS} Unicode characters identifying only the decisive "
                        "defect. For TYPE_II or TYPE_III choose exactly one proposal_id and set reason to null. "
                        "Do not restate proposals, debate procedure, add background, abstain, add keys, or use "
                        "Markdown. Required option_set_ids: "
                        + json.dumps(batch["option_set_ids"], ensure_ascii=False)
                    ),
                    stage="detailed_clause_initial_ballot",
                    max_output_tokens=self.max_output_tokens,
                )
                try:
                    action = ClauseInitialBallotAction.model_validate(json.loads(response.text))
                    self._validate_action(action, batch["option_sets"])
                    self._validate_concise_reasons(action, batch["option_sets"])
                except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                    self.engine.pause_for_unconfigured_policy(
                        participant_id=representative_id,
                        reason_code="SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED",
                    )
                self.repo.docs.write_once(partial_relative, action.model_dump_json(indent=2))
                self.repo.events.append(
                    "DETAILED_CLAUSE_INITIAL_BALLOT_PARTIAL_SUBMITTED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "representative_id": representative_id,
                        "batch_id": batch["batch_id"],
                        "batch_number": index,
                        "batch_count": len(batches),
                        "option_set_count": len(batch["option_sets"]),
                        "record_path": str(partial_relative),
                    },
                    actor=representative_id,
                )
            votes.extend(action.votes)

        merged = ClauseInitialBallotAction(votes=votes)
        all_option_sets = [item for batch in batches for item in batch["option_sets"]]
        self._validate_action(merged, all_option_sets)
        self._validate_concise_reasons(merged, all_option_sets)
        return merged

    @staticmethod
    def _validate_action(action: ClauseInitialBallotAction, option_sets: list[dict]) -> None:
        by_id = {item["option_set_id"]: item for item in option_sets}
        actual = [vote.option_set_id for vote in action.votes]
        if len(actual) != len(set(actual)) or set(actual) != set(by_id):
            raise ValueError("ballot must cover every option set exactly once")
        for vote in action.votes:
            option_set = by_id[vote.option_set_id]
            if option_set["option_set_type"] == "TYPE_I":
                if vote.choice not in {"SUPPORT", "OPPOSE"}:
                    raise ValueError("Type I choice must be SUPPORT or OPPOSE")
                if vote.choice == "OPPOSE" and not (vote.reason or "").strip():
                    raise ValueError("Type I OPPOSE requires a reason")
            elif vote.choice not in set(option_set["proposal_ids"]):
                raise ValueError("multi-option choice must reference one proposal in its set")

    @classmethod
    def _validate_concise_reasons(
        cls,
        action: ClauseInitialBallotAction,
        option_sets: list[dict],
    ) -> None:
        by_id = {item["option_set_id"]: item for item in option_sets}
        for vote in action.votes:
            reason = (vote.reason or "").strip()
            requires_reason = (
                by_id[vote.option_set_id]["option_set_type"] == "TYPE_I"
                and vote.choice == "OPPOSE"
            )
            if requires_reason:
                if len(reason) > cls.MAX_OPPOSE_REASON_CHARACTERS:
                    raise ValueError(
                        "Type I OPPOSE reason exceeds the concise-reason character limit"
                    )
            elif reason:
                raise ValueError("ballot reason must be null unless the choice is Type I OPPOSE")

    def _freeze_outcomes(
        self,
        *,
        frozen: dict,
        option_sets: list[dict],
        eligible_count: int,
    ) -> dict:
        relative = Path("public/detailed_clauses/initial_ballot_outcomes.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return json.loads(absolute.read_text(encoding="utf-8"))
        supermajority = high_threshold(self.repo, eligible_count)
        outcomes = []
        for option_set in option_sets:
            option_set_id = option_set["option_set_id"]
            votes = [
                next(item for item in submission["votes"] if item["option_set_id"] == option_set_id)
                for submission in frozen["submissions"]
            ]
            legal_choices = (
                ["SUPPORT", "OPPOSE"]
                if option_set["option_set_type"] == "TYPE_I"
                else option_set["proposal_ids"]
            )
            tally = {choice: sum(vote["choice"] == choice for vote in votes) for choice in legal_choices}
            outcome = {
                "option_set_id": option_set_id,
                "option_set_type": option_set["option_set_type"],
                "eligible_count": eligible_count,
                "supermajority_required": supermajority,
                "tally": tally,
            }
            if option_set["option_set_type"] == "TYPE_I":
                if tally["SUPPORT"] >= supermajority:
                    outcome.update(
                        status="TYPE_I_ADOPTED_FIRST_ROUND",
                        adopted_proposal_id=option_set["proposal_ids"][0],
                    )
                else:
                    outcome.update(status="TYPE_I_SECOND_ROUND_REQUIRED")
            elif option_set["option_set_type"] == "TYPE_II":
                winner = max(legal_choices, key=lambda choice: tally[choice])
                if tally[winner] >= supermajority:
                    outcome.update(status="TYPE_II_ADOPTED_FIRST_ROUND", adopted_proposal_id=winner)
                else:
                    outcome.update(status="TYPE_II_EXPLANATION_ROUND_REQUIRED")
            else:
                ordered = sorted(legal_choices, key=lambda choice: (-tally[choice], choice))
                cutoff = tally[ordered[1]]
                tied_at_cutoff = [choice for choice in ordered if tally[choice] == cutoff]
                if len([choice for choice in ordered if tally[choice] > cutoff]) + len(tied_at_cutoff) > 2:
                    outcome.update(
                        status="TYPE_III_TOP_TWO_TIE_REQUIRES_HUMAN",
                        tied_proposal_ids=tied_at_cutoff,
                    )
                else:
                    outcome.update(status="TYPE_III_TOP_TWO_READY", top_two_proposal_ids=ordered[:2])
            outcomes.append(outcome)
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "eligible_count": eligible_count,
            "supermajority_formula": high_threshold_formula(self.repo),
            "supermajority_required": supermajority,
            "outcomes": outcomes,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "DETAILED_CLAUSE_INITIAL_BALLOT_OUTCOMES_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "eligible_count": eligible_count,
                "supermajority_required": supermajority,
                "option_set_count": len(outcomes),
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        self.engine.progress.speech(
            "ASSEMBLY",
            "细则第一轮表决结果（密封窗口已冻结）",
            json.dumps(record, indent=2, ensure_ascii=False),
        )
        return record

    def _pause(self, outcomes: dict) -> None:
        relative = Path(
            "governance_private/detailed_clauses/pause_detailed_clause_followup_ballots_not_implemented.json"
        )
        if not (self.repo.root / relative).exists():
            self.repo.docs.write_once(
                relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "reason_code": self.BOUNDARY_REASON,
                        "initial_outcomes_path": "public/detailed_clauses/initial_ballot_outcomes.json",
                    },
                    indent=2,
                ),
            )
            self.repo.events.append(
                "MEETING_PAUSED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "reason_code": self.BOUNDARY_REASON,
                    "record_path": str(relative),
                },
                actor="orchestrator",
            )
        self.engine.status.phase = MeetingPhase.PAUSED
        self.engine.status.paused_reason = self.BOUNDARY_REASON
        self.engine.progress.status(
            MeetingPhase.PAUSED,
            f"{self.BOUNDARY_REASON} · 第一轮结果已冻结；下一步执行 Type I 第二轮、Type II 解释轮及 Type III top-two runoff",
        )
