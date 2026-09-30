from __future__ import annotations

import secrets

from project_ensemble.domain import DeliverableType, MeetingType


MEETING_ID_PREFIXES = frozenset({"M", "AU", "LR", "VR", "DL", "SR"})


def meeting_id_prefix(
    meeting_type: MeetingType, deliverable_type: DeliverableType
) -> str:
    """Choose an ID namespace from the frozen meeting purpose, not its parent."""
    if meeting_type == MeetingType.AUDIT:
        return "AU"
    if meeting_type == MeetingType.RESEARCH or deliverable_type == DeliverableType.LITERATURE_REVIEW:
        return "LR"
    if meeting_type == MeetingType.SCHOLARLY_RENDERING:
        return "SR"
    return "DL"


def meeting_id(*, prefix: str = "M") -> str:
    """Generate a new opaque ID; the default keeps older direct callers working."""
    if prefix not in MEETING_ID_PREFIXES:
        raise ValueError(f"unsupported meeting ID prefix: {prefix}")
    return prefix + "-" + secrets.token_hex(4).upper()


def representative_ids(n: int) -> list[str]:
    """Generate meeting-local opaque IDs without encoding provider/persona identity."""
    if n < 1:
        return []
    pool: set[str] = set()
    while len(pool) < n:
        pool.add("R-" + secrets.token_hex(3).upper())
    return list(pool)


def audit_member_ids(n: int) -> list[str]:
    """Generate meeting-local audit IDs that cannot be confused with Representatives."""
    if n < 1:
        return []
    pool: set[str] = set()
    while len(pool) < n:
        pool.add("AUD-" + secrets.token_hex(3).upper())
    return list(pool)
