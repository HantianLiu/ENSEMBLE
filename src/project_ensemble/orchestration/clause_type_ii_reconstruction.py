from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.governance_private.thresholds import high_threshold
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.runtime.model_replacements import current_runtime_for
from project_ensemble.storage.meeting import MeetingRepository


class NewOptionReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    proposal_id: str
    decision: Literal["QUALIFY", "EXCLUDE", "REWRITE_REQUIRED"]
    normalized_text: str | None = None
    meaning_changed: bool = False
    reason_code: Literal[
        "QUALIFIED",
        "SUBSTANTIVE_DUPLICATE",
        "OUT_OF_SCOPE",
        "NOT_EXECUTABLE_TEXT",
        "MULTIPLE_INSEPARABLE_OPTIONS",
        "NORMALIZATION_CHANGED_MEANING",
    ]
    reason: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def disposition_contract(self) -> "NewOptionReviewItem":
        if self.decision == "QUALIFY" and not self.normalized_text:
            raise ValueError("QUALIFY requires normalized_text")
        if self.decision != "QUALIFY" and self.reason_code == "QUALIFIED":
            raise ValueError("only QUALIFY may use QUALIFIED")
        return self


class NewOptionReviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviews: list[NewOptionReviewItem]


class NewOptionProposerDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["ACCEPT_NORMALIZATION", "REVISE", "OBJECT_EXCLUSION", "WITHDRAW"]
    revised_text: str | None = None

    @model_validator(mode="after")
    def revision_contract(self) -> "NewOptionProposerDisposition":
        if (self.action == "REVISE") != (self.revised_text is not None):
            raise ValueError("REVISE requires revised_text and other actions forbid it")
        return self


class NewOptionObjectionVote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    proposal_id: str
    choice: Literal["RESTORE", "UPHOLD_EXCLUSION"]


class NewOptionObjectionBallot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    votes: list[NewOptionObjectionVote]


class ReconstructedChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_set_id: str
    choice: str


class ReconstructedBallot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    votes: list[ReconstructedChoice]


class ReconstructedExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_set_id: str
    preferred_proposal_id: str
    explanation: str = Field(min_length=1, max_length=1200)


class ReconstructedExplanationSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanations: list[ReconstructedExplanation]


class ChairTieDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate_id: str
    reason: str = Field(min_length=1, max_length=500)


class TypeIINewOptionResult(BaseModel):
    effective_option_sets_path: str
    outcomes_path: str
    qualified_new_option_count: int
    reconstructed_option_set_count: int


