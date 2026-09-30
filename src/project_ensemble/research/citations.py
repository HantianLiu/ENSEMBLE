from __future__ import annotations

from project_ensemble.research.models import EvidenceCitation, EvidencePacket
from project_ensemble.storage.meeting import MeetingRepository


def validate_evidence_citations(
    repo: MeetingRepository,
    citations: list[EvidenceCitation],
) -> None:
    """Fail closed unless every meeting statement citation resolves within this meeting."""

    packet_root = repo.root / "public/research/evidence_packets"
    for citation in citations:
        packet_path = packet_root / f"{citation.packet_id}.json"
        if not packet_path.is_file():
            raise ValueError(
                f"evidence citation references a packet outside this meeting: {citation.packet_id}"
            )
        packet = EvidencePacket.model_validate_json(packet_path.read_text(encoding="utf-8"))
        if packet.packet_id != citation.packet_id:
            raise ValueError("evidence packet filename and packet_id do not match")
        available_source_ids = {source.source_id for source in packet.sources}
        missing = sorted(set(citation.source_ids) - available_source_ids)
        if missing:
            raise ValueError(
                f"evidence citation references sources absent from {citation.packet_id}: "
                + ", ".join(missing)
            )
