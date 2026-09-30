from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.domain import MeetingPhase
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.storage.meeting import MeetingRepository


class ChairProposalGrouping(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_clause_id: str = Field(pattern=r"^[0-9]+\.[0-9]+(?:-[A-Z]+)?$")
    relationship: Literal["INDEPENDENT", "MUTUALLY_EXCLUSIVE"]
    proposal_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def relationship_matches_size(self) -> "ChairProposalGrouping":
        if len(self.proposal_ids) != len(set(self.proposal_ids)):
            raise ValueError("proposal IDs within a grouping must be unique")
        if self.relationship == "INDEPENDENT" and len(self.proposal_ids) != 1:
            raise ValueError("an independent grouping contains exactly one proposal")
        if self.relationship == "MUTUALLY_EXCLUSIVE" and len(self.proposal_ids) < 2:
            raise ValueError("a mutually exclusive grouping contains at least two proposals")
        return self


class ChairClauseOptionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groupings: list[ChairProposalGrouping]


class ClauseOptionSetResult(BaseModel):
    option_set_count: int
    type_i_count: int
    type_ii_count: int
    type_iii_count: int
    record_path: str
    next_phase: MeetingPhase
    paused_reason: str | None = None


class ClauseOptionSetRunner:
    """Freeze Chair's procedural partition of proposals into Type I/II/III sets."""

    BOUNDARY_REASON = "DETAILED_CLAUSE_BALLOT_EXECUTION_NOT_IMPLEMENTED"

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
        continue_into_ballots: bool = False,
    ):
        self.repo = repo
        self.engine = engine
        self.governance_docs = Path(governance_docs)
        self.max_output_tokens = max_output_tokens
        self.continue_into_ballots = continue_into_ballots

    def run(self, *, draft_path: Path, active_reviews_path: Path) -> ClauseOptionSetResult:
        relative = Path("public/detailed_clauses/option_sets.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            frozen = json.loads(absolute.read_text(encoding="utf-8"))
        else:
            self.repo.events.append(
                "MEETING_RESUMED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "prior_reason_code": "CLAUSE_OPTION_SET_CONSTRUCTION_NOT_IMPLEMENTED",
                    "resume_stage": MeetingPhase.CLAUSE_REVIEW.value,
                },
                actor="orchestrator",
            )
            frozen = self._construct(draft_path=draft_path, active_reviews_path=active_reviews_path)
            self.repo.docs.write_once(relative, json.dumps(frozen, indent=2, ensure_ascii=False))
            self.repo.events.append(
                "DETAILED_CLAUSE_OPTION_SETS_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "option_set_count": frozen["option_set_count"],
                    "type_i_count": frozen["type_counts"]["TYPE_I"],
                    "type_ii_count": frozen["type_counts"]["TYPE_II"],
                    "type_iii_count": frozen["type_counts"]["TYPE_III"],
                    "proposal_count": frozen["proposal_count"],
                    "record_path": str(relative),
                },
                actor="CHAIR",
            )
        self.engine.progress.speech(
            "CHAIR",
            "细则 Type I/II/III option sets（已冻结）",
            json.dumps(frozen, indent=2, ensure_ascii=False),
        )
        if self.continue_into_ballots:
            self.engine.status.phase = MeetingPhase.BALLOT
            self.engine.status.paused_reason = None
            next_phase = MeetingPhase.BALLOT
            paused_reason = None
        else:
            self._pause_at_ballot_boundary(relative=relative, frozen=frozen)
            next_phase = MeetingPhase.PAUSED
            paused_reason = self.BOUNDARY_REASON
        return ClauseOptionSetResult(
            option_set_count=frozen["option_set_count"],
            type_i_count=frozen["type_counts"]["TYPE_I"],
            type_ii_count=frozen["type_counts"]["TYPE_II"],
            type_iii_count=frozen["type_counts"]["TYPE_III"],
            record_path=str(relative),
            next_phase=next_phase,
            paused_reason=paused_reason,
        )

    def _construct(self, *, draft_path: Path, active_reviews_path: Path) -> dict:
        reviews = json.loads(active_reviews_path.read_text(encoding="utf-8"))
        proposals = reviews["proposals"]
        proposal_by_id = {item["proposal_id"]: item for item in proposals}
        proposal_groups: dict[str, list[dict]] = {}
        for proposal in proposals:
            proposal_groups.setdefault(proposal["target_clause_id"], []).append(proposal)
        system_text = self._chair_system_text()
        draft_text = draft_path.read_text(encoding="utf-8")
        all_groupings: list[ChairProposalGrouping] = []
        adjudicated_target_count = sum(
            1 for target_proposals in proposal_groups.values() if len(target_proposals) > 1
        )
        completed_adjudications = 0
        for target_clause_id, target_proposals in proposal_groups.items():
            if len(target_proposals) == 1:
                proposal = target_proposals[0]
                all_groupings.append(
                    ChairProposalGrouping(
                        target_clause_id=target_clause_id,
                        relationship="INDEPENDENT",
                        proposal_ids=[proposal["proposal_id"]],
                        reason="Only one frozen proposal targets this clause; it forms a Type I set.",
                    )
                )
                continue
            completed_adjudications += 1
            target_proposal_by_id = {
                item["proposal_id"]: item for item in target_proposals
            }
            plan_relative = (
                Path("chair_private/detailed_clauses/option_plans")
                / f"{target_clause_id.replace('.', '_').replace('-', '_')}.json"
            )
            plan_path = self.repo.root / plan_relative
            if plan_path.exists():
                plan = ChairClauseOptionPlan.model_validate_json(
                    plan_path.read_text(encoding="utf-8")
                )
                self._validate_plan(plan, target_proposal_by_id)
                self.engine.progress.info(
                    f"已恢复 Chair option-set 分类 {completed_adjudications}/"
                    f"{adjudicated_target_count} · 条款 {target_clause_id}"
                )
            else:
                user_text = self._target_grouping_prompt(
                    target_clause_id=target_clause_id,
                    standing_clause=self._extract_clause(draft_text, target_clause_id),
                    proposals=target_proposals,
                )
                self.engine.progress.info(
                    f"Chair 正在分类 {completed_adjudications}/{adjudicated_target_count} · "
                    f"条款 {target_clause_id} · {len(target_proposals)} 项提案"
                )
                response = self.engine.find_recorded_response(
                    "CHAIR",
                    system_text=system_text,
                    user_text=user_text,
                    stage="chair_clause_option_construction",
                )
                if response is None:
                    response = self.engine.invoke_participant(
                        "CHAIR",
                        system_text=system_text,
                        user_text=user_text,
                        stage="chair_clause_option_construction",
                        max_output_tokens=self.max_output_tokens,
                    )
                else:
                    self.engine.progress.info(
                        f"已恢复条款 {target_clause_id} 的 Chair 原始响应"
                    )
                plan = self.engine.validate_structured_response(
                    "CHAIR",
                    response=response,
                    schema_model=ChairClauseOptionPlan,
                    stage="chair_clause_option_construction",
                    max_output_tokens=self.max_output_tokens,
                    semantic_requirement=(
                        f"Classify every proposal targeting clause {target_clause_id} exactly once; "
                        "do not judge merit, rewrite text, or combine compatible proposals."
                    ),
                )
                try:
                    self._validate_plan(plan, target_proposal_by_id)
                except (TypeError, ValueError):
                    self.engine.pause_for_unconfigured_policy(
                        participant_id="CHAIR",
                        reason_code="INVALID_CLAUSE_OPTION_GROUPING_AFTER_SCHEMA_VALIDATION",
                    )
                self.repo.docs.write_once(plan_relative, plan.model_dump_json(indent=2))
                self.repo.events.append(
                    "CHAIR_CLAUSE_OPTION_GROUPING_FROZEN",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "target_clause_id": target_clause_id,
                        "proposal_count": len(target_proposals),
                        "grouping_count": len(plan.groupings),
                        "record_path": str(plan_relative),
                    },
                    actor="CHAIR",
                )
            all_groupings.extend(plan.groupings)

        proposal_order = {item["proposal_id"]: index for index, item in enumerate(proposals)}
        groupings = sorted(
            all_groupings,
            key=lambda item: min(proposal_order[proposal_id] for proposal_id in item.proposal_ids),
        )
        per_clause: dict[str, int] = {}
        option_sets = []
        for grouping in groupings:
            per_clause[grouping.target_clause_id] = per_clause.get(grouping.target_clause_id, 0) + 1
            ordinal = per_clause[grouping.target_clause_id]
            size = len(grouping.proposal_ids)
            option_type = "TYPE_I" if size == 1 else "TYPE_II" if size == 2 else "TYPE_III"
            option_sets.append(
                {
                    "option_set_id": (
                        f"OS-{grouping.target_clause_id.replace('.', '-')}-{ordinal:03d}"
                    ),
                    "target_clause_id": grouping.target_clause_id,
                    "option_set_type": option_type,
                    "relationship": grouping.relationship,
                    "proposal_ids": grouping.proposal_ids,
                    "options": [proposal_by_id[item_id] for item_id in grouping.proposal_ids],
                    "chair_reason": grouping.reason,
                }
            )
        type_counts = {
            option_type: sum(1 for item in option_sets if item["option_set_type"] == option_type)
            for option_type in ("TYPE_I", "TYPE_II", "TYPE_III")
        }
        return {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "source_draft_path": str(draft_path.relative_to(self.repo.root)),
            "source_reviews_path": str(active_reviews_path.relative_to(self.repo.root)),
            "construction_rule": (
                "proposals are partitioned independently per target clause; singleton targets are "
                "mechanical Type I sets; Chair classifies only multi-proposal targets; compatible "
                "proposals are independent singleton sets and only mutually exclusive proposals share "
                "a set; set cardinality 1/2/3+ maps to Type I/II/III"
            ),
            "proposal_count": len(proposals),
            "option_set_count": len(option_sets),
            "type_counts": type_counts,
            "option_sets": option_sets,
        }

    @staticmethod
    def _target_grouping_prompt(
        *, target_clause_id: str, standing_clause: str, proposals: list[dict]
    ) -> str:
        return (
            "Perform only procedural option-set construction for the frozen proposals targeting one "
            "detailed clause. Put every compatible proposal in its own INDEPENDENT singleton grouping "
            "so it receives a Type I support/oppose ballot. Group two or more proposals together as "
            "MUTUALLY_EXCLUSIVE only when their normative effects cannot all be applied to this clause. "
            "Similarity, overlap, redundancy, or addressing the same problem is not by itself mutual "
            "exclusion. Do not rank substantive merit, merge proposals, rewrite text, or omit a proposal. "
            "Return exactly one JSON object with key groupings. Each grouping has target_clause_id, "
            "relationship (INDEPENDENT or MUTUALLY_EXCLUSIVE), proposal_ids, and reason. Every supplied "
            "proposal_id must occur exactly once. Do not use Markdown.\n\nTARGET CLAUSE ID:\n"
            + target_clause_id
            + "\n\nCURRENT STANDING CLAUSE:\n"
            + standing_clause
            + "\n\nFROZEN PROPOSALS FOR THIS CLAUSE:\n"
            + json.dumps(proposals, indent=2, ensure_ascii=False)
        )

    @staticmethod
    def _extract_clause(draft_text: str, clause_id: str) -> str:
        pattern = re.compile(
            rf"^{re.escape(clause_id)}\s+.*?"
            rf"(?=^[0-9]+\.[0-9]+(?:-[A-Z]+)?\s+|^第\S+条(?:\s|$)|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        matches = list(pattern.finditer(draft_text))
        if len(matches) != 1:
            raise ValueError(f"target clause {clause_id} is not uniquely present in standing text")
        return matches[0].group(0).strip()

    @staticmethod
    def _validate_plan(plan: ChairClauseOptionPlan, proposal_by_id: dict[str, dict]) -> None:
        expected = set(proposal_by_id)
        actual = [proposal_id for item in plan.groupings for proposal_id in item.proposal_ids]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("Chair option plan must contain every proposal exactly once")
        for grouping in plan.groupings:
            targets = {
                proposal_by_id[proposal_id]["target_clause_id"]
                for proposal_id in grouping.proposal_ids
            }
            if targets != {grouping.target_clause_id}:
                raise ValueError("an option grouping may contain only its declared target clause")

    def _chair_system_text(self) -> str:
        files = (
            self.governance_docs / "03_roles/chair/chair_role.md",
            self.governance_docs / "07_runtime_memory/other_participants/deliberation_chair.md",
            self.governance_docs / "02_deliberation/deliberation_protocol.md",
        )
        return "\n\n".join(path.read_text(encoding="utf-8") for path in files)

    def _pause_at_ballot_boundary(self, *, relative: Path, frozen: dict) -> None:
        pause_relative = Path(
            "governance_private/detailed_clauses/pause_detailed_clause_ballot_execution_not_implemented.json"
        )
        if not (self.repo.root / pause_relative).exists():
            self.repo.docs.write_once(
                pause_relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "reason_code": self.BOUNDARY_REASON,
                        "completed_stage": "C2_OPTION_SET_CONSTRUCTION",
                        "option_set_count": frozen["option_set_count"],
                        "option_sets_path": str(relative),
                    },
                    indent=2,
                ),
            )
            self.repo.events.append(
                "MEETING_PAUSED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "reason_code": self.BOUNDARY_REASON,
                    "record_path": str(pause_relative),
                },
                actor="orchestrator",
            )
        self.engine.status.phase = MeetingPhase.PAUSED
        self.engine.status.paused_reason = self.BOUNDARY_REASON
        self.engine.progress.status(
            MeetingPhase.PAUSED,
            f"{self.BOUNDARY_REASON} · option sets 已冻结；下一步进入 Type I/II/III 表决",
        )