class TypeIINewOptionRunner:
    """Qualify Type II new options and finish each affected set without recursion."""

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        max_output_tokens: int | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.max_output_tokens = max_output_tokens

    def run(
        self,
        *,
        draft_path: Path,
        option_sets_path: Path,
        unresolved: list[dict],
        active_records: list[dict],
        frozen_second_ballots: dict,
        new_options: list[dict],
    ) -> TypeIINewOptionResult:
        outcome_relative = Path(
            "public/detailed_clauses/type_ii_second_ballot_outcomes.json"
        )
        effective_relative = Path(
            "public/detailed_clauses/option_sets_reconstructed.json"
        )
        outcome_path = self.repo.root / outcome_relative
        effective_path = self.repo.root / effective_relative
        if outcome_path.exists() and effective_path.exists():
            effective_record = json.loads(effective_path.read_text(encoding="utf-8"))
            qualified_path = (
                self.repo.root
                / "public/detailed_clauses/type_ii_qualified_new_options.json"
            )
            qualified_count = 0
            if qualified_path.exists():
                qualified_count = int(
                    json.loads(qualified_path.read_text(encoding="utf-8")).get(
                        "qualified_option_count", 0
                    )
                )
            return TypeIINewOptionResult(
                effective_option_sets_path=str(effective_relative),
                outcomes_path=str(outcome_relative),
                qualified_new_option_count=qualified_count,
                reconstructed_option_set_count=len(
                    effective_record.get("reconstructed_option_sets", [])
                ),
            )

        qualified = self._qualify(
            draft_path=draft_path,
            active_records=active_records,
            new_options=new_options,
        )
        effective_path, reconstructed = self._freeze_effective_option_sets(
            draft_path=draft_path,
            option_sets_path=option_sets_path,
            unresolved=unresolved,
            qualified=qualified,
            raw_affected_ids={item["option_set_id"] for item in new_options},
        )
        outcomes = []
        reconstructed_by_id = {item["option_set_id"]: item for item in reconstructed}
        affected_ids = set(reconstructed_by_id)
        for option_set in unresolved:
            if option_set["option_set_id"] not in affected_ids:
                outcomes.append(
                    self._existing_second_ballot_outcome(
                        option_set=option_set,
                        frozen=frozen_second_ballots,
                        eligible_count=len(active_records),
                    )
                )
        if reconstructed:
            outcomes.extend(
                self._run_reconstructed_ballots(
                    draft_path=draft_path,
                    active_records=active_records,
                    option_sets=reconstructed,
                )
            )
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "policy_version": "TYPE_II_NEW_OPTION_RECONSTRUCTION_V1",
            "eligible_count": len(active_records),
            "outcomes": sorted(outcomes, key=lambda item: item["option_set_id"]),
        }
        self.repo.docs.write_once(
            outcome_relative, json.dumps(record, indent=2, ensure_ascii=False)
        )
        self.repo.events.append(
            "TYPE_II_RECONSTRUCTED_OUTCOMES_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "option_set_count": len(outcomes),
                "qualified_new_option_count": len(qualified),
                "record_path": str(outcome_relative),
            },
            actor="orchestrator",
        )
        return TypeIINewOptionResult(
            effective_option_sets_path=str(effective_path.relative_to(self.repo.root)),
            outcomes_path=str(outcome_relative),
            qualified_new_option_count=len(qualified),
            reconstructed_option_set_count=len(reconstructed),
        )

    def _qualify(
        self, *, draft_path: Path, active_records: list[dict], new_options: list[dict]
    ) -> list[dict]:
        audit_relative = Path(
            "governance_private/detailed_clauses/type_ii_reconstruction/qualification.json"
        )
        audit_path = self.repo.root / audit_relative
        public_relative = Path(
            "public/detailed_clauses/type_ii_qualified_new_options.json"
        )
        if audit_path.exists():
            frozen = json.loads(audit_path.read_text(encoding="utf-8"))
            return frozen["qualified_options"]

        plan = self._chair_review(
            stage="chair_type_ii_new_option_qualification",
            draft_path=draft_path,
            options=new_options,
            second_attempt=False,
        )
        by_id = {item["proposal_id"]: item for item in new_options}
        self._validate_review_plan(plan, set(by_id))
        qualified: list[dict] = []
        excluded: list[dict] = []
        trajectories: list[dict] = []
        record_by_rid = {item["representative_id"]: item for item in active_records}
        for review in plan.reviews:
            source = by_id[review.proposal_id]
            trajectory = {
                "proposal_id": review.proposal_id,
                "first_review": review.model_dump(mode="json"),
            }
            if review.decision == "QUALIFY" and not review.meaning_changed:
                qualified.append({**source, "text": review.normalized_text})
                trajectory["final"] = "QUALIFIED"
                trajectories.append(trajectory)
                continue

            disposition = self._proposer_disposition(
                record=record_by_rid[source["proposer_id"]],
                source=source,
                review=review,
                draft_path=draft_path,
            )
            trajectory["proposer_disposition"] = disposition.model_dump(mode="json")
            if disposition.action == "ACCEPT_NORMALIZATION" and review.decision == "QUALIFY":
                qualified.append({**source, "text": review.normalized_text})
                trajectory["final"] = "QUALIFIED"
            elif disposition.action == "REVISE":
                revised = {**source, "text": disposition.revised_text}
                second = self._chair_review(
                    stage=f"chair_type_ii_new_option_second_review_{review.proposal_id}",
                    draft_path=draft_path,
                    options=[revised],
                    second_attempt=True,
                ).reviews[0]
                self._validate_review_plan(
                    NewOptionReviewPlan(reviews=[second]), {review.proposal_id}
                )
                trajectory["second_review"] = second.model_dump(mode="json")
                if second.decision == "QUALIFY" and not second.meaning_changed:
                    qualified.append({**revised, "text": second.normalized_text})
                    trajectory["final"] = "QUALIFIED_AFTER_REVISION"
                else:
                    trajectory["final"] = "WITHDRAWN_AFTER_SECOND_FAILURE"
            elif disposition.action == "OBJECT_EXCLUSION" and review.decision == "EXCLUDE":
                excluded.append({"source": source, "review": review.model_dump(mode="json")})
                trajectory["final"] = "OBJECTION_PENDING"
            else:
                trajectory["final"] = "WITHDRAWN"
            trajectories.append(trajectory)

        if excluded:
            restored = self._vote_on_exclusions(
                draft_path=draft_path,
                active_records=active_records,
                exclusions=excluded,
            )
            for item in excluded:
                if item["source"]["proposal_id"] in restored:
                    qualified.append(item["source"])
            for trajectory in trajectories:
                if trajectory["proposal_id"] in restored:
                    trajectory["final"] = "RESTORED_BY_ABSOLUTE_MAJORITY"
                elif trajectory["final"] == "OBJECTION_PENDING":
                    trajectory["final"] = "EXCLUDED_AFTER_OBJECTION_VOTE"

        audit = {
            "meeting_id": self.repo.meeting_id,
            "policy_version": "TYPE_II_NEW_OPTION_RECONSTRUCTION_V1",
            "raw_option_count": len(new_options),
            "qualified_options": qualified,
            "trajectories": trajectories,
        }
        self.repo.docs.write_once(
            audit_relative, json.dumps(audit, indent=2, ensure_ascii=False)
        )
        public = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "qualified_option_count": len(qualified),
            "options": [
                {key: value for key, value in item.items() if key != "proposer_id"}
                for item in qualified
            ],
        }
        self.repo.docs.write_once(
            public_relative, json.dumps(public, indent=2, ensure_ascii=False)
        )
        return qualified

    def _chair_review(
        self,
        *,
        stage: str,
        draft_path: Path,
        options: list[dict],
        second_attempt: bool,
    ) -> NewOptionReviewPlan:
        allowed = (
            "On this second and final review, QUALIFY only if the revised text is executable and "
            "preserves the proposer's meaning; otherwise EXCLUDE."
            if second_attempt
            else "Use only mechanical eligibility criteria: substantive duplicate, out of scope, "
            "not executable as text, or multiple inseparable options. Do not judge merit."
        )
        system = "Task: perform only mechanical Type II new-option qualification under Chair authority."
        user = (
            allowed
            + " Return one review for every proposal_id in order. normalized_text may make only "
            "formatting, numbering, grammar, and obvious typo corrections. Set meaning_changed=true "
            "whenever the normalization may alter substance. Do not add keys or Markdown.\n\n"
            "STANDING TEXT:\n"
            + draft_path.read_text(encoding="utf-8")
            + "\n\nSEALED NEW OPTIONS:\n"
            + json.dumps(options, indent=2, ensure_ascii=False)
        )
        disposition = self._invoke(
            "CHAIR", stage=stage, schema=NewOptionReviewPlan, system=system, user=user
        )
        return disposition

    def _proposer_disposition(
        self, *, record: dict, source: dict, review: NewOptionReviewItem, draft_path: Path
    ) -> NewOptionProposerDisposition:
        rid = record["representative_id"]
        allowed = {
            "QUALIFY": ["ACCEPT_NORMALIZATION", "REVISE", "WITHDRAW"],
            "REWRITE_REQUIRED": ["REVISE", "WITHDRAW"],
            "EXCLUDE": ["OBJECT_EXCLUSION", "WITHDRAW"],
        }[review.decision]
        disposition = self._invoke(
            rid,
            stage=f"type_ii_new_option_proposer_disposition_{source['proposal_id']}",
            schema=NewOptionProposerDisposition,
            system="Task: respond only for your own sealed new option.",
            user=(
                "Choose one allowed action: "
                + json.dumps(allowed)
                + ". You receive at most one revised-text attempt. REVISE must include a complete "
                "revised_text; other actions must use null.\n\nYOUR OPTION:\n"
                + json.dumps(source, indent=2, ensure_ascii=False)
                + "\n\nCHAIR MECHANICAL REVIEW:\n"
                + review.model_dump_json(indent=2)
                + "\n\nSTANDING TEXT:\n"
                + draft_path.read_text(encoding="utf-8")
            ),
        )
        if disposition.action not in allowed:
            raise ValueError("proposer selected an action unavailable for this disposition")
        return disposition

    def _vote_on_exclusions(
        self, *, draft_path: Path, active_records: list[dict], exclusions: list[dict]
    ) -> set[str]:
        proposal_ids = [item["source"]["proposal_id"] for item in exclusions]
        ballots = self._collect(
            records=active_records,
            stage="type_ii_new_option_exclusion_objections",
            schema=NewOptionObjectionBallot,
            root=Path(
                "governance_private/detailed_clauses/type_ii_reconstruction/exclusion_votes"
            ),
            system="Task: vote on mechanical eligibility, not substantive merit.",
            user=(
                "Vote RESTORE or UPHOLD_EXCLUSION for every proposal_id exactly once. RESTORE only "
                "when the Chair's stated mechanical exclusion is inapplicable. No abstention or reason."
                "\n\nEXCLUSIONS:\n"
                + json.dumps(exclusions, indent=2, ensure_ascii=False)
                + "\n\nSTANDING TEXT:\n"
                + draft_path.read_text(encoding="utf-8")
            ),
            validator=lambda action: self._validate_objection_ballot(action, proposal_ids),
        )
        required = math.floor(len(active_records) / 2) + 1
        return {
            proposal_id
            for proposal_id in proposal_ids
            if sum(
                next(v for v in ballot.votes if v.proposal_id == proposal_id).choice
                == "RESTORE"
                for ballot in ballots.values()
            )
            >= required
        }

    def _freeze_effective_option_sets(
        self,
        *,
        draft_path: Path,
        option_sets_path: Path,
        unresolved: list[dict],
        qualified: list[dict],
        raw_affected_ids: set[str],
    ) -> tuple[Path, list[dict]]:
        relative = Path("public/detailed_clauses/option_sets_reconstructed.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            record = json.loads(absolute.read_text(encoding="utf-8"))
            return absolute, record["reconstructed_option_sets"]
        source = json.loads(option_sets_path.read_text(encoding="utf-8"))
        qualified_by_set: dict[str, list[dict]] = {}
        for option in qualified:
            qualified_by_set.setdefault(option["option_set_id"], []).append(option)
        unresolved_by_id = {item["option_set_id"]: item for item in unresolved}
        source_ids = {item["option_set_id"] for item in source["option_sets"]}
        if not raw_affected_ids <= set(unresolved_by_id) or not raw_affected_ids <= source_ids:
            raise ValueError("new Type II option refers to an unknown or resolved option set")
        reconstructed = []
        effective = []
        for option_set in source["option_sets"]:
            option_set_id = option_set["option_set_id"]
            if option_set_id not in raw_affected_ids:
                effective.append(option_set)
                continue
            new_items = [
                {
                    "proposal_id": item["proposal_id"],
                    "target_clause_id": option_set["target_clause_id"],
                    "proposal_type": "NEW_OPTION",
                    "text": item["text"],
                    "reason": item["reason"],
                    "substantive_gain": item["substantive_gain"],
                }
                for item in qualified_by_set.get(option_set_id, [])
            ]
            original_finalists = unresolved_by_id[option_set_id]["proposal_ids"]
            original_options = [
                item
                for item in option_set["options"]
                if item["proposal_id"] in original_finalists
            ]
            if new_items:
                status_quo_id = f"STATUS_QUO-{option_set_id}"
                status_quo = {
                    "proposal_id": status_quo_id,
                    "target_clause_id": option_set["target_clause_id"],
                    "proposal_type": "REPLACE",
                    "text": self._extract_clause(
                        draft_path.read_text(encoding="utf-8"), option_set["target_clause_id"]
                    ),
                    "reason": "Retain the frozen standing clause.",
                    "status_quo": True,
                }
                rebuilt = {
                    **option_set,
                    "option_set_type": "TYPE_III",
                    "proposal_ids": [
                        *original_finalists,
                        *[item["proposal_id"] for item in new_items],
                        status_quo_id,
                    ],
                    "options": [*original_options, *new_items, status_quo],
                    "recursive_new_options_allowed": False,
                }
            else:
                rebuilt = {
                    **option_set,
                    "option_set_type": "TYPE_II",
                    "proposal_ids": original_finalists,
                    "options": original_options,
                    "reballot_reason": "ALL_NEW_OPTIONS_WITHDRAWN_OR_EXCLUDED",
                    "recursive_new_options_allowed": False,
                }
            effective.append(rebuilt)
            reconstructed.append(rebuilt)
        record = {
            **source,
            "source_option_sets_path": str(option_sets_path.relative_to(self.repo.root)),
            "reconstruction_policy": "TYPE_II_NEW_OPTION_RECONSTRUCTION_V1",
            "option_sets": effective,
            "reconstructed_option_sets": reconstructed,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        return absolute, reconstructed

    def _run_reconstructed_ballots(
        self, *, draft_path: Path, active_records: list[dict], option_sets: list[dict]
    ) -> list[dict]:
        first = self._collect_choice_ballots(
            stage="type_ii_reconstructed_multi_option_ballot",
            root=Path("governance_private/detailed_clauses/type_ii_reconstruction/first_ballots"),
            records=active_records,
            option_sets=option_sets,
            draft_path=draft_path,
        )
        required = high_threshold(self.repo, len(active_records))
        outcomes = []
        multi_sets = []
        for option_set in option_sets:
            option_set_id = option_set["option_set_id"]
            tally = self._tally(first.values(), option_set_id, option_set["proposal_ids"])
            winner = self._winner_or_chair(option_set_id, tally)
            if len(option_set["proposal_ids"]) > 2 and tally[winner] < required:
                multi_sets.append(option_set)
                continue
            outcomes.append(
                self._terminal_outcome(
                    option_set, tally, winner, required, tally[winner] < required
                )
            )
        if not multi_sets:
            return outcomes
        finalists: dict[str, list[str]] = {}
        for option_set in multi_sets:
            candidates = option_set["proposal_ids"]
            tally = self._tally(first.values(), option_set["option_set_id"], candidates)
            finalists[option_set["option_set_id"]] = self._top_two(
                option_set_id=option_set["option_set_id"],
                candidates=candidates,
                tally=tally,
                records=active_records,
                draft_path=draft_path,
            )
        runoff_sets = [
            {
                **option_set,
                "proposal_ids": finalists[option_set["option_set_id"]],
                "options": [
                    item
                    for item in option_set["options"]
                    if item["proposal_id"] in finalists[option_set["option_set_id"]]
                ],
            }
            for option_set in multi_sets
        ]
        runoff = self._collect_choice_ballots(
            stage="type_ii_reconstructed_top_two_runoff",
            root=Path("governance_private/detailed_clauses/type_ii_reconstruction/runoff_ballots"),
            records=active_records,
            option_sets=runoff_sets,
            draft_path=draft_path,
        )
        followup = []
        for option_set in runoff_sets:
            option_set_id = option_set["option_set_id"]
            tally = self._tally(runoff.values(), option_set_id, option_set["proposal_ids"])
            winner = self._winner_or_chair(option_set_id, tally)
            if tally[winner] >= required:
                outcomes.append(self._terminal_outcome(option_set, tally, winner, required, False))
            else:
                followup.append(option_set)
        if followup:
            self._collect_explanations(
                records=active_records, option_sets=followup, draft_path=draft_path
            )
            final = self._collect_choice_ballots(
                stage="type_ii_reconstructed_final_binary",
                root=Path("governance_private/detailed_clauses/type_ii_reconstruction/final_ballots"),
                records=active_records,
                option_sets=followup,
                draft_path=draft_path,
            )
            for option_set in followup:
                option_set_id = option_set["option_set_id"]
                tally = self._tally(final.values(), option_set_id, option_set["proposal_ids"])
                winner = self._winner_or_chair(option_set_id, tally)
                outcomes.append(self._terminal_outcome(option_set, tally, winner, required, True))
        return outcomes

    def _top_two(
        self,
        *,
        option_set_id: str,
        candidates: list[str],
        tally: dict[str, int],
        records: list[dict],
        draft_path: Path,
    ) -> list[str]:
        ordered = sorted(candidates, key=lambda item: (-tally[item], item))
        cutoff = tally[ordered[1]]
        secured = [item for item in ordered if tally[item] > cutoff]
        tied = [item for item in ordered if tally[item] == cutoff]
        seats = 2 - len(secured)
        if len(tied) <= seats:
            return [*secured, *tied]
        tie_set = [{"option_set_id": option_set_id, "proposal_ids": tied, "options": []}]
        ballots = self._collect_choice_ballots(
            stage=f"type_ii_reconstructed_cutoff_{option_set_id}",
            root=Path("governance_private/detailed_clauses/type_ii_reconstruction/cutoff_ballots")
            / option_set_id,
            records=records,
            option_sets=tie_set,
            draft_path=draft_path,
        )
        tie_tally = self._tally(ballots.values(), option_set_id, tied)
        tie_ordered = sorted(tied, key=lambda item: (-tie_tally[item], item))
        boundary = tie_tally[tie_ordered[seats - 1]]
        selected = [item for item in tie_ordered if tie_tally[item] > boundary]
        boundary_tied = [item for item in tie_ordered if tie_tally[item] == boundary]
        while len(selected) < seats:
            remaining = seats - len(selected)
            if len(boundary_tied) <= remaining:
                selected.extend(boundary_tied)
                break
            decision = self._chair_tie(option_set_id, boundary_tied, len(selected) + 1)
            selected.append(decision.candidate_id)
            boundary_tied.remove(decision.candidate_id)
        return [*secured, *selected]

    def _collect_choice_ballots(
        self,
        *,
        stage: str,
        root: Path,
        records: list[dict],
        option_sets: list[dict],
        draft_path: Path,
    ) -> dict[str, ReconstructedBallot]:
        legal = {item["option_set_id"]: item["proposal_ids"] for item in option_sets}
        return self._collect(
            records=records,
            stage=stage,
            schema=ReconstructedBallot,
            root=root,
            system="Task: cast a sealed clause ballot. Do not abstain or add reasons.",
            user=(
                "Choose exactly one legal proposal_id for every option_set_id. Return only votes. "
                "No new option is permitted in this reconstructed procedure.\n\nOPTION SETS:\n"
                + json.dumps(option_sets, indent=2, ensure_ascii=False)
                + "\n\nSTANDING TEXT:\n"
                + draft_path.read_text(encoding="utf-8")
            ),
            validator=lambda action: self._validate_choice_ballot(action, legal),
        )

    def _collect_explanations(
        self, *, records: list[dict], option_sets: list[dict], draft_path: Path
    ) -> None:
        legal = {item["option_set_id"]: item["proposal_ids"] for item in option_sets}
        results = self._collect(
            records=records,
            stage="type_ii_reconstructed_explanations",
            schema=ReconstructedExplanationSet,
            root=Path("governance_private/detailed_clauses/type_ii_reconstruction/explanations"),
            system="Explain your current preference concisely; this is not a ballot.",
            user=(
                "Give one explanation for every option_set_id and name one preferred_proposal_id. "
                "No new option may be introduced.\n\nOPTION SETS:\n"
                + json.dumps(option_sets, indent=2, ensure_ascii=False)
                + "\n\nSTANDING TEXT:\n"
                + draft_path.read_text(encoding="utf-8")
            ),
            validator=lambda action: self._validate_explanations(action, legal),
        )
        relative = Path("public/detailed_clauses/type_ii_reconstructed_explanations.json")
        if not (self.repo.root / relative).exists():
            self.repo.docs.write_once(
                relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "status": "FROZEN",
                        "submissions": [
                            {
                                "representative_id": rid,
                                "explanations": action.model_dump(mode="json")["explanations"],
                            }
                            for rid, action in results.items()
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )

    def _collect(
        self,
        *,
        records: list[dict],
        stage: str,
        schema: type[BaseModel],
        root: Path,
        system: str,
        user: str,
        validator,
    ) -> dict:
        complete = {}
        missing = []
        for record in records:
            rid = record["representative_id"]
            path = self.repo.root / root / f"{rid}.json"
            if path.exists():
                action = schema.model_validate_json(path.read_text(encoding="utf-8"))
                validator(action)
                complete[rid] = action
            else:
                missing.append(record)

        def call(record):
            rid = record["representative_id"]
            action = self._invoke(rid, stage=stage, schema=schema, system=system, user=user)
            validator(action)
            return rid, action

        def persist(result):
            rid, action = result
            self.repo.docs.write_once(root / f"{rid}.json", action.model_dump_json(indent=2))

        results = run_bounded_representative_lanes(
            missing,
            call,
            self.engine.model_concurrency_limit,
            on_result=persist,
            progress=self.engine.progress,
            progress_records=records,
            completed_participant_ids=complete,
            runtime_key=lambda record: current_runtime_for(
                self.repo, str(record["representative_id"])
            ),
        )
        complete.update({rid: action for rid, action in results})
        return complete

    def _invoke(self, participant_id: str, *, stage: str, schema, system: str, user: str):
        response = self.engine.find_recorded_response(
            participant_id, system_text=system, user_text=user, stage=stage
        )
        if response is None:
            response = self.engine.invoke_participant(
                participant_id,
                system_text=system,
                user_text=user,
                stage=stage,
                max_output_tokens=self.max_output_tokens,
            )
        return self.engine.validate_structured_response(
            participant_id,
            response=response,
            schema_model=schema,
            stage=stage,
            max_output_tokens=self.max_output_tokens,
        )

    def _chair_tie(
        self, option_set_id: str, candidates: list[str], seat_number: int = 1
    ) -> ChairTieDecision:
        decision = self._invoke(
            "CHAIR",
            stage=f"chair_type_ii_reconstruction_tie_{option_set_id}_{seat_number}",
            schema=ChairTieDecision,
            system="Cast one public Chair deciding vote after a tied Assembly runoff.",
            user=(
                "Choose one candidate_id and give one concise public reason. Candidates: "
                + json.dumps(candidates, ensure_ascii=False)
            ),
        )
        if decision.candidate_id not in candidates:
            raise ValueError("Chair selected an ineligible tied option")
        return decision

    def _winner_or_chair(self, option_set_id: str, tally: dict[str, int]) -> str:
        high = max(tally.values())
        leaders = [candidate for candidate, count in tally.items() if count == high]
        return (
            leaders[0]
            if len(leaders) == 1
            else self._chair_tie(option_set_id, leaders).candidate_id
        )

    @staticmethod
    def _terminal_outcome(
        option_set: dict,
        tally: dict[str, int],
        winner: str,
        required: int,
        contested: bool,
    ) -> dict:
        status_quo = winner.startswith("STATUS_QUO-")
        return {
            "option_set_id": option_set["option_set_id"],
            "eligible_count": sum(tally.values()),
            "supermajority_required": required,
            "tally": tally,
            "adopted_proposal_id": None if status_quo else winner,
            "contested": contested,
            "status": (
                "STATUS_QUO_RETAINED"
                if status_quo
                else "TYPE_II_RECONSTRUCTED_ADOPTED_CONTESTED"
                if contested
                else "TYPE_II_RECONSTRUCTED_ADOPTED"
            ),
        }

    def _existing_second_ballot_outcome(
        self,
        *, option_set: dict, frozen: dict, eligible_count: int
    ) -> dict:
        option_set_id = option_set["option_set_id"]
        choices = option_set["proposal_ids"]
        votes = [
            next(v for v in submission["votes"] if v["option_set_id"] == option_set_id)
            for submission in frozen["submissions"]
        ]
        tally = {choice: sum(vote["choice"] == choice for vote in votes) for choice in choices}
        high = max(tally.values())
        leaders = [choice for choice, count in tally.items() if count == high]
        winner = (
            leaders[0]
            if len(leaders) == 1
            else self._chair_tie(option_set_id, leaders).candidate_id
        )
        required = high_threshold(self.repo, eligible_count)
        return {
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

    @staticmethod
    def _validate_review_plan(plan: NewOptionReviewPlan, expected: set[str]) -> None:
        actual = [item.proposal_id for item in plan.reviews]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("Chair qualification must review every new option exactly once")

    @staticmethod
    def _validate_objection_ballot(
        action: NewOptionObjectionBallot, proposal_ids: list[str]
    ) -> None:
        actual = [item.proposal_id for item in action.votes]
        if len(actual) != len(set(actual)) or set(actual) != set(proposal_ids):
            raise ValueError("objection ballot must cover every challenged exclusion")

    @staticmethod
    def _validate_choice_ballot(action: ReconstructedBallot, legal: dict[str, list[str]]) -> None:
        actual = [item.option_set_id for item in action.votes]
        if len(actual) != len(set(actual)) or set(actual) != set(legal):
            raise ValueError("ballot must cover every reconstructed option set")
        for item in action.votes:
            if item.choice not in legal[item.option_set_id]:
                raise ValueError("ballot selected an ineligible reconstructed option")

    @staticmethod
    def _validate_explanations(
        action: ReconstructedExplanationSet, legal: dict[str, list[str]]
    ) -> None:
        actual = [item.option_set_id for item in action.explanations]
        if len(actual) != len(set(actual)) or set(actual) != set(legal):
            raise ValueError("explanations must cover every unresolved reconstructed set")
        for item in action.explanations:
            if item.preferred_proposal_id not in legal[item.option_set_id]:
                raise ValueError("explanation preferred an ineligible option")

    @staticmethod
    def _tally(ballots, option_set_id: str, candidates: list[str]) -> dict[str, int]:
        return {
            candidate: sum(
                next(v for v in ballot.votes if v.option_set_id == option_set_id).choice
                == candidate
                for ballot in ballots
            )
            for candidate in candidates
        }

    @staticmethod
    def _extract_clause(text: str, clause_id: str) -> str:
        pattern = re.compile(
            rf"^{re.escape(clause_id)}\s+.*?"
            rf"(?=^[0-9]+\.[0-9]+(?:-[A-Z]+)?\s+|^第\S+条(?:\s|$)|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError(f"target clause {clause_id} is not uniquely present")
        return matches[0].group(0).strip()
