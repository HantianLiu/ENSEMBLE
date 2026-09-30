from __future__ import annotations

from enum import Enum
from project_ensemble.errors import BallotNotClosedError, InvalidBallotError


class CosponsorshipStatus(str, Enum):
    OPEN = "OPEN"
    FROZEN = "FROZEN"


class SealedCosponsorshipLedger:
    """Governance-private pre-ballot support ledger.

    Co-sponsorship is hidden from other Representatives until the relevant ballot is closed.
    Conflicting co-sponsorship is recorded, not blocked here; Audit may inspect it later.
    """

    def __init__(self, eligible_representatives: set[str], item_ids: set[str]):
        self.eligible_representatives = set(eligible_representatives)
        self.item_ids = set(item_ids)
        self.status = CosponsorshipStatus.OPEN
        self._support: dict[str, set[str]] = {item: set() for item in item_ids}

    def add(self, representative_id: str, item_id: str) -> None:
        if self.status != CosponsorshipStatus.OPEN:
            raise InvalidBallotError("co-sponsorship window is frozen")
        if representative_id not in self.eligible_representatives:
            raise InvalidBallotError("representative is not eligible")
        if item_id not in self.item_ids:
            raise InvalidBallotError("unknown proposal/amendment")
        self._support[item_id].add(representative_id)

    def freeze(self) -> None:
        self.status = CosponsorshipStatus.FROZEN

    def reveal(self, *, ballot_closed: bool) -> dict[str, set[str]]:
        if self.status != CosponsorshipStatus.FROZEN or not ballot_closed:
            raise BallotNotClosedError("co-sponsorship disclosure before ballot close is prohibited")
        return {item: set(reps) for item, reps in self._support.items()}

    def governance_private_snapshot(self) -> dict[str, list[str]]:
        return {item: sorted(reps) for item, reps in self._support.items()}
