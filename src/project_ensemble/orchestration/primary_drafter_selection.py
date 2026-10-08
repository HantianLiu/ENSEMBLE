from __future__ import annotations

import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.runtime.model_lanes import run_bounded_representative_lanes
from project_ensemble.runtime.model_replacements import current_runtime_for
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.runtime.prompt_contract import prompt_contract_version


class PrimaryDrafterRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_ids: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def unique_candidates(self) -> "PrimaryDrafterRanking":
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidate_ids must be a ranking without duplicates")
        return self


class PrimaryDrafterChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate_id: str = Field(min_length=1)


class PrimaryDrafterChairDecision(PrimaryDrafterChoice):
    reason: str = Field(min_length=1, max_length=500)


class PrimaryDrafterSelectionRunner:
    """Resolve a Drafting Alignment tie without disclosing the private scores."""

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

    def _ballot_system_text(self) -> str:
        if prompt_contract_version(self.repo.root) >= 2:
            return (
                "Task: select a Primary Drafter using only the approved text and current public "
                "substantive work. Return only the requested sealed ballot."
            )
        return (
            "Task: participate only in a sealed Primary Drafter selection. "
            "The private Drafting Alignment scores and ranks are unavailable and must not "
            "be inferred or requested. Base the choice on the approved text and public work."
        )

    def run(
        self,
        *,
        candidates: list[str],
        representative_records: list[dict],
        final_draft: Path,
    ) -> str:
        candidates = sorted(set(candidates))
        if len(candidates) < 2:
            raise ValueError("a tied Primary Drafter selection requires at least two candidates")
        outcome_path = self.repo.root / "governance_private/drafting_alignment/primary_drafter_selection.json"
        if outcome_path.exists():
            frozen = json.loads(outcome_path.read_text(encoding="utf-8"))
            if frozen.get("candidate_ids") != candidates:
                raise ValueError("frozen Primary Drafter candidate set conflicts with current scores")
            return str(frozen["winner_id"])

        ranking = self._collect_rankings(
            stage="primary_drafter_ranking",
            candidates=candidates,
            representative_records=representative_records,
            final_draft=final_draft,
        )
        first_tally = self._first_preference_tally(ranking.values(), candidates)
        finalists = self._advancing_pair(
            candidates=candidates,
            tally=first_tally,
            representative_records=representative_records,
            final_draft=final_draft,
        )
        final_choices = self._collect_choices(
            stage="primary_drafter_final_binary",
            candidates=finalists,
            representative_records=representative_records,
            final_draft=final_draft,
        )
        final_tally = {
            candidate: sum(choice.candidate_id == candidate for choice in final_choices.values())
            for candidate in finalists
        }
        required = math.floor(len(representative_records) / 2) + 1
        winner = next(
            (candidate for candidate in finalists if final_tally[candidate] >= required),
            None,
        )
        chair_reason = None
        if winner is None:
            decision = self._chair_decision(
                stage="chair_primary_drafter_final_tiebreak",
                candidates=finalists,
                final_draft=final_draft,
            )
            winner = decision.candidate_id
            chair_reason = decision.reason

        record = {
            "meeting_id": self.repo.meeting_id,
            "policy_version": "PRIMARY_DRAFTER_TIE_V1",
            "candidate_ids": candidates,
            "eligible_voter_ids": [item["representative_id"] for item in representative_records],
            "first_preference_tally": first_tally,
            "finalist_ids": finalists,
            "final_binary_tally": final_tally,
            "strict_majority_required": required,
            "chair_tiebreak_used": chair_reason is not None,
            "chair_tiebreak_reason": chair_reason,
            "winner_id": winner,
        }
        relative = Path("governance_private/drafting_alignment/primary_drafter_selection.json")
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "PRIMARY_DRAFTER_TIE_RESOLVED",
            {
                "meeting_id": self.repo.meeting_id,
                "candidate_count": len(candidates),
                "eligible_voter_count": len(representative_records),
                "chair_tiebreak_used": chair_reason is not None,
                "winner_id": winner,
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        return winner

    def _advancing_pair(
        self,
        *,
        candidates: list[str],
        tally: dict[str, int],
        representative_records: list[dict],
        final_draft: Path,
    ) -> list[str]:
        if len(candidates) == 2:
            return candidates
        cutoff = sorted(tally.values(), reverse=True)[1]
        secured = sorted(candidate for candidate, count in tally.items() if count > cutoff)
        tied = sorted(candidate for candidate, count in tally.items() if count == cutoff)
        seats = 2 - len(secured)
        if len(tied) == seats:
            return [*secured, *tied]

        runoff = self._collect_rankings(
            stage="primary_drafter_advancement_runoff",
            candidates=tied,
            representative_records=representative_records,
            final_draft=final_draft,
        )
        runoff_tally = self._first_preference_tally(runoff.values(), tied)
        ordered = sorted(tied, key=lambda item: (-runoff_tally[item], item))
        boundary = runoff_tally[ordered[seats - 1]]
        selected = [item for item in ordered if runoff_tally[item] > boundary]
        boundary_tied = [item for item in ordered if runoff_tally[item] == boundary]
        remaining = seats - len(selected)
        if len(boundary_tied) > remaining:
            while remaining:
                decision = self._chair_decision(
                    stage=f"chair_primary_drafter_advancement_tiebreak_{remaining}",
                    candidates=boundary_tied,
                    final_draft=final_draft,
                )
                selected.append(decision.candidate_id)
                boundary_tied.remove(decision.candidate_id)
                remaining -= 1
        else:
            selected.extend(boundary_tied)
        return [*secured, *selected]

    def _collect_rankings(
        self,
        *,
        stage: str,
        candidates: list[str],
        representative_records: list[dict],
        final_draft: Path,
    ) -> dict[str, PrimaryDrafterRanking]:
        return self._collect(
            stage=stage,
            candidates=candidates,
            representative_records=representative_records,
            final_draft=final_draft,
            schema=PrimaryDrafterRanking,
            prompt=(
                "Submit one sealed complete ranking of candidate_ids from most to least preferred. "
                "Use every supplied candidate exactly once. Do not abstain, add a reason, discuss "
                "scores, or add keys."
            ),
        )

    def _collect_choices(
        self,
        *,
        stage: str,
        candidates: list[str],
        representative_records: list[dict],
        final_draft: Path,
    ) -> dict[str, PrimaryDrafterChoice]:
        return self._collect(
            stage=stage,
            candidates=candidates,
            representative_records=representative_records,
            final_draft=final_draft,
            schema=PrimaryDrafterChoice,
            prompt=(
                "Cast one sealed binary choice. candidate_id must be one supplied candidate. "
                "Do not abstain, add a reason, or add keys."
            ),
        )

    def _collect(
        self,
        *,
        stage: str,
        candidates: list[str],
        representative_records: list[dict],
        final_draft: Path,
        schema: type[BaseModel],
        prompt: str,
    ) -> dict:
        root = Path("governance_private/drafting_alignment/primary_drafter_ballots") / stage
        completed: dict[str, BaseModel] = {}
        missing = []
        for record in representative_records:
            rid = record["representative_id"]
            path = self.repo.root / root / f"{rid}.json"
            if path.exists():
                completed[rid] = schema.model_validate_json(path.read_text(encoding="utf-8"))
            else:
                missing.append(record)

        def collect(record: dict):
            rid = record["representative_id"]
            response = self.engine.invoke_participant(
                rid,
                system_text=self._ballot_system_text(),
                user_text=(
                    prompt
                    + "\n\nCANDIDATE IDS:\n"
                    + json.dumps(candidates, ensure_ascii=False)
                    + "\n\nAPPROVED GENERAL PRINCIPLE:\n"
                    + final_draft.read_text(encoding="utf-8")
                ),
                stage=stage,
                max_output_tokens=self.max_output_tokens,
            )
            action = self.engine.validate_structured_response(
                rid,
                response=response,
                schema_model=schema,
                stage=stage,
                max_output_tokens=self.max_output_tokens,
            )
            self._validate_candidate_set(action, candidates)
            return rid, action

        def persist(result):
            rid, action = result
            relative = root / f"{rid}.json"
            self.repo.docs.write_once(relative, action.model_dump_json(indent=2))
            self.repo.events.append(
                "PRIMARY_DRAFTER_BALLOT_SUBMITTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "representative_id": rid,
                    "stage": stage,
                    "record_path": str(relative),
                },
                actor=rid,
            )

        results = run_bounded_representative_lanes(
            missing,
            collect,
            self.engine.model_concurrency_limit,
            on_result=persist,
            progress=self.engine.progress,
            progress_records=representative_records,
            completed_participant_ids=completed,
            runtime_key=lambda record: current_runtime_for(
                self.repo, str(record["representative_id"])
            ),
        )
        completed.update({rid: action for rid, action in results})
        return completed

    @staticmethod
    def _validate_candidate_set(action: BaseModel, candidates: list[str]) -> None:
        if isinstance(action, PrimaryDrafterRanking):
            if set(action.candidate_ids) != set(candidates) or len(action.candidate_ids) != len(candidates):
                raise ValueError("ranking must cover every candidate exactly once")
        elif isinstance(action, PrimaryDrafterChoice):
            if action.candidate_id not in candidates:
                raise ValueError("choice must name one eligible candidate")

    def _chair_decision(
        self, *, stage: str, candidates: list[str], final_draft: Path
    ) -> PrimaryDrafterChairDecision:
        response = self.engine.invoke_participant(
            "CHAIR",
            system_text=(
                "Task: cast the Chair's public procedural deciding vote in a Primary Drafter tie. "
                "Choose exactly one eligible candidate and give one concise public reason."
            ),
            user_text=(
                "Choose one candidate from "
                + json.dumps(candidates, ensure_ascii=False)
                + ". Do not refer to private scores.\n\nAPPROVED GENERAL PRINCIPLE:\n"
                + final_draft.read_text(encoding="utf-8")
            ),
            stage=stage,
            max_output_tokens=self.max_output_tokens,
        )
        decision = self.engine.validate_structured_response(
            "CHAIR",
            response=response,
            schema_model=PrimaryDrafterChairDecision,
            stage=stage,
            max_output_tokens=self.max_output_tokens,
        )
        self._validate_candidate_set(decision, candidates)
        return decision

    @staticmethod
    def _first_preference_tally(
        rankings, candidates: list[str]
    ) -> dict[str, int]:
        return {
            candidate: sum(ranking.candidate_ids[0] == candidate for ranking in rankings)
            for candidate in candidates
        }
