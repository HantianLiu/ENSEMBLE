from __future__ import annotations

from pydantic import BaseModel
from project_ensemble.domain import MeetingPhase
from project_ensemble.errors import RepresentativeUnavailableError


class MeetingStatus(BaseModel):
    phase: MeetingPhase = MeetingPhase.INIT
    paused_reason: str | None = None
    unavailable_representative: str | None = None

    def pause_for_unavailable(self, representative_id: str, exc: RepresentativeUnavailableError) -> None:
        self.phase = MeetingPhase.PAUSED
        self.unavailable_representative = representative_id
        self.paused_reason = str(exc)
