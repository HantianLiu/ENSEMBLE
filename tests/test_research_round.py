import pytest
from pydantic import ValidationError

from project_ensemble.research.models import ResearchRoundDedupGroup
from project_ensemble.research.models import ResearchRoundReleaseGate, ResearchRoundSubmission


def test_research_round_allows_no_request_or_up_to_four_claims():
    assert ResearchRoundSubmission().claims == []
    claims = [f"claim {index}" for index in range(4)]
    assert ResearchRoundSubmission(claims=claims).claims == claims


def test_research_round_rejects_more_than_four_or_duplicate_claims():
    with pytest.raises(ValidationError):
        ResearchRoundSubmission(claims=[f"claim {index}" for index in range(5)])
    with pytest.raises(ValidationError, match="must be unique"):
        ResearchRoundSubmission(claims=["same claim", "same claim"])


def test_research_round_release_gate_blocks_partial_or_failed_batch():
    incomplete = {
        "expected_claim_ids": ["C-1", "C-2"],
        "resolutions": [
            {"claim_id": "C-1", "status": "STAGED_PACKET", "packet_id": "RP-1"},
            {"claim_id": "C-2", "status": "FAILED"},
        ],
        "released": True,
        "published_packet_ids": ["RP-1"],
        "literature_bundle_published": True,
    }
    with pytest.raises(ValidationError, match="cannot release"):
        ResearchRoundReleaseGate.model_validate(incomplete)


def test_research_round_release_gate_allows_atomic_batch_and_rejection():
    gate = ResearchRoundReleaseGate.model_validate(
        {
            "expected_claim_ids": ["C-1", "C-2"],
            "resolutions": [
                {"claim_id": "C-1", "status": "STAGED_PACKET", "packet_id": "RP-1"},
                {"claim_id": "C-2", "status": "REJECTED_NON_RESEARCHABLE"},
            ],
            "released": True,
            "published_packet_ids": ["RP-1"],
            "staged_document_ids": ["DOC-SHA256-1"],
            "published_document_ids": ["DOC-SHA256-1"],
            "literature_bundle_published": True,
        }
    )
    assert gate.released is True


def test_research_round_release_gate_allows_nonblocking_qc_failure():
    gate = ResearchRoundReleaseGate.model_validate(
        {
            "expected_claim_ids": ["C-1"],
            "resolutions": [
                {
                    "claim_id": "C-1",
                    "status": "QC_FAILED",
                    "failure_code": "RESEARCH_SYNTHESIS_SCHEMA_INVALID",
                    "failure_summary": "Evidence synthesis remained invalid after repair.",
                }
            ],
            "released": True,
            "published_packet_ids": [],
            "staged_document_ids": [],
            "published_document_ids": [],
            "literature_bundle_published": True,
        }
    )
    assert gate.resolutions[0].status.value == "QC_FAILED"


def test_research_round_release_gate_blocks_early_document_publication():
    with pytest.raises(ValidationError, match="cannot publish packets or documents"):
        ResearchRoundReleaseGate.model_validate(
            {
                "expected_claim_ids": ["C-1"],
                "resolutions": [{"claim_id": "C-1", "status": "PENDING"}],
                "staged_document_ids": ["DOC-SHA256-1"],
                "released": False,
                "published_document_ids": ["DOC-SHA256-1"],
                "literature_bundle_published": False,
            }
        )


def test_exact_fingerprint_dedup_group_keeps_origins_audit_private():
    group = ResearchRoundDedupGroup.model_validate(
        {
            "group_id": "RG-1",
            "claim_fingerprint": "a" * 64,
            "normalized_claim": "Claim C holds under scope S.",
            "origins": [
                {"request_id": "RQ-1", "requester_id": "R-1"},
                {"request_id": "RQ-2", "requester_id": "R-2"},
            ],
            "packet_id": "RP-1",
        }
    )
    assert len(group.origins) == 2
    assert group.claim_fingerprint == "a" * 64


def test_dedup_group_rejects_duplicate_request_mapping():
    with pytest.raises(ValidationError, match="request IDs must be unique"):
        ResearchRoundDedupGroup.model_validate(
            {
                "group_id": "RG-1",
                "claim_fingerprint": "b" * 64,
                "normalized_claim": "Claim C holds under scope S.",
                "origins": [
                    {"request_id": "RQ-1", "requester_id": "R-1"},
                    {"request_id": "RQ-1", "requester_id": "R-2"},
                ],
            }
        )
