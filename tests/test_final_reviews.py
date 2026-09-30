import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from project_ensemble.domain import GenerationResponse
from project_ensemble.orchestration.final_reviews import (
    FinalReviewRunner,
    LibrarianEpistemicReview,
    LibrarianExecutionReview,
)
from project_ensemble.storage.meeting import MeetingRepository


def test_negative_reconvene_recommendation_may_preserve_its_rationale():
    review = LibrarianEpistemicReview.model_validate(
        {
            "epistemic_status": "CAUTIONS",
            "summary": "The resolution has cautions but no material omission.",
            "findings": [],
            "reconvene_worthy": False,
            "reconvene_reason": "The cautions can be handled during execution without reopening the vote.",
        }
    )

    assert review.reconvene_worthy is False
    assert review.reconvene_reason.startswith("The cautions")


def test_positive_reconvene_recommendation_still_requires_a_reason():
    with pytest.raises(ValidationError, match="reconvene_worthy requires a reason"):
        LibrarianEpistemicReview.model_validate(
            {
                "epistemic_status": "MATERIAL_OMISSION",
                "summary": "A material omission exists.",
                "findings": [],
                "reconvene_worthy": True,
                "reconvene_reason": None,
            }
        )


def test_legacy_negative_recommendation_with_null_reason_remains_valid():
    review = LibrarianEpistemicReview.model_validate(
        {
            "epistemic_status": "COMPLETE",
            "summary": "No material epistemic gap was found.",
            "findings": [],
            "reconvene_worthy": False,
            "reconvene_reason": None,
        }
    )

    assert review.reconvene_reason is None


def test_execution_review_reconvene_status_requires_matching_boolean():
    with pytest.raises(ValidationError, match="requires reconvene_worthy=true"):
        LibrarianExecutionReview.model_validate(
            {
                "execution_readiness": "RECONVENE_RECOMMENDED",
                "summary": "Human should consider reopening the meeting.",
                "findings": [],
                "human_decision_points": [],
                "reconvene_worthy": False,
                "reconvene_reason": None,
            }
        )


class _Progress:
    def status(self, *_args):
        pass

    def speech(self, *_args):
        pass

    def info(self, *_args):
        pass


class _HandoffEngine:
    def __init__(self):
        self.status = SimpleNamespace(phase=None, paused_reason=None)
        self.progress = _Progress()
        self.calls: list[tuple[str, str, str]] = []

    def find_recorded_response(self, *_args, **_kwargs):
        return None

    def invoke_participant(self, participant_id, *, user_text, stage, **_kwargs):
        self.calls.append((participant_id, stage, user_text))
        if participant_id == "CHAIR":
            text = "# 执行移交\n\n按认证决议执行，并保留独立智库警示。"
        else:
            text = json.dumps(
                {
                    "execution_readiness": "READY_WITH_CAUTIONS",
                    "summary": f"{participant_id} review",
                    "findings": [],
                    "human_decision_points": ["Human must choose the final disposition."],
                    "reconvene_worthy": False,
                    "reconvene_reason": "Execution caution does not require a new vote.",
                }
            )
        return GenerationResponse(
            text=text,
            provider_id="fake",
            model_id="fake-model",
        )

    def validate_structured_response(self, _participant_id, *, response, schema_model, **_kwargs):
        return schema_model.model_validate_json(response.text)

    def pause_for_unconfigured_policy(self, **_kwargs):
        raise AssertionError("valid fixture output must not pause")


class _CertificationEngine:
    def __init__(self, resolution_sha256: str):
        self.resolution_sha256 = resolution_sha256
        self.status = SimpleNamespace(phase=None, paused_reason=None)
        self.progress = _Progress()
        self.calls: list[dict] = []

    def invoke_participant(self, participant_id, *, user_text, stage, **_kwargs):
        self.calls.append(
            {
                "participant_id": participant_id,
                "stage": stage,
                "user_text": user_text,
            }
        )
        return GenerationResponse(
            text=json.dumps(
                {
                    "status": "CERTIFIED",
                    "resolution_sha256": self.resolution_sha256,
                    "checks": [
                        {
                            "name": "COMPLETE_FROZEN_EVIDENCE",
                            "status": "PASS",
                            "explanation": "Later frozen records discharge the earlier boundaries.",
                            "evidence_refs": [
                                "type_i_revisions.json",
                                "type_i_second_ballot_outcomes.json",
                                "type_iii_cutoff_tiebreak_outcomes.json",
                            ],
                        }
                    ],
                    "corrections": [],
                    "summary": "The complete frozen record is procedurally certifiable.",
                }
            ),
            provider_id="fake",
            model_id="fake-model",
        )

    def validate_structured_response(
        self, _participant_id, *, response, schema_model, **_kwargs
    ):
        return schema_model.model_validate_json(response.text)


