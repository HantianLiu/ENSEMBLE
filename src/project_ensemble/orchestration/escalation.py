from __future__ import annotations

from project_ensemble.errors import RepresentativeUnavailableError
from project_ensemble.notifications.email import EmailNotifier
from project_ensemble.storage.meeting import MeetingRepository


class HumanEscalationService:
    """Persist the decision point before attempting its out-of-band notification."""

    def __init__(self, repo: MeetingRepository, notifier: EmailNotifier):
        self.repo = repo
        self.notifier = notifier

    def request(self, *, reason_code: str, summary: str, related_event_hash: str | None = None) -> dict:
        payload = {
            "meeting_id": self.repo.meeting_id,
            "reason_code": reason_code,
            "summary": summary,
            "related_event_hash": related_event_hash,
        }
        event = self.repo.events.append("HUMAN_INTERVENTION_REQUIRED", payload, actor="orchestrator")
        recipient = self.repo.escalation_email()
        if recipient is None or not getattr(self.notifier, "enabled", True):
            self.repo.events.append(
                "NOTIFICATION_SKIPPED",
                {
                    "source_event_hash": event["event_hash"],
                    "reason": "recipient_or_transport_not_configured",
                },
                actor="orchestrator",
            )
            return event
        self.notifier.send_escalation(
            repo=self.repo,
            source_event=event,
            recipient=recipient,
            reason_code=reason_code,
            summary=summary,
        )
        return event

    def representative_unavailable(self, representative_id: str, exc: RepresentativeUnavailableError) -> None:
        pause_event = self.repo.events.append(
            "MEETING_PAUSED",
            {
                "meeting_id": self.repo.meeting_id,
                "reason_code": "REPRESENTATIVE_UNAVAILABLE",
                "representative_id": representative_id,
            },
            actor="orchestrator",
        )
        self.request(
            reason_code="REPRESENTATIVE_UNAVAILABLE",
            summary=f"Representative {representative_id} remained unavailable after the configured retries.",
            related_event_hash=pause_event["event_hash"],
        )

    def meeting_failure(self, *, reason_code: str, summary: str) -> None:
        failure_event = self.repo.events.append(
            "MEETING_FAILED",
            {
                "meeting_id": self.repo.meeting_id,
                "reason_code": reason_code,
            },
            actor="orchestrator",
        )
        self.request(
            reason_code=reason_code,
            summary=summary,
            related_event_hash=failure_event["event_hash"],
        )
