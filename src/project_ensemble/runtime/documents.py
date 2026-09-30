from __future__ import annotations

from pathlib import Path
from typing import Literal
from project_ensemble.domain import Persona
from project_ensemble.runtime.context import RepresentativeContextSpec


RepresentativePromptFamily = Literal["legacy_shared", "deliberation", "literature_research"]


PERSONA_FILES = {
    Persona.SYSTEMS_INTEGRATOR: "systems_integrator.md",
    Persona.PRAGMATIC_MINIMALIST: "pragmatic_minimalist.md",
    Persona.EXPLORATORY_SYNTHESIST: "exploratory_synthesist.md",
    Persona.LIBRARIAN: "librarian.md",
}

STAGE_FILES = {
    "initial_draft": "phase_initial_draft.md",
    "research_request": "phase_research_request.md",
    "general_position": "phase_general_position.md",
    "cosponsorship": "phase_cosponsorship.md",
    "ballot": "phase_ballot.md",
    "reason_statement": "phase_reason_statement.md",
    "primary_drafter": "phase_primary_drafter.md",
    "active_detailed_drafting": "phase_active_detailed_drafting.md",
    "consultative": "phase_consultative.md",
    "post_meeting_submission": "phase_post_meeting_submission.md",
    "type_i_revision": "phase_type_i_revision.md",
    "research_decomposition": "phase_research_decomposition.md",
    "research_outline_review": "phase_research_outline_review.md",
    "literature_module_questions": "phase_literature_module_questions.md",
    "literature_module_followup": "phase_literature_module_followup.md",
    "literature_module_followup_question_revision": "phase_literature_module_followup.md",
    "literature_module_draft": "phase_literature_module_draft.md",
    "literature_module_specialist_review": "phase_literature_module_specialist_review.md",
    "literature_module_formal_review": "phase_literature_module_formal_review.md",
    "literature_module_confirmation": "phase_literature_module_confirmation.md",
    "literature_module_amendment": "phase_literature_module_amendment.md",
    "literature_module_dissent": "phase_literature_module_dissent.md",
    "literature_report_draft": "phase_literature_report_draft.md",
    "literature_report_review": "phase_literature_report_review.md",
    "literature_report_final_position": "phase_literature_report_final_position.md",
    "literature_report_fact_vote": "phase_literature_report_fact_vote.md",
    "literature_publication_patch_vote": "phase_literature_publication_patch_vote.md",
    "scholarly_science_review": "phase_scholarly_science_review.md",
    "scholarly_citation_review": "phase_scholarly_citation_review.md",
}


class GovernanceDocumentResolver:
    """Resolves exactly one persona and one current-stage protocol from governance docs."""

    def __init__(self, governance_root: str | Path):
        self.root = Path(governance_root)

    def representative_context_spec(
        self,
        *,
        persona: Persona,
        stage: str,
        representative_id: str | None = None,
        public_state_files: tuple[Path, ...] = (),
        own_state_files: tuple[Path, ...] = (),
        prompt_family: RepresentativePromptFamily = "deliberation",
    ) -> RepresentativeContextSpec:
        if stage not in STAGE_FILES:
            raise KeyError(f"unknown or not-yet-surfaced Representative stage: {stage}")
        if prompt_family not in {"legacy_shared", "deliberation", "literature_research"}:
            raise ValueError(f"unknown Representative prompt family: {prompt_family}")
        public_files = list(public_state_files)
        meeting_root = self._meeting_root((*public_state_files, *own_state_files))
        if meeting_root is not None and stage in {
            "ballot", "type_i_revision", "literature_module_confirmation"
        }:
            policy_path = meeting_root / "public/decision_policy.json"
            if policy_path.is_file() and policy_path not in public_files:
                public_files.append(policy_path)
        released_snapshots: list[Path] = []
        if meeting_root is not None:
            released_snapshots = sorted(
                (meeting_root / "public/research/rounds").glob("*/evidence_snapshot.json")
            )
        literature = prompt_family == "literature_research"
        return RepresentativeContextSpec(
            common_rules=self.root / "01_constitution" / (
                "literature_representative_rules.md"
                if literature else "common_representative_rules.md"
            ),
            persona_runtime=self.root / "07_runtime_memory" / (
                "literature_research" if literature else "deliberation"
            ) / PERSONA_FILES[persona],
            current_stage_protocol=self.root / "07_runtime_protocol" / "deliberation" / STAGE_FILES[stage],
            public_state_files=tuple(public_files),
            research_evidence_files=tuple(released_snapshots),
            own_state_files=own_state_files,
            representative_id=representative_id,
            stage=stage,
            meeting_root=meeting_root,
            governance_root=self.root,
        )

    @staticmethod
    def _meeting_root(paths: tuple[Path, ...]) -> Path | None:
        for path in paths:
            resolved = path.resolve()
            for parent in (resolved.parent, *resolved.parents):
                if parent.name in {
                    "public",
                    "representatives",
                    "governance_private",
                    "identity_private",
                }:
                    return parent.parent
        return None
