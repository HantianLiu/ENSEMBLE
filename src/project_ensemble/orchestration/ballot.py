from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field

from project_ensemble.errors import BallotNotClosedError, InvalidBallotError, PolicyNotConfiguredError


class BallotStatus(str, Enum):
    OPEN = "OPEN"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class SealedBallot(BaseModel):
    ballot_id: str
    eligible_representatives: set[str]
    options: tuple[str, ...]
    status: BallotStatus = BallotStatus.OPEN
    _votes: dict[str, str] = {}

    model_config = {"arbitrary_types_allowed": True}

    def model_post_init(self, __context) -> None:
        object.__setattr__(self, "_votes", {})
        if len(self.options) < 1:
            raise ValueError("ballot must have at least one option")
        if any(opt.upper() == "ABSTAIN" for opt in self.options):
            raise ValueError("abstention is not an allowed option")

    def submit(self, representative_id: str, option: str) -> None:
        if self.status != BallotStatus.OPEN:
            raise InvalidBallotError("ballot is not open")
        if representative_id not in self.eligible_representatives:
            raise InvalidBallotError("representative is not eligible")
        if representative_id in self._votes:
            raise InvalidBallotError("representative already submitted a ballot")
        if option not in self.options:
            raise InvalidBallotError("option is not on the ballot")
        self._votes[representative_id] = option

    @property
    def submitted_count(self) -> int:
        return len(self._votes)

    def pause(self) -> None:
        if self.status == BallotStatus.OPEN:
            self.status = BallotStatus.PAUSED

    def resume(self, *, partial_ballot_policy: str | None = None) -> None:
        if self.status != BallotStatus.PAUSED:
            raise InvalidBallotError("ballot is not paused")
        if self._votes and partial_ballot_policy is None:
            raise PolicyNotConfiguredError("mid-ballot recovery policy is unresolved")
        if partial_ballot_policy not in {None, "retain"}:
            raise PolicyNotConfiguredError("only an explicitly configured recovery policy may be used")
        self.status = BallotStatus.OPEN

    def close(self) -> None:
        if self.status != BallotStatus.OPEN:
            raise InvalidBallotError("ballot is not open")
        missing = self.eligible_representatives - set(self._votes)
        if missing:
            raise InvalidBallotError(f"cannot close ballot with missing votes: {sorted(missing)}")
        self.status = BallotStatus.CLOSED

    def tally(self) -> dict[str, int]:
        if self.status != BallotStatus.CLOSED:
            raise BallotNotClosedError("partial tally disclosure is prohibited")
        counts = {opt: 0 for opt in self.options}
        for vote in self._votes.values():
            counts[vote] += 1
        return counts

    def sealed_export(self) -> dict:
        """Governance-private export; never Representative-visible before close."""
        return {"ballot_id": self.ballot_id, "status": self.status.value, "votes": dict(self._votes)}
