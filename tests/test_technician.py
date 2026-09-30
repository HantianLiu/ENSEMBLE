from __future__ import annotations

import json

from project_ensemble.runtime.technician import compact_evidence_request


def test_technician_trims_only_evidence_and_retains_cited_items():
    original = {
        "task": "Compare the evidence without changing this instruction",
        "needed_packet_id": "RP-KEEP",
        "evidence_packets": [
            {"packet_id": "RP-KEEP", "text": "k" * 200},
            {"packet_id": "RP-LOW", "text": "l" * 200},
            {"packet_id": "RP-HIGH", "text": "h" * 200},
        ],
    }
    calls = []

    def rank(items):
        calls.append(items)
        return [0, 2, 1]

    outcome = compact_evidence_request(
        user_text=json.dumps(original),
        fits=lambda text: len(json.loads(text)["evidence_packets"]) <= 2,
        rank=rank,
    )
    assert outcome is not None
    revised, details = outcome
    parsed = json.loads(revised)
    assert parsed["task"] == original["task"]
    assert "RP-KEEP" in [item["packet_id"] for item in parsed["evidence_packets"]]
    assert details["method"] == "TECHNICIAN_RANKED_EVIDENCE_TRIM"
    assert parsed["_technician_context_compaction"]["partial_evidence_view"] is True
    assert len(calls) == 1


def test_technician_rejects_unrecognized_or_invalid_repair():
    source = json.dumps({"task": "do not trim", "unknown_documents": ["a" * 300, "b" * 300]})
    assert compact_evidence_request(user_text=source, fits=lambda _: False, rank=lambda _: []) is None
    source = json.dumps({"evidence_packets": [{"id": "A"}, {"id": "B"}]})
    assert compact_evidence_request(user_text=source, fits=lambda _: False, rank=lambda _: [0, 0]) is None