def test_noncertified_legacy_chair_report_is_recertified_with_complete_evidence(
    tmp_path,
):
    repo = MeetingRepository(tmp_path / "M-RECERT")
    repo.docs.write_once(
        "public/meeting_manifest.json", '{"meeting_id":"M-RECERT"}'
    )
    resolution = repo.docs.write_once(
        "public/detailed_clauses/C1.md", "Certified candidate text.\n"
    )
    provenance = repo.docs.write_once(
        "public/detailed_clauses/C1.provenance.json", '{"status":"FROZEN"}'
    )
    mechanical = repo.docs.write_once(
        "governance_private/finalization/mechanical_procedural_check.json",
        '{"status":"FROZEN","passed":true}',
    )
    evidence = {
        "public/detailed_clauses/option_sets.json": '{"option_sets":[]}',
        "public/detailed_clauses/type_i_revisions.json": (
            '{"status":"FROZEN","decisions":[{"final_text":"REVISED-TEXT"}]}'
        ),
        "public/detailed_clauses/type_i_second_ballot_outcomes.json": (
            '{"status":"FROZEN","outcomes":[{"tally":{"SUPPORT":11,"OPPOSE":0}}]}'
        ),
        "public/detailed_clauses/type_iii_cutoff_tiebreak_outcomes.json": (
            '{"status":"FROZEN","outcomes":[{"tally":{"A":9,"B":2}}]}'
        ),
        "governance_private/detailed_clauses/type_iii_cutoff_tiebreak_policy.json": (
            '{"policy_status":"CONFIRMED"}'
        ),
    }
    for path, content in evidence.items():
        repo.docs.write_once(path, content)
    resolution_sha = FinalReviewRunner._sha256(resolution.read_text(encoding="utf-8"))
    legacy = {
        "status": "HUMAN_REQUIRED",
        "resolution_sha256": resolution_sha,
        "checks": [
            {
                "name": "MISSING_EVIDENCE",
                "status": "FAIL",
                "explanation": "The earlier packet omitted later frozen records.",
                "evidence_refs": ["legacy packet"],
            }
        ],
        "corrections": ["Supply the omitted records."],
        "summary": "Legacy incomplete review.",
    }
    legacy_path = repo.docs.write_once(
        "public/final/chair_procedural_certification.json",
        json.dumps(legacy),
    )
    engine = _CertificationEngine(resolution_sha)
    runner = FinalReviewRunner(
        repo=repo,
        engine=engine,
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )

    certification_path, certification = runner._chair_certify(
        provisional_draft_path=resolution,
        provenance_path=provenance,
        mechanical_path=mechanical,
    )

    assert certification.status == "CERTIFIED"
    assert certification_path.name == "chair_procedural_certification_v2.json"
    assert json.loads(legacy_path.read_text(encoding="utf-8"))["status"] == "HUMAN_REQUIRED"
    assert engine.calls[0]["stage"] == "chair_procedural_recertification_v2"
    prompt = engine.calls[0]["user_text"]
    for path in evidence:
        assert path in prompt
    manifest_path = (
        repo.root / "public/final/chair_procedural_certification_v2.evidence.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["certification_version"] == 2
    assert manifest["evidence_file_count"] == len(evidence) + 3
    assert len(manifest["evidence_snapshot_sha256"]) == 64
    assert repo.events.verify()

    # The same frozen evidence reuses V2 and never spends another Chair call.
    resumed_path, resumed = runner._chair_certify(
        provisional_draft_path=resolution,
        provenance_path=provenance,
        mechanical_path=mechanical,
    )
    assert resumed_path == certification_path
    assert resumed.status == "CERTIFIED"
    assert len(engine.calls) == 1


def test_handoff_artifacts_are_recoverable_and_librarian_inputs_stay_independent(tmp_path):
    repo = MeetingRepository(tmp_path / "M-TEST")
    repo.docs.write_once(
        "public/meeting_manifest.json",
        json.dumps(
            {
                "meeting_id": "M-TEST",
                "created_at": "2026-09-18T00:00:00+00:00",
                "meeting_type": "deliberation",
                "participant_count": 2,
                "representative_count": 2,
                "research_enabled": False,
            }
        ),
    )
    repo.docs.write_once("public/task.json", '{"description":"测试任务"}')
    repo.docs.write_once("public/final/procedurally_certified_resolution.md", "认证文本\n")
    repo.docs.write_once("public/detailed_clauses/C1.provenance.json", '{"source":"votes"}')
    repo.docs.write_once("public/final/chair_procedural_certification.json", '{"status":"CERTIFIED"}')
    registry = [
        {
            "representative_id": "R-LIB-A",
            "runtime": {"provider_id": "p1", "model_id": "m1", "persona": "librarian"},
        },
        {
            "representative_id": "R-LIB-B",
            "runtime": {"provider_id": "p2", "model_id": "m2", "persona": "librarian"},
        },
    ]
    repo.docs.write_once(
        "identity_private/representative_registry.json", json.dumps(registry)
    )
    for representative_id, marker in (("R-LIB-A", "PRIVATE-A"), ("R-LIB-B", "PRIVATE-B")):
        repo.docs.write_once(
            f"think_tank_private/epistemic_reviews/{representative_id}.json",
            json.dumps(
                {
                    "epistemic_status": "CAUTIONS",
                    "summary": marker,
                    "findings": [],
                    "reconvene_worthy": False,
                    "reconvene_reason": None,
                }
            ),
        )
    epistemic_bundle = {
        "meeting_id": "M-TEST",
        "reviews": [
            {"reviewer_id": "R-LIB-A", "review": {"summary": "PUBLIC-A"}},
            {"reviewer_id": "R-LIB-B", "review": {"summary": "PUBLIC-B"}},
        ],
    }
    repo.docs.write_once(
        "public/final/think_tank_epistemic_reviews.json",
        json.dumps(epistemic_bundle),
    )
    engine = _HandoffEngine()
    governance_docs = Path(__file__).resolve().parents[1] / "docs/governance"
    runner = FinalReviewRunner(
        repo=repo,
        engine=engine,
        governance_docs=governance_docs,
    )
    certified_path = repo.root / "public/final/procedurally_certified_resolution.md"
    provenance_path = repo.root / "public/detailed_clauses/C1.provenance.json"
    certification_path = repo.root / "public/final/chair_procedural_certification.json"

    handoff_path = runner._chair_execution_handoff_brief(
        certified_path=certified_path,
        provenance_path=provenance_path,
        certification_path=certification_path,
        epistemic_bundle=epistemic_bundle,
    )
    assert "机器可审计来源" in handoff_path.read_text(encoding="utf-8")
    assert (repo.root / "public/final/chair_execution_handoff_brief.provenance.json").exists()

    # A fully frozen brief is reused without calling Chair a second time.
    runner._chair_execution_handoff_brief(
        certified_path=certified_path,
        provenance_path=provenance_path,
        certification_path=certification_path,
        epistemic_bundle=epistemic_bundle,
    )
    assert [call[0] for call in engine.calls].count("CHAIR") == 1

    execution_path, bundle = runner._collect_execution_reviews(
        certified_path=certified_path,
        provenance_path=provenance_path,
        handoff_path=handoff_path,
    )
    assert bundle["aggregation_policy"].endswith("WITHOUT_SYNTHESIS")
    assert bundle["review_count"] == 2
    prompts = {participant: text for participant, _stage, text in engine.calls if participant != "CHAIR"}
    assert "PRIVATE-A" in prompts["R-LIB-A"] and "PRIVATE-B" not in prompts["R-LIB-A"]
    assert "PRIVATE-B" in prompts["R-LIB-B"] and "PRIVATE-A" not in prompts["R-LIB-B"]

    packet_path = runner._freeze_human_review_packet(
        certified_path=certified_path,
        certification_path=certification_path,
        epistemic_bundle_path=repo.root / "public/final/think_tank_epistemic_reviews.json",
        handoff_path=handoff_path,
        execution_bundle_path=execution_path,
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["status"] == "HANDOFF_READY"
    assert packet["automatic_effect"] == "NONE_UNTIL_HUMAN_DECISION"
