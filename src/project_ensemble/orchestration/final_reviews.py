from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.domain import MeetingPhase
from project_ensemble.governance_private.thresholds import high_threshold, high_threshold_formula
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.final_publication import FinalPublicationRunner
from project_ensemble.orchestration.post_meeting_accountability import (
    PostMeetingAccountabilityRunner,
)
from project_ensemble.runtime.progress import TaskProgressItem
from project_ensemble.storage.meeting import MeetingRepository


class ProceduralCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    explanation: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class ChairProceduralCertification(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Literal["CERTIFIED", "CORRECTION_REQUIRED", "HUMAN_REQUIRED"]
    resolution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: list[ProceduralCheck] = Field(min_length=1)
    corrections: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def status_matches_checks(self) -> "ChairProceduralCertification":
        failures = [item for item in self.checks if item.status == "FAIL"]
        if self.status == "CERTIFIED" and (failures or self.corrections):
            raise ValueError("CERTIFIED requires all checks to pass and no corrections")
        if self.status != "CERTIFIED" and not (failures or self.corrections):
            raise ValueError("a non-certified result must state a failure or correction")
        return self


class ThinkTankFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    severity: Literal["NOTE", "CAUTION", "MATERIAL"]
    category: Literal[
        "OMISSION",
        "DEFINITION",
        "ASSUMPTION",
        "EVIDENCE",
        "CONTRADICTION",
        "VERIFICATION",
        "LITERATURE_OR_BENCHMARK",
        "PROVENANCE",
        "CONCLUSION_STRENGTH",
        "TRIAL_POLICY",
    ]
    finding: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    human_attention: str = Field(min_length=1)


class LibrarianEpistemicReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    epistemic_status: Literal["COMPLETE", "CAUTIONS", "MATERIAL_OMISSION", "NEEDS_CLARIFICATION"]
    summary: str = Field(min_length=1)
    findings: list[ThinkTankFinding] = Field(default_factory=list)
    reconvene_worthy: bool
    reconvene_reason: str | None = None

    @model_validator(mode="after")
    def reconvene_reason_is_explicit(self) -> "LibrarianEpistemicReview":
        if self.reconvene_worthy and not (self.reconvene_reason or "").strip():
            raise ValueError("reconvene_worthy requires a reason")
        return self


class ThinkTankExecutionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    severity: Literal["NOTE", "CAUTION", "MATERIAL"]
    category: Literal[
        "KNOWLEDGE_COMPLETENESS",
        "MISSING_ASSUMPTION",
        "EVIDENCE_GAP",
        "AMBIGUOUS_CLAIM",
        "EXECUTION_CAUTION",
        "RESEARCH_CAUTION",
        "PROVENANCE",
    ]
    finding: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    human_attention: str = Field(min_length=1)


class LibrarianExecutionReview(BaseModel):
    """One Librarian's independent review of the Chair handoff brief."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    execution_readiness: Literal[
        "READY",
        "READY_WITH_CAUTIONS",
        "HUMAN_CLARIFICATION_REQUIRED",
        "RECONVENE_RECOMMENDED",
    ]
    summary: str = Field(min_length=1)
    findings: list[ThinkTankExecutionFinding] = Field(default_factory=list)
    human_decision_points: list[str] = Field(default_factory=list)
    reconvene_worthy: bool
    reconvene_reason: str | None = None

    @model_validator(mode="after")
    def reconvene_reason_is_explicit(self) -> "LibrarianExecutionReview":
        if self.reconvene_worthy and not (self.reconvene_reason or "").strip():
            raise ValueError("reconvene_worthy requires a reason")
        if self.execution_readiness == "RECONVENE_RECOMMENDED" and not self.reconvene_worthy:
            raise ValueError("RECONVENE_RECOMMENDED requires reconvene_worthy=true")
        return self


class FinalReviewResult(BaseModel):
    chair_certification_status: str
    certified_resolution_path: str | None = None
    librarian_review_count: int = 0
    material_finding_count: int = 0
    reconvene_recommendation_count: int = 0
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


class FinalReviewRunner:
    """Run Chair procedural certification, then independent Librarian reviews."""

    CHAIR_CORRECTION_REASON = "CHAIR_PROCEDURAL_CORRECTION_REQUIRED"
    MECHANICAL_FAILURE_REASON = "MECHANICAL_PROCEDURAL_CHECK_FAILED"
    PUBLICATION_READABILITY_REASON = "FINAL_PUBLICATION_READABILITY_REVISION_REQUIRED"
    CERTIFICATION_EVIDENCE_PATHS = (
        "public/detailed_clauses/motion_outcomes.json",
        "public/detailed_clauses/option_sets.json",
        "public/detailed_clauses/initial_ballot_outcomes.json",
        "public/detailed_clauses/type_i_revisions.json",
        "public/detailed_clauses/type_i_second_ballot_outcomes.json",
        "public/detailed_clauses/type_iii_cutoff_tiebreak_outcomes.json",
        "public/detailed_clauses/type_iii_runoff_outcomes.json",
        "public/detailed_clauses/type_ii_explanations.json",
        "public/detailed_clauses/type_ii_second_ballot_outcomes.json",
        "governance_private/detailed_clauses/trial_docket_policy_activation.json",
        "governance_private/detailed_clauses/type_iii_cutoff_tiebreak_policy.json",
    )

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
        self.governance_docs = Path(governance_docs)
        self.max_output_tokens = max_output_tokens

    def run(self, *, provisional_draft_path: Path, provenance_path: Path) -> FinalReviewResult:
        mechanical_path, mechanical = self._mechanical_check(
            provisional_draft_path=provisional_draft_path,
            provenance_path=provenance_path,
        )
        if not mechanical["passed"]:
            self._pause(self.MECHANICAL_FAILURE_REASON)
            return FinalReviewResult(
                chair_certification_status="MECHANICAL_FAILURE",
                next_phase=MeetingPhase.PAUSED,
                paused_reason=self.MECHANICAL_FAILURE_REASON,
            )

        certification_path, certification = self._chair_certify(
            provisional_draft_path=provisional_draft_path,
            provenance_path=provenance_path,
            mechanical_path=mechanical_path,
        )
        if certification.status != "CERTIFIED":
            self._pause(self.CHAIR_CORRECTION_REASON)
            return FinalReviewResult(
                chair_certification_status=certification.status,
                next_phase=MeetingPhase.PAUSED,
                paused_reason=self.CHAIR_CORRECTION_REASON,
            )

        certified_path = self._freeze_certified_resolution(
            provisional_draft_path=provisional_draft_path,
            provenance_path=provenance_path,
            certification_path=certification_path,
            certification=certification,
        )
        bundle = self._collect_librarian_reviews(
            certified_path=certified_path,
            provenance_path=provenance_path,
            certification_path=certification_path,
        )
        reviews = bundle["reviews"]
        material_count = sum(
            finding["severity"] == "MATERIAL"
            for review in reviews
            for finding in review["review"]["findings"]
        )
        reconvene_count = sum(review["review"]["reconvene_worthy"] for review in reviews)
        handoff_path = self._chair_execution_handoff_brief(
            certified_path=certified_path,
            provenance_path=provenance_path,
            certification_path=certification_path,
            epistemic_bundle=bundle,
        )
        execution_bundle_path, execution_bundle = self._collect_execution_reviews(
            certified_path=certified_path,
            provenance_path=provenance_path,
            handoff_path=handoff_path,
        )
        human_packet_path = self._freeze_human_review_packet(
            certified_path=certified_path,
            certification_path=certification_path,
            epistemic_bundle_path=self.repo.root
            / "public/final/think_tank_epistemic_reviews.json",
            handoff_path=handoff_path,
            execution_bundle_path=execution_bundle_path,
        )
        publication = FinalPublicationRunner(
            repo=self.repo,
            engine=self.engine,
            governance_docs=self.governance_docs,
            max_output_tokens=self.max_output_tokens,
        ).run(
            certified_resolution_path=certified_path,
            chair_handoff_path=handoff_path,
            epistemic_reviews_path=(
                self.repo.root / "public/final/think_tank_epistemic_reviews.json"
            ),
            execution_reviews_path=execution_bundle_path,
            human_review_packet_path=human_packet_path,
        )
        if publication.readability_status == "READABLE":
            if not publication.report_markdown_path or not publication.publication_manifest_path:
                raise ValueError("readable final publication is missing frozen artifacts")
            accountability = PostMeetingAccountabilityRunner(
                repo=self.repo,
                engine=self.engine,
                governance_docs=self.governance_docs,
                max_output_tokens=self.max_output_tokens,
            ).run(
                final_report_path=self.repo.root / publication.report_markdown_path,
                publication_manifest_path=(
                    self.repo.root / publication.publication_manifest_path
                ),
                certification_path=certification_path,
            )
            next_phase = MeetingPhase.HANDOFF_READY
            paused_reason = None
            self.engine.status.phase = next_phase
            self.engine.status.paused_reason = None
            self.engine.progress.status(
                next_phase,
                "最终出版物、会后审计申请和 Chair 述职均已冻结；Human 可进行最终处置",
            )
        else:
            accountability = None
            next_phase = MeetingPhase.PAUSED
            paused_reason = self.PUBLICATION_READABILITY_REASON
            self._pause(paused_reason)
        return FinalReviewResult(
            chair_certification_status=certification.status,
            certified_resolution_path=str(certified_path.relative_to(self.repo.root)),
            librarian_review_count=len(reviews),
            material_finding_count=material_count,
            reconvene_recommendation_count=reconvene_count,
            chair_handoff_brief_path=str(handoff_path.relative_to(self.repo.root)),
            think_tank_execution_review_path=str(
                execution_bundle_path.relative_to(self.repo.root)
            ),
            think_tank_execution_review_count=len(execution_bundle["reviews"]),
            human_review_packet_path=str(human_packet_path.relative_to(self.repo.root)),
            chair_readability_status=publication.readability_status,
            chair_readability_certification_path=(
                publication.readability_certification_path
            ),
            final_report_markdown_path=publication.report_markdown_path,
            final_report_pdf_path=publication.report_pdf_path,
            final_publication_manifest_path=publication.publication_manifest_path,
            minority_report_count=(
                accountability.minority_report_count if accountability else 0
            ),
            audit_requester_count=(
                accountability.audit_requester_count if accountability else 0
            ),
            audit_petition_count=(
                accountability.audit_petition_count if accountability else 0
            ),
            audit_petition_bundle_path=(
                accountability.audit_petition_bundle_path if accountability else None
            ),
            chair_accountability_report_path=(
                accountability.chair_accountability_report_path if accountability else None
            ),
            chair_accountability_cli_brief_path=(
                accountability.chair_accountability_cli_brief_path
                if accountability
                else None
            ),
            next_phase=next_phase,
            paused_reason=paused_reason,
        )

    def _mechanical_check(
        self,
        *,
        provisional_draft_path: Path,
        provenance_path: Path,
    ) -> tuple[Path, dict]:
        relative = Path("governance_private/finalization/mechanical_procedural_check.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return absolute, json.loads(absolute.read_text(encoding="utf-8"))

        checks: list[dict] = []
        checks.append(
            {
                "name": "EVENT_CHAIN",
                "passed": self.repo.events.verify(),
                "details": "append-only event hash chain verification",
            }
        )
        transition = json.loads(
            self.repo.docs.read_text("identity_private/general_principle/status_transition.json")
        )
        active_count = sum(value == "ACTIVE" for value in transition["representative_statuses"].values())
        required = high_threshold(self.repo, active_count)
        checks.append(
            {
                "name": "ELIGIBILITY",
                "passed": active_count > 0,
                "details": f"N_ACTIVE={active_count}",
            }
        )
        outcome_names = (
            "initial_ballot_outcomes.json",
            "type_i_second_ballot_outcomes.json",
            "type_iii_runoff_outcomes.json",
            "type_ii_second_ballot_outcomes.json",
        )
        outcome_records = []
        tally_valid = True
        threshold_valid = True
        for name in outcome_names:
            path = self.repo.root / "public/detailed_clauses" / name
            if not path.exists():
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            outcome_records.extend(record["outcomes"])
            for outcome in record["outcomes"]:
                tally_valid &= sum(outcome["tally"].values()) == active_count
                threshold_valid &= outcome["supermajority_required"] == required
        checks.extend(
            [
                {
                    "name": "BALLOT_COMPLETENESS",
                    "passed": tally_valid,
                    "details": f"each frozen tally sums to N_ACTIVE={active_count}",
                },
                {
                    "name": "THRESHOLDS",
                    "passed": threshold_valid,
                    "details": f"high threshold={high_threshold_formula(self.repo)}={required}",
                },
            ]
        )
        option_record = json.loads(
            self.repo.docs.read_text("public/detailed_clauses/option_sets.json")
        )
        expected_sets = {item["option_set_id"] for item in option_record["option_sets"]}
        winners = [item for item in outcome_records if item.get("adopted_proposal_id")]
        winner_sets = [item["option_set_id"] for item in winners]
        rejected = [
            item
            for item in outcome_records
            if item.get("status") == "TYPE_I_REJECTED_SECOND_ROUND"
        ]
        terminal_sets = [*winner_sets, *(item["option_set_id"] for item in rejected)]
        checks.append(
            {
                "name": "OPTION_SET_COVERAGE",
                "passed": (
                    len(terminal_sets) == len(set(terminal_sets))
                    and set(terminal_sets) == expected_sets
                ),
                "details": (
                    f"{len(set(terminal_sets))}/{len(expected_sets)} option sets have one terminal "
                    f"outcome ({len(winner_sets)} adopted, {len(rejected)} rejected)"
                ),
            }
        )
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        text = provisional_draft_path.read_text(encoding="utf-8")
        provenance_valid = (
            provenance["result_sha256"] == self._sha256(text)
            and provenance["applied_proposal_count"] == len(winner_sets)
            and len(provenance["applications"]) == len(winner_sets)
        )
        checks.append(
            {
                "name": "APPLICATION_AND_PROVENANCE",
                "passed": provenance_valid,
                "details": "C1 hash and one-to-one application table agree with all adopted outcomes; rejected Type I sets are not applied",
            }
        )
        record = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "active_count": active_count,
            "supermajority_required": required,
            "passed": all(item["passed"] for item in checks),
            "checks": checks,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "MECHANICAL_PROCEDURAL_CHECK_FROZEN",
            {"meeting_id": self.repo.meeting_id, "passed": record["passed"], "record_path": str(relative)},
            actor="orchestrator",
        )
        return absolute, record

    def _chair_certify(
        self,
        *,
        provisional_draft_path: Path,
        provenance_path: Path,
        mechanical_path: Path,
    ) -> tuple[Path, ChairProceduralCertification]:
        evidence_manifest = self._certification_evidence_manifest(
            provisional_draft_path=provisional_draft_path,
            provenance_path=provenance_path,
            mechanical_path=mechanical_path,
        )
        relative, version, reusable = self._certification_target(evidence_manifest)
        absolute = self.repo.root / relative
        if reusable:
            return absolute, ChairProceduralCertification.model_validate_json(
                absolute.read_text(encoding="utf-8")
            )
        manifest_relative = relative.with_name(f"{relative.stem}.evidence.json")
        manifest_absolute = self.repo.root / manifest_relative
        evidence_manifest = {
            **evidence_manifest,
            "certification_version": version,
            "certification_path": str(relative),
        }
        if not manifest_absolute.exists():
            self.repo.docs.write_once(
                manifest_relative,
                json.dumps(evidence_manifest, indent=2, ensure_ascii=False),
            )
            self.repo.events.append(
                "CHAIR_PROCEDURAL_CERTIFICATION_EVIDENCE_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "certification_version": version,
                    "certification_path": str(relative),
                    "evidence_snapshot_sha256": evidence_manifest[
                        "evidence_snapshot_sha256"
                    ],
                    "record_path": str(manifest_relative),
                },
                actor="orchestrator",
            )
        self.engine.status.phase = MeetingPhase.CHAIR_REVIEW
        self.engine.progress.status(
            MeetingPhase.CHAIR_REVIEW,
            (
                "Chair 正在基于完整冻结证据重新执行程序认证"
                if version > 1
                else "Chair 正在检查资格、票数、门槛、option-set closure、文书应用和 provenance"
            ),
        )
        resolution = provisional_draft_path.read_text(encoding="utf-8")
        resolution_sha = self._sha256(resolution)
        response = self.engine.invoke_participant(
            "CHAIR",
            system_text=self._chair_system_text(),
            user_text=(
                "Perform the mandatory procedural review of the provisional detailed-clause resolution. "
                "This is not substantive reconsideration. Check vote count, threshold, eligibility, option "
                "set closure, sequencing/backtracking, document application, rejected-content exclusion, "
                "provenance, and mandatory steps. Return exactly one JSON object with status CERTIFIED, "
                "CORRECTION_REQUIRED, or HUMAN_REQUIRED; resolution_sha256; checks; corrections; and summary. "
                "Each check has name, status PASS/FAIL, explanation, and evidence_refs. Echo the resolution "
                "hash exactly. CERTIFIED is legal only if every check passes and corrections is empty. Do not "
                "rewrite the resolution or express substantive preferences. Do not use Markdown.\n\n"
                f"CERTIFICATION VERSION: {version}\n"
                "The evidence manifest below is authoritative for which frozen procedural records are "
                "included. A later-stage frozen revision or tiebreak record supersedes the corresponding "
                "earlier proposal text or unresolved boundary for certification purposes.\n\n"
                f"RESOLUTION SHA-256: {resolution_sha}\n\nPROVISIONAL C1:\n{resolution}\n\n"
                "MECHANICAL CHECK:\n"
                + mechanical_path.read_text(encoding="utf-8")
                + "\n\nC1 PROVENANCE:\n"
                + provenance_path.read_text(encoding="utf-8")
                + "\n\nCERTIFICATION EVIDENCE MANIFEST:\n"
                + json.dumps(evidence_manifest, indent=2, ensure_ascii=False)
                + "\n\nOPTION SETS AND OUTCOMES:\n"
                + self._public_ballot_evidence()
            ),
            stage=(
                "chair_procedural_certification"
                if version == 1
                else f"chair_procedural_recertification_v{version}"
            ),
            max_output_tokens=self.max_output_tokens,
        )
        certification = self.engine.validate_structured_response(
            "CHAIR",
            response=response,
            schema_model=ChairProceduralCertification,
            stage=(
                "chair_procedural_certification"
                if version == 1
                else f"chair_procedural_recertification_v{version}"
            ),
            max_output_tokens=self.max_output_tokens,
            semantic_requirement=(
                "Review only procedural integrity; do not change substantive outcomes or the resolution text."
            ),
        )
        if certification.resolution_sha256 != resolution_sha:
            raise ValueError("Chair certification refers to the wrong resolution hash")
        self.repo.docs.write_once(relative, certification.model_dump_json(indent=2))
        self.repo.events.append(
            "CHAIR_PROCEDURAL_CERTIFICATION_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "status": certification.status,
                "resolution_sha256": resolution_sha,
                "certification_version": version,
                "evidence_snapshot_sha256": evidence_manifest[
                    "evidence_snapshot_sha256"
                ],
                "record_path": str(relative),
            },
            actor="CHAIR",
        )
        self.engine.progress.speech(
            "CHAIR", "程序终检报告（已冻结）", certification.model_dump_json(indent=2)
        )
        return absolute, certification

    def _certification_evidence_manifest(
        self,
        *,
        provisional_draft_path: Path,
        provenance_path: Path,
        mechanical_path: Path,
    ) -> dict:
        paths = [provisional_draft_path, provenance_path, mechanical_path]
        paths.extend(
            self.repo.root / relative
            for relative in self.CERTIFICATION_EVIDENCE_PATHS
            if (self.repo.root / relative).exists()
        )
        unique = {
            str(path.relative_to(self.repo.root)): path
            for path in paths
        }
        files = [
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
            for relative, path in sorted(unique.items())
        ]
        canonical = json.dumps(
            files,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "evidence_snapshot_sha256": hashlib.sha256(canonical).hexdigest(),
            "evidence_file_count": len(files),
            "evidence_files": files,
        }

    def _certification_target(self, evidence_manifest: dict) -> tuple[Path, int, bool]:
        """Reuse matching evidence, or append a new immutable certification version."""

        public_root = self.repo.root / "public/final"
        base = public_root / "chair_procedural_certification.json"
        versioned = [
            path
            for path in public_root.glob("chair_procedural_certification_v*.json")
            if re.fullmatch(
                r"chair_procedural_certification_v[0-9]+\.json", path.name
            )
        ]

        def version_of(path: Path) -> int:
            if path.name == base.name:
                return 1
            match = re.fullmatch(r"chair_procedural_certification_v([0-9]+)\.json", path.name)
            return int(match.group(1)) if match else 0

        candidates = sorted(
            (path for path in [base, *versioned] if path.exists()),
            key=version_of,
        )
        if not candidates:
            return Path("public/final/chair_procedural_certification.json"), 1, False

        latest = candidates[-1]
        latest_version = version_of(latest)
        certification = ChairProceduralCertification.model_validate_json(
            latest.read_text(encoding="utf-8")
        )
        # A frozen successful certification remains authoritative. Re-certification
        # is only for a non-certified result whose evidence packet was incomplete
        # or has since gained new immutable correction evidence.
        if certification.status == "CERTIFIED":
            return latest.relative_to(self.repo.root), latest_version, True

        sidecar = latest.with_name(f"{latest.stem}.evidence.json")
        if sidecar.exists():
            prior_manifest = json.loads(sidecar.read_text(encoding="utf-8"))
            if (
                prior_manifest.get("evidence_snapshot_sha256")
                == evidence_manifest["evidence_snapshot_sha256"]
            ):
                return latest.relative_to(self.repo.root), latest_version, True

        next_version = latest_version + 1
        return (
            Path(f"public/final/chair_procedural_certification_v{next_version}.json"),
            next_version,
            False,
        )

    def _freeze_certified_resolution(
        self,
        *,
        provisional_draft_path: Path,
        provenance_path: Path,
        certification_path: Path,
        certification: ChairProceduralCertification,
    ) -> Path:
        relative = Path("public/final/procedurally_certified_resolution.md")
        absolute = self.repo.root / relative
        text = provisional_draft_path.read_text(encoding="utf-8")
        if not absolute.exists():
            self.repo.docs.write_once(relative, text)
            metadata_relative = Path("public/final/procedurally_certified_resolution.provenance.json")
            self.repo.docs.write_once(
                metadata_relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "source_path": str(provisional_draft_path.relative_to(self.repo.root)),
                        "source_provenance_path": str(provenance_path.relative_to(self.repo.root)),
                        "certification_path": str(certification_path.relative_to(self.repo.root)),
                        "sha256": certification.resolution_sha256,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "PROCEDURALLY_CERTIFIED_RESOLUTION_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "resolution_sha256": certification.resolution_sha256,
                    "record_path": str(relative),
                    "provenance_path": str(metadata_relative),
                },
                actor="orchestrator",
            )
        elif self._sha256(absolute.read_text(encoding="utf-8")) != certification.resolution_sha256:
            raise ValueError("certified resolution does not match Chair certification")
        return absolute

    def _collect_librarian_reviews(
        self,
        *,
        certified_path: Path,
        provenance_path: Path,
        certification_path: Path,
    ) -> dict:
        bundle_relative = Path("public/final/think_tank_epistemic_reviews.json")
        bundle_path = self.repo.root / bundle_relative
        if bundle_path.exists():
            return json.loads(bundle_path.read_text(encoding="utf-8"))
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        librarians = [item for item in registry if item["runtime"]["persona"] == "librarian"]
        if not librarians:
            raise ValueError("Think Tank requires one Librarian per selected base model")
        runtimes = {
            (item["runtime"]["provider_id"], item["runtime"]["model_id"])
            for item in librarians
        }
        if len(runtimes) != len(librarians):
            raise ValueError("Think Tank Librarians must represent distinct base models")
        self.engine.status.phase = MeetingPhase.THINK_TANK_REVIEW
        self.engine.progress.status(
            MeetingPhase.THINK_TANK_REVIEW,
            f"收集 {len(librarians)} 名 Librarian 的独立知识完整性审查；全部完成前不公开",
        )
        pending_librarians = [
            record
            for record in librarians
            if not (
                self.repo.root
                / "think_tank_private/epistemic_reviews"
                / f"{record['representative_id']}.json"
            ).exists()
        ]
        register_tasks = getattr(self.engine.progress, "task_batch_started", None)
        if librarians and callable(register_tasks):
            pending_ids = {record["representative_id"] for record in pending_librarians}
            register_tasks(
                [
                    TaskProgressItem(
                        task_id=record["representative_id"],
                        participant_id=record["representative_id"],
                        provider_id=record["runtime"]["provider_id"],
                        model_id=record["runtime"]["model_id"],
                        persona=record["runtime"]["persona"],
                        initial_state=(
                            "pending"
                            if record["representative_id"] in pending_ids
                            else "completed"
                        ),
                        initial_detail=(
                            None
                            if record["representative_id"] in pending_ids
                            else "已恢复完整结果"
                        ),
                    )
                    for record in librarians
                ]
            )
        reviews = []
        for index, record in enumerate(librarians, start=1):
            representative_id = record["representative_id"]
            relative = Path("think_tank_private/epistemic_reviews") / f"{representative_id}.json"
            absolute = self.repo.root / relative
            if absolute.exists():
                review = LibrarianEpistemicReview.model_validate_json(
                    absolute.read_text(encoding="utf-8")
                )
                self.engine.progress.info(
                    f"已恢复 {index}/{len(librarians)} 份完整 Librarian 审查"
                )
            else:
                system_text = self._think_tank_system_text()
                user_text = (
                    "Independently review the procedurally certified resolution for epistemic and "
                    "transactional completeness. Identify omissions, missing definitions or assumptions, "
                    "unsupported claims, evidence/conclusion mismatches, contradictions, missing tests, "
                    "literature or benchmark gaps, provenance breaks, and cautions in the trial policies. "
                    "You have no appellate authority: do not rewrite the resolution, select rejected "
                    "proposals, trigger a revote, or claim that the meeting is automatically reopened. "
                    "Return exactly one JSON object with epistemic_status, summary, findings, "
                    "reconvene_worthy, and reconvene_reason. Each finding has severity NOTE/CAUTION/MATERIAL, "
                    "category, finding, evidence_refs, and human_attention. A reconvene recommendation is "
                    "advice to Human only. Write for a Human reader: lead with a short conclusion, "
                    "then state important findings, their evidence and practical impact, and finally "
                    "recommendations. Use clear, concise, non-bureaucratic language; do not repeat the "
                    "meeting procedure or rely on unexplained internal terms. Do not use Markdown.\n\nORIGINAL TASK:\n"
                    + self.repo.docs.read_text("public/task.json")
                    + "\n\nPROCEDURALLY CERTIFIED RESOLUTION:\n"
                    + certified_path.read_text(encoding="utf-8")
                    + "\n\nPROVENANCE:\n"
                    + provenance_path.read_text(encoding="utf-8")
                    + "\n\nCHAIR CERTIFICATION:\n"
                    + certification_path.read_text(encoding="utf-8")
                    + "\n\nRATIFIED GENERAL PRINCIPLE:\n"
                    + self.repo.docs.read_text("public/general_principle/D3.md")
                )
                response = self.engine.find_recorded_response(
                    representative_id,
                    system_text=system_text,
                    user_text=user_text,
                    stage="think_tank_epistemic_review",
                )
                if response is None:
                    response = self.engine.invoke_participant(
                        representative_id,
                        system_text=system_text,
                        user_text=user_text,
                        stage="think_tank_epistemic_review",
                        max_output_tokens=self.max_output_tokens,
                    )
                else:
                    self.engine.progress.info(
                        f"已恢复 {representative_id} 的原始 Librarian 审查响应；继续格式修复"
                    )
                review = self.engine.validate_structured_response(
                    representative_id,
                    response=response,
                    schema_model=LibrarianEpistemicReview,
                    stage="think_tank_epistemic_review",
                    max_output_tokens=self.max_output_tokens,
                    semantic_requirement=(
                        "Preserve independent epistemic findings and advisory status; never modify or "
                        "automatically reopen the certified resolution."
                    ),
                )
                self.repo.docs.write_once(relative, review.model_dump_json(indent=2))
                self.repo.events.append(
                    "THINK_TANK_EPISTEMIC_REVIEW_SUBMITTED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "reviewer_id": representative_id,
                        "record_path": str(relative),
                    },
                    actor=representative_id,
                )
            reviews.append(
                {"reviewer_id": representative_id, "review": review.model_dump(mode="json")}
            )
        bundle = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "aggregation_policy": "INDEPENDENT_REVIEWS_MECHANICALLY_BUNDLED_WITHOUT_SYNTHESIS",
            "review_count": len(reviews),
            "reviews": reviews,
            "authority_limit": "ADVISORY_ONLY_NO_AUTOMATIC_REOPEN_OR_REWRITE",
        }
        self.repo.docs.write_once(bundle_relative, json.dumps(bundle, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "THINK_TANK_EPISTEMIC_REVIEW_BUNDLE_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "review_count": len(reviews),
                "record_path": str(bundle_relative),
            },
            actor="orchestrator",
        )
        for item in reviews:
            self.engine.progress.speech(
                item["reviewer_id"],
                "Librarian 知识完整性审查（窗口已冻结）",
                json.dumps(item["review"], indent=2, ensure_ascii=False),
            )
        return bundle

    def _chair_execution_handoff_brief(
        self,
        *,
        certified_path: Path,
        provenance_path: Path,
        certification_path: Path,
        epistemic_bundle: dict,
    ) -> Path:
        relative = Path("public/final/chair_execution_handoff_brief.md")
        absolute = self.repo.root / relative
        provenance_relative = Path(
            "public/final/chair_execution_handoff_brief.provenance.json"
        )
        if absolute.exists():
            if not (self.repo.root / provenance_relative).exists():
                raise ValueError("Chair handoff brief exists without provenance")
            return absolute

        self.engine.status.phase = MeetingPhase.CHAIR_REVIEW
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.CHAIR_REVIEW,
            "Chair 正在形成面向 Human 和后续执行者的主要 execution/handoff brief",
        )
        system_text = self._handoff_chair_system_text()
        user_text = (
            "Produce the primary execution/handoff brief for Human and the eventual executor. "
            "Use the original task's primary language and a task-appropriate Markdown structure. "
            "Be concise and operational: do not restate the entire resolution or turn it into "
            "bureaucratic prose. Explain what the Assembly actually approved, how the task should "
            "be understood and handed off, binding constraints, concrete next actions, acceptance or "
            "verification points where the approved text supplies them, and the relevant provenance. "
            "Clearly identify contested, protective, trial-policy, or Human-modified portions. "
            "Report the independent Think Tank cautions as attributed advisory material; do not merge "
            "them into a false consensus, let them rewrite the certified resolution, or claim that they "
            "automatically reopen the meeting. Do not make an ACCEPT/REJECT/EXPERIMENTAL/DEFER decision "
            "for Human and do not execute the task. Return only the brief, without Markdown fences.\n\n"
            "ORIGINAL TASK:\n"
            + self.repo.docs.read_text("public/task.json")
            + "\n\nPROCEDURALLY CERTIFIED RESOLUTION:\n"
            + certified_path.read_text(encoding="utf-8")
            + "\n\nRESOLUTION PROVENANCE:\n"
            + provenance_path.read_text(encoding="utf-8")
            + "\n\nCHAIR PROCEDURAL CERTIFICATION:\n"
            + certification_path.read_text(encoding="utf-8")
            + "\n\nINDEPENDENT THINK TANK EPISTEMIC REVIEWS:\n"
            + json.dumps(epistemic_bundle, indent=2, ensure_ascii=False)
        )
        response = self.engine.find_recorded_response(
            "CHAIR",
            system_text=system_text,
            user_text=user_text,
            stage="chair_execution_handoff_brief",
        )
        if response is None:
            response = self.engine.invoke_participant(
                "CHAIR",
                system_text=system_text,
                user_text=user_text,
                stage="chair_execution_handoff_brief",
                max_output_tokens=self.max_output_tokens,
            )
        else:
            self.engine.progress.info("已恢复 Chair 的 execution/handoff brief 原始响应")
        brief = response.text.strip()
        if not brief:
            self.engine.pause_for_unconfigured_policy(
                participant_id="CHAIR",
                reason_code="EMPTY_CHAIR_EXECUTION_HANDOFF_BRIEF",
            )
        source_paths = {
            "certified_resolution": str(certified_path.relative_to(self.repo.root)),
            "resolution_provenance": str(provenance_path.relative_to(self.repo.root)),
            "chair_certification": str(certification_path.relative_to(self.repo.root)),
            "think_tank_epistemic_reviews": "public/final/think_tank_epistemic_reviews.json",
        }
        source_hashes = {
            name: self._sha256((self.repo.root / path).read_text(encoding="utf-8"))
            for name, path in source_paths.items()
        }
        rendered = brief + "\n\n---\n\n## 机器可审计来源\n\n" + "\n".join(
            f"- `{name}`: `{path}`（SHA-256 `{source_hashes[name]}`）"
            for name, path in source_paths.items()
        ) + "\n"
        self.repo.docs.write_once(relative, rendered)
        self.repo.docs.write_once(
            provenance_relative,
            json.dumps(
                {
                    "meeting_id": self.repo.meeting_id,
                    "actor": "CHAIR",
                    "brief_path": str(relative),
                    "brief_sha256": self._sha256(rendered),
                    "source_paths": source_paths,
                    "source_sha256": source_hashes,
                    "think_tank_treatment": "ATTRIBUTED_ADVISORY_MATERIAL_NOT_SYNTHESIZED_AS_CONSENSUS",
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        self.repo.events.append(
            "CHAIR_EXECUTION_HANDOFF_BRIEF_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "record_path": str(relative),
                "provenance_path": str(provenance_relative),
                "brief_sha256": self._sha256(rendered),
            },
            actor="CHAIR",
        )
        self.engine.progress.speech("CHAIR", "执行与移交主文书（已冻结）", rendered)
        return absolute

    def _collect_execution_reviews(
        self,
        *,
        certified_path: Path,
        provenance_path: Path,
        handoff_path: Path,
    ) -> tuple[Path, dict]:
        bundle_relative = Path("public/final/think_tank_execution_reviews.json")
        bundle_path = self.repo.root / bundle_relative
        if bundle_path.exists():
            return bundle_path, json.loads(bundle_path.read_text(encoding="utf-8"))
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        librarians = [item for item in registry if item["runtime"]["persona"] == "librarian"]
        if not librarians:
            raise ValueError("Think Tank execution review requires Librarians")
        runtimes = {
            (item["runtime"]["provider_id"], item["runtime"]["model_id"])
            for item in librarians
        }
        if len(runtimes) != len(librarians):
            raise ValueError("Think Tank Librarians must represent distinct base models")
        epistemic_private_root = self.repo.root / "think_tank_private/epistemic_reviews"
        self.engine.status.phase = MeetingPhase.THINK_TANK_REVIEW
        self.engine.progress.status(
            MeetingPhase.THINK_TANK_REVIEW,
            f"收集 {len(librarians)} 名 Librarian 对 Chair 交接文书的独立审查；全部完成前不公开",
        )
        pending_librarians = [
            record
            for record in librarians
            if not (
                self.repo.root
                / "think_tank_private/execution_reviews"
                / f"{record['representative_id']}.json"
            ).exists()
        ]
        register_tasks = getattr(self.engine.progress, "task_batch_started", None)
        if librarians and callable(register_tasks):
            pending_ids = {record["representative_id"] for record in pending_librarians}
            register_tasks(
                [
                    TaskProgressItem(
                        task_id=record["representative_id"],
                        participant_id=record["representative_id"],
                        provider_id=record["runtime"]["provider_id"],
                        model_id=record["runtime"]["model_id"],
                        persona=record["runtime"]["persona"],
                        initial_state=(
                            "pending"
                            if record["representative_id"] in pending_ids
                            else "completed"
                        ),
                        initial_detail=(
                            None
                            if record["representative_id"] in pending_ids
                            else "已恢复完整结果"
                        ),
                    )
                    for record in librarians
                ]
            )
        reviews = []
        for index, record in enumerate(librarians, start=1):
            representative_id = record["representative_id"]
            relative = Path("think_tank_private/execution_reviews") / f"{representative_id}.json"
            absolute = self.repo.root / relative
            if absolute.exists():
                review = LibrarianExecutionReview.model_validate_json(
                    absolute.read_text(encoding="utf-8")
                )
                self.engine.progress.info(
                    f"已恢复 {index}/{len(librarians)} 份完整 Think Tank 执行审查"
                )
            else:
                own_epistemic_path = epistemic_private_root / f"{representative_id}.json"
                if not own_epistemic_path.exists():
                    raise ValueError(
                        f"missing prior epistemic review for Librarian {representative_id}"
                    )
                system_text = self._execution_review_system_text()
                user_text = (
                    "Independently review the Chair execution/handoff brief against the procedurally "
                    "certified resolution and knowledge state. Keep the response concise. Check knowledge "
                    "completeness, missing assumptions, evidence gaps, ambiguous claims, provenance, and "
                    "task-specific execution or research cautions. Do not rewrite the Chair brief, alter "
                    "the resolution, aggregate other Librarians' positions, trigger a revote, or decide "
                    "Human's final disposition. Return exactly one JSON object with execution_readiness "
                    "(READY, READY_WITH_CAUTIONS, HUMAN_CLARIFICATION_REQUIRED, or "
                    "RECONVENE_RECOMMENDED), "
                    "summary, findings, human_decision_points, reconvene_worthy, and reconvene_reason. "
                    "Each finding has severity NOTE/CAUTION/MATERIAL; category "
                    "KNOWLEDGE_COMPLETENESS/MISSING_ASSUMPTION/EVIDENCE_GAP/AMBIGUOUS_CLAIM/"
                    "EXECUTION_CAUTION/RESEARCH_CAUTION/PROVENANCE; finding; evidence_refs; and "
                    "human_attention. A reconvene recommendation is advisory only. Write for a Human "
                    "reader in this order: short conclusion; important findings; evidence and practical "
                    "impact; recommendations. Use clear, concise, non-bureaucratic language and avoid "
                    "repeating internal procedure. Do not use Markdown."
                    "\n\nORIGINAL TASK:\n"
                    + self.repo.docs.read_text("public/task.json")
                    + "\n\nPROCEDURALLY CERTIFIED RESOLUTION:\n"
                    + certified_path.read_text(encoding="utf-8")
                    + "\n\nRESOLUTION PROVENANCE:\n"
                    + provenance_path.read_text(encoding="utf-8")
                    + "\n\nCHAIR EXECUTION/HANDOFF BRIEF:\n"
                    + handoff_path.read_text(encoding="utf-8")
                    + "\n\nYOUR PRIOR INDEPENDENT EPISTEMIC REVIEW:\n"
                    + own_epistemic_path.read_text(encoding="utf-8")
                )
                response = self.engine.find_recorded_response(
                    representative_id,
                    system_text=system_text,
                    user_text=user_text,
                    stage="think_tank_execution_review",
                )
                if response is None:
                    response = self.engine.invoke_participant(
                        representative_id,
                        system_text=system_text,
                        user_text=user_text,
                        stage="think_tank_execution_review",
                        max_output_tokens=self.max_output_tokens,
                    )
                else:
                    self.engine.progress.info(
                        f"已恢复 {representative_id} 的 Think Tank 执行审查原始响应"
                    )
                review = self.engine.validate_structured_response(
                    representative_id,
                    response=response,
                    schema_model=LibrarianExecutionReview,
                    stage="think_tank_execution_review",
                    max_output_tokens=self.max_output_tokens,
                    semantic_requirement=(
                        "Review the handoff independently and concisely; preserve advisory authority "
                        "and do not alter or automatically reopen the certified resolution."
                    ),
                )
                self.repo.docs.write_once(relative, review.model_dump_json(indent=2))
                self.repo.events.append(
                    "THINK_TANK_EXECUTION_REVIEW_SUBMITTED",
                    {
                        "meeting_id": self.repo.meeting_id,
                        "reviewer_id": representative_id,
                        "record_path": str(relative),
                    },
                    actor=representative_id,
                )
            reviews.append(
                {"reviewer_id": representative_id, "review": review.model_dump(mode="json")}
            )
        bundle = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "aggregation_policy": "INDEPENDENT_REVIEWS_MECHANICALLY_BUNDLED_WITHOUT_SYNTHESIS",
            "review_count": len(reviews),
            "reviews": reviews,
            "authority_limit": "ADVISORY_ONLY_HUMAN_CONTROLS_FINAL_DISPOSITION",
        }
        self.repo.docs.write_once(bundle_relative, json.dumps(bundle, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "THINK_TANK_EXECUTION_REVIEW_BUNDLE_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "review_count": len(reviews),
                "record_path": str(bundle_relative),
            },
            actor="orchestrator",
        )
        for item in reviews:
            self.engine.progress.speech(
                item["reviewer_id"],
                "Think Tank 执行移交审查（窗口已冻结）",
                json.dumps(item["review"], indent=2, ensure_ascii=False),
            )
        return bundle_path, bundle

    def _freeze_human_review_packet(
        self,
        *,
        certified_path: Path,
        certification_path: Path,
        epistemic_bundle_path: Path,
        handoff_path: Path,
        execution_bundle_path: Path,
    ) -> Path:
        relative = Path("public/final/human_review_packet.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            return absolute
        artifact_paths = {
            "procedurally_certified_resolution": str(certified_path.relative_to(self.repo.root)),
            "chair_procedural_certification": str(
                certification_path.relative_to(self.repo.root)
            ),
            "think_tank_epistemic_reviews": str(
                epistemic_bundle_path.relative_to(self.repo.root)
            ),
            "chair_execution_handoff_brief": str(handoff_path.relative_to(self.repo.root)),
            "think_tank_execution_reviews": str(
                execution_bundle_path.relative_to(self.repo.root)
            ),
        }
        packet = {
            "meeting_id": self.repo.meeting_id,
            "status": "HANDOFF_READY",
            "artifacts": artifact_paths,
            "artifact_sha256": {
                name: self._sha256((self.repo.root / path).read_text(encoding="utf-8"))
                for name, path in artifact_paths.items()
            },
            "human_disposition_options": ["ACCEPT", "REJECT", "EXPERIMENTAL", "DEFER"],
            "automatic_effect": "NONE_UNTIL_HUMAN_DECISION",
            "presentation_rule": "CHAIR_BRIEF_AND_THINK_TANK_REVIEWS_REMAIN_SEPARATE",
        }
        self.repo.docs.write_once(relative, json.dumps(packet, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "HUMAN_REVIEW_PACKET_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "record_path": str(relative),
                "artifact_count": len(artifact_paths),
            },
            actor="orchestrator",
        )
        return absolute

    def _public_ballot_evidence(self) -> str:
        return "\n\n".join(
            f"## {path}\n{self.repo.docs.read_text(path)}"
            for path in self.CERTIFICATION_EVIDENCE_PATHS
            if (self.repo.root / path).exists()
        )

    def _chair_system_text(self) -> str:
        paths = (
            self.governance_docs / "03_roles/chair/chair_role.md",
            self.governance_docs / "07_runtime_memory/other_participants/deliberation_chair.md",
            self.governance_docs / "02_deliberation/finalization_and_handoff.md",
        )
        return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)

    def _handoff_chair_system_text(self) -> str:
        return self._chair_system_text() + "\n\n" + (
            self.governance_docs / "06_execution_interface/execution_handoff.md"
        ).read_text(encoding="utf-8")

    def _think_tank_system_text(self) -> str:
        paths = (
            self.governance_docs / "01_constitution/common_representative_rules.md",
            self.governance_docs / "03_roles/representatives/librarian.md",
            self.governance_docs / "04_think_tank/think_tank_review.md",
            self.governance_docs / "07_runtime_memory/other_participants/think_tank_librarian.md",
            self.governance_docs / "10_open_questions/open_questions.md",
        )
        return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)

    def _execution_review_system_text(self) -> str:
        paths = (
            self.governance_docs / "02_deliberation/finalization_and_handoff.md",
            self.governance_docs / "06_execution_interface/execution_handoff.md",
        )
        return self._think_tank_system_text() + "\n\n" + "\n\n".join(
            path.read_text(encoding="utf-8") for path in paths
        )

    def _pause(self, reason: str) -> None:
        relative = Path("governance_private/finalization") / f"pause_{reason.lower()}.json"
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
        messages = {
            self.MECHANICAL_FAILURE_REASON: "机械程序检查发现不一致；不得进入 Think Tank",
            self.CHAIR_CORRECTION_REASON: "Chair 未予程序认证；须先处理程序纠正或 Human 咨询",
            self.PUBLICATION_READABILITY_REASON: (
                "Chair 认为最终出版稿仍有展示层可读性问题；不得冻结 PDF，须先修订出版模板"
            ),
        }
        self.engine.progress.status(MeetingPhase.PAUSED, f"{reason} · {messages[reason]}")

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
