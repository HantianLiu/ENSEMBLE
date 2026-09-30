from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from project_ensemble.domain import MeetingPhase
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.storage.meeting import MeetingRepository


class AppliedClauseProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    proposal_id: str = Field(min_length=1)
    target_clause_id: str = Field(pattern=r"^[0-9]+\.[0-9]+(?:-[A-Z]+)?$")
    proposal_type: Literal["SUPPLEMENT", "REPLACE", "NEW_OPTION"]
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    applied_excerpt: str = Field(min_length=1)
    application_summary: str = Field(min_length=1)


class ChairClauseApplication(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["APPLY_ADOPTED_DETAILED_CLAUSES"]
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_text: str = Field(min_length=1)
    applications: list[AppliedClauseProposal]


class ClauseApplicationResult(BaseModel):
    draft_path: str
    provenance_path: str
    applied_proposal_count: int
    contested_proposal_count: int
    next_phase: MeetingPhase
    paused_reason: str | None = None


class ClauseApplicationRunner:
    """Apply the complete frozen clause ballot outcome without substantive discretion."""

    BOUNDARY_REASON = "CHAIR_PROCEDURAL_CERTIFICATION_NOT_IMPLEMENTED"

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
        continue_into_reviews: bool = False,
    ):
        self.repo = repo
        self.engine = engine
        self.governance_docs = Path(governance_docs)
        self.max_output_tokens = max_output_tokens
        self.continue_into_reviews = continue_into_reviews

    def run(
        self,
        *,
        draft_path: Path,
        option_sets_path: Path,
        initial_outcomes_path: Path,
        runoff_outcomes_path: Path,
        second_outcomes_path: Path | None = None,
        type_i_revisions_path: Path | None = None,
        type_i_second_outcomes_path: Path | None = None,
    ) -> ClauseApplicationResult:
        self._ensure_resume_record()
        adopted = self._adopted_proposals(
            option_sets_path=option_sets_path,
            initial_outcomes_path=initial_outcomes_path,
            runoff_outcomes_path=runoff_outcomes_path,
            second_outcomes_path=second_outcomes_path,
            type_i_revisions_path=type_i_revisions_path,
            type_i_second_outcomes_path=type_i_second_outcomes_path,
        )
        draft_relative = Path("public/detailed_clauses/C1.md")
        provenance_relative = Path("public/detailed_clauses/C1.provenance.json")
        application_relative = Path("chair_private/detailed_clauses/C1_application.json")
        draft_output = self.repo.root / draft_relative
        application_path = self.repo.root / application_relative
        base_text = draft_path.read_text(encoding="utf-8")
        base_sha = self._sha256(base_text)

        self.engine.status.phase = MeetingPhase.CHAIR_REVIEW
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.CHAIR_REVIEW,
            f"Chair 正在把 {len(adopted)} 项冻结胜出提案机械应用到 C0，并建立逐项 provenance",
        )
        if application_path.exists():
            application = ChairClauseApplication.model_validate_json(
                application_path.read_text(encoding="utf-8")
            )
        else:
            response = self.engine.invoke_participant(
                "CHAIR",
                system_text=self._chair_system_text(),
                user_text=(
                    "Perform one document-control operation: apply every supplied ADOPTED PROPOSAL to C0 "
                    "and produce C1. The Assembly has already selected the substance; you have no authority "
                    "to select, reject, weaken, strengthen, merge away, or add normative content. Preserve "
                    "unaffected C0 text. Render proposal wording into the target clause with only the minimal "
                    "grammatical, numbering, and cross-reference edits needed for a coherent document. For "
                    "SUPPLEMENT, retain the existing clause and add the full normative effect. For REPLACE, "
                    "replace only the identified target material. For NEW_OPTION, add the selected option at "
                    "its target. Return exactly one JSON object with action "
                    "APPLY_ADOPTED_DETAILED_CLAUSES, base_sha256, final_text, and applications. Include each "
                    "adopted proposal exactly once in applications, with proposal_id, target_clause_id, "
                    "proposal_type, source_text_sha256, applied_excerpt (an exact non-empty substring of "
                    "final_text showing its actual landing), and application_summary. Echo all supplied IDs, "
                    "types, targets, hashes, and the base hash exactly. Do not use Markdown fences.\n\n"
                    f"BASE SHA-256: {base_sha}\n\nCURRENT C0:\n{base_text}\n\n"
                    "ADOPTED PROPOSALS AND FROZEN OUTCOMES:\n"
                    + json.dumps(adopted, indent=2, ensure_ascii=False)
                ),
                stage="chair_apply_detailed_clauses",
                max_output_tokens=self.max_output_tokens,
            )
            application = self.engine.validate_structured_response(
                "CHAIR",
                response=response,
                schema_model=ChairClauseApplication,
                stage="chair_apply_detailed_clauses",
                max_output_tokens=self.max_output_tokens,
                semantic_requirement=(
                    "Preserve all C0 content except exact adopted replacements, and apply every supplied "
                    "winner once without introducing any non-adopted normative content."
                ),
            )
            self._validate_application(
                application=application,
                adopted=adopted,
                base_sha=base_sha,
                base_text=base_text,
            )
            self.repo.docs.write_once(application_relative, application.model_dump_json(indent=2))
            self.repo.events.append(
                "CHAIR_DETAILED_CLAUSE_APPLICATION_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "base_sha256": base_sha,
                    "applied_proposal_count": len(adopted),
                    "record_path": str(application_relative),
                },
                actor="CHAIR",
            )

        self._validate_application(
            application=application,
            adopted=adopted,
            base_sha=base_sha,
            base_text=base_text,
        )
        final_text = application.final_text.rstrip() + "\n"
        if not draft_output.exists():
            self.repo.docs.write_once(draft_relative, final_text)
            provenance = {
                "meeting_id": self.repo.meeting_id,
                "draft": "C1",
                "status": "PROVISIONAL_RESOLUTION",
                "base_draft_path": str(draft_path.relative_to(self.repo.root)),
                "base_sha256": base_sha,
                "result_sha256": self._sha256(final_text),
                "chair_application_path": str(application_relative),
                "applied_proposal_count": len(adopted),
                "contested_proposal_count": sum(item["contested"] for item in adopted),
                "applications": [item.model_dump(mode="json") for item in application.applications],
                "outcome_sources": sorted({item["outcome_path"] for item in adopted}),
            }
            self.repo.docs.write_once(
                provenance_relative,
                json.dumps(provenance, indent=2, ensure_ascii=False),
            )
            self.repo.events.append(
                "DETAILED_CLAUSE_PROVISIONAL_RESOLUTION_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "draft_path": str(draft_relative),
                    "provenance_path": str(provenance_relative),
                    "result_sha256": provenance["result_sha256"],
                    "applied_proposal_count": len(adopted),
                },
                actor="orchestrator",
            )
        elif draft_output.read_text(encoding="utf-8") != final_text:
            raise ValueError("persisted C1 does not match the frozen Chair application")

        self.engine.progress.speech("CHAIR", "细则临时决议 C1（已冻结）", final_text)
        if self.continue_into_reviews:
            self.engine.status.phase = MeetingPhase.CHAIR_REVIEW
            self.engine.status.paused_reason = None
            next_phase = MeetingPhase.CHAIR_REVIEW
            paused_reason = None
        else:
            self._pause(draft_relative=draft_relative, provenance_relative=provenance_relative)
            next_phase = MeetingPhase.PAUSED
            paused_reason = self.BOUNDARY_REASON
        return ClauseApplicationResult(
            draft_path=str(draft_relative),
            provenance_path=str(provenance_relative),
            applied_proposal_count=len(adopted),
            contested_proposal_count=sum(item["contested"] for item in adopted),
            next_phase=next_phase,
            paused_reason=paused_reason,
        )

    def _adopted_proposals(
        self,
        *,
        option_sets_path: Path,
        initial_outcomes_path: Path,
        runoff_outcomes_path: Path,
        second_outcomes_path: Path | None,
        type_i_revisions_path: Path | None,
        type_i_second_outcomes_path: Path | None,
    ) -> list[dict]:
        option_sets = json.loads(option_sets_path.read_text(encoding="utf-8"))["option_sets"]
        proposal_by_id = {
            proposal["proposal_id"]: proposal
            for option_set in option_sets
            for proposal in option_set["options"]
        }
        if type_i_revisions_path is not None:
            revisions = json.loads(type_i_revisions_path.read_text(encoding="utf-8"))
            for decision in revisions["decisions"]:
                proposal_id = decision["proposal_id"]
                if proposal_id not in proposal_by_id:
                    raise ValueError(f"unknown revised Type I proposal {proposal_id}")
                proposal_by_id[proposal_id] = {
                    **proposal_by_id[proposal_id],
                    "text": decision["final_text"],
                    "type_i_revision_action": decision["action"],
                    "type_i_revision_summary": decision["revision_summary"],
                    "type_i_revisions_path": str(
                        type_i_revisions_path.relative_to(self.repo.root)
                    ),
                }
        adopted_by_set: dict[str, dict] = {}
        resolved_sets: set[str] = set()
        outcome_paths = [initial_outcomes_path, runoff_outcomes_path]
        if second_outcomes_path is not None:
            outcome_paths.append(second_outcomes_path)
        if type_i_second_outcomes_path is not None:
            outcome_paths.append(type_i_second_outcomes_path)
        for path in outcome_paths:
            record = json.loads(path.read_text(encoding="utf-8"))
            for outcome in record["outcomes"]:
                proposal_id = outcome.get("adopted_proposal_id")
                if proposal_id is None:
                    if outcome.get("status") in {
                        "TYPE_I_REJECTED_SECOND_ROUND",
                        "STATUS_QUO_RETAINED",
                    }:
                        resolved_sets.add(outcome["option_set_id"])
                    continue
                option_set_id = outcome["option_set_id"]
                if option_set_id in adopted_by_set:
                    raise ValueError(f"option set {option_set_id} has multiple frozen winners")
                if proposal_id not in proposal_by_id:
                    raise ValueError(f"unknown adopted proposal {proposal_id}")
                proposal = proposal_by_id[proposal_id]
                adopted_by_set[option_set_id] = {
                    **proposal,
                    "source_text_sha256": self._sha256(proposal["text"]),
                    "outcome_path": str(path.relative_to(self.repo.root)),
                    "outcome_status": outcome["status"],
                    "contested": bool(outcome.get("contested", False)),
                }
                resolved_sets.add(option_set_id)
        expected_sets = {item["option_set_id"] for item in option_sets}
        if resolved_sets != expected_sets:
            missing = sorted(expected_sets - resolved_sets)
            raise ValueError(
                f"not every option set has one frozen terminal outcome; missing={missing}"
            )
        return [
            adopted_by_set[item["option_set_id"]]
            for item in option_sets
            if item["option_set_id"] in adopted_by_set
        ]

    @staticmethod
    def _validate_application(
        *,
        application: ChairClauseApplication,
        adopted: list[dict],
        base_sha: str,
        base_text: str,
    ) -> None:
        if application.base_sha256 != base_sha:
            raise ValueError("Chair application base hash does not match C0")
        if adopted and application.final_text.strip() == base_text.strip():
            raise ValueError("adopted proposal application did not change C0")
        expected = {item["proposal_id"]: item for item in adopted}
        actual_ids = [item.proposal_id for item in application.applications]
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
            raise ValueError("Chair application must cover every adopted proposal exactly once")
        for item in application.applications:
            source = expected[item.proposal_id]
            if (
                item.target_clause_id != source["target_clause_id"]
                or item.proposal_type != source["proposal_type"]
                or item.source_text_sha256 != source["source_text_sha256"]
            ):
                raise ValueError(f"Chair application changed frozen metadata for {item.proposal_id}")
            if item.applied_excerpt not in application.final_text:
                raise ValueError(f"applied excerpt for {item.proposal_id} is not present in C1")

    def _ensure_resume_record(self) -> None:
        relative = Path("governance_private/detailed_clauses/clause_application_resume.json")
        if (self.repo.root / relative).exists():
            return
        record = {
            "meeting_id": self.repo.meeting_id,
            "prior_reason_code": "ADOPTED_CLAUSE_APPLICATION_NOT_IMPLEMENTED",
            "resume_stage": MeetingPhase.CHAIR_REVIEW.value,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append("MEETING_RESUMED", record, actor="orchestrator")

    def _pause(self, *, draft_relative: Path, provenance_relative: Path) -> None:
        relative = Path(
            "governance_private/detailed_clauses/pause_chair_procedural_certification_not_implemented.json"
        )
        if not (self.repo.root / relative).exists():
            self.repo.docs.write_once(
                relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "reason_code": self.BOUNDARY_REASON,
                        "provisional_draft_path": str(draft_relative),
                        "provenance_path": str(provenance_relative),
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
            f"{self.BOUNDARY_REASON} · C1 临时决议及逐项 provenance 已冻结；下一步执行 Chair 程序终检",
        )

    def _chair_system_text(self) -> str:
        files = (
            self.governance_docs / "03_roles/chair/chair_role.md",
            self.governance_docs / "07_runtime_memory/other_participants/deliberation_chair.md",
            self.governance_docs / "02_deliberation/deliberation_protocol.md",
            self.governance_docs / "02_deliberation/finalization_and_handoff.md",
        )
        return "\n\n".join(path.read_text(encoding="utf-8") for path in files)

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
