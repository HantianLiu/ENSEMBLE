from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from project_ensemble.research.models import (
    CacheInvalidationAuthority,
    CacheInvalidationReason,
    EvidencePacket,
    ResearchCacheInvalidation,
)
from project_ensemble.storage.meeting import MeetingRepository


class ResearchPacketCache:
    """Meeting-local, immutable invalidation ledger for public evidence packets."""

    def __init__(self, repo: MeetingRepository):
        self.repo = repo

    def invalidate(
        self,
        *,
        packet_id: str,
        authority: CacheInvalidationAuthority,
        reason: CacheInvalidationReason,
        rationale: str,
        evidence_url: str | None = None,
    ) -> ResearchCacheInvalidation:
        packet_path = self.repo.root / "public/research/evidence_packets" / f"{packet_id}.json"
        if not packet_path.is_file():
            raise ValueError(f"cannot invalidate a packet outside this meeting: {packet_id}")
        packet = EvidencePacket.model_validate_json(packet_path.read_text(encoding="utf-8"))
        if packet.packet_id != packet_id:
            raise ValueError("evidence packet filename and packet_id do not match")
        record = ResearchCacheInvalidation(
            packet_id=packet_id,
            invalidated_at=datetime.now(timezone.utc),
            authority=authority,
            reason=reason,
            rationale=rationale,
            evidence_url=evidence_url,
        )
        relative = Path("public/research/cache_invalidations") / f"{packet_id}.json"
        self.repo.docs.write_once(relative, record.model_dump_json(indent=2))
        self.repo.events.append(
            "RESEARCH_CACHE_INVALIDATED",
            {
                "meeting_id": self.repo.meeting_id,
                "packet_id": packet_id,
                "authority": authority.value,
                "reason": reason.value,
                "evidence_url": evidence_url,
                "invalidation_path": str(relative),
            },
            actor=authority.value,
        )
        return record

    def is_invalidated(self, packet_id: str) -> bool:
        return (
            self.repo.root / "public/research/cache_invalidations" / f"{packet_id}.json"
        ).is_file()
