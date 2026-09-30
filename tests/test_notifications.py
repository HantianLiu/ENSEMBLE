import json

import pytest

from project_ensemble.config import EmailNotificationConfig
from project_ensemble.domain import MeetingType
from project_ensemble.errors import NotificationDeliveryError
from project_ensemble.notifications.email import (
    EmailNotifier,
    SlurmEmailNotifier,
    build_email_notifier,
    validate_email_address,
)
from project_ensemble.orchestration.escalation import HumanEscalationService
from project_ensemble.storage.meeting import MeetingRepository


class FakeSMTP:
    instances = []

    def __init__(self, *args, **kwargs):
        self.messages = []
        self.started_tls = False
        self.logins = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self):
        pass

    def starttls(self, **kwargs):
        self.started_tls = True

    def login(self, username, password):
        self.logins.append((username, password))

    def send_message(self, message):
        self.messages.append(message)


def repo(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    return MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        meeting_type=MeetingType.DELIBERATION,
        task_description="sensitive task",
        escalation_email="human@example.test",
    )


def notifier():
    return EmailNotifier(
        EmailNotificationConfig(
            enabled=True,
            host="smtp.example.test",
            from_address="ensemble@example.test",
        )
    )


def test_email_validation_rejects_header_injection():
    with pytest.raises(ValueError):
        validate_email_address("person@example.test\nBcc: attacker@example.test")


def test_notification_rejects_untrusted_receipt_path(tmp_path):
    meeting = repo(tmp_path)
    with pytest.raises(ValueError, match="event hash"):
        notifier().send_escalation(
            repo=meeting,
            source_event={"event_hash": "../../escape"},
            recipient="human@example.test",
            reason_code="FAILURE",
            summary="Needs attention",
        )


def test_notification_is_minimal_and_deduplicated(monkeypatch, tmp_path):
    FakeSMTP.instances.clear()
    monkeypatch.setattr("project_ensemble.notifications.email.smtplib.SMTP", FakeSMTP)
    meeting = repo(tmp_path)
    event = meeting.events.append(
        "HUMAN_INTERVENTION_REQUIRED",
        {"meeting_id": meeting.meeting_id, "reason_code": "POLICY_NOT_CONFIGURED"},
        actor="orchestrator",
    )
    n = notifier()
    assert n.send_escalation(
        repo=meeting,
        source_event=event,
        recipient="human@example.test",
        reason_code="POLICY_NOT_CONFIGURED",
        summary="Human must select an explicit policy.",
    )
    assert not n.send_escalation(
        repo=meeting,
        source_event=event,
        recipient="human@example.test",
        reason_code="POLICY_NOT_CONFIGURED",
        summary="Human must select an explicit policy.",
    )
    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.started_tls
    message = smtp.messages[0]
    body = message.get_content()
    assert "sensitive task" not in body
    assert "POLICY_NOT_CONFIGURED" in body
    assert meeting.events.verify()


def test_delivery_failure_is_persisted(monkeypatch, tmp_path):
    class FailingSMTP(FakeSMTP):
        def send_message(self, message):
            raise OSError("network unavailable and secret details")

    monkeypatch.setattr("project_ensemble.notifications.email.smtplib.SMTP", FailingSMTP)
    meeting = repo(tmp_path)
    event = meeting.events.append("HUMAN_INTERVENTION_REQUIRED", {}, actor="orchestrator")
    with pytest.raises(NotificationDeliveryError):
        notifier().send_escalation(
            repo=meeting,
            source_event=event,
            recipient="human@example.test",
            reason_code="FAILURE",
            summary="Needs attention",
        )
    events = [json.loads(line) for line in meeting.events.path.read_text().splitlines()]
    assert events[-1]["event_type"] == "NOTIFICATION_FAILED"
    assert "network unavailable" not in json.dumps(events[-1])
    assert meeting.events.verify()


def test_slurm_notification_submits_minimal_mail_job(monkeypatch, tmp_path):
    class Completed:
        stdout = b"123456;cluster\n"

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("project_ensemble.notifications.email.shutil.which", lambda command: "/usr/bin/sbatch")
    monkeypatch.setattr("project_ensemble.notifications.email.subprocess.run", fake_run)
    meeting = repo(tmp_path)
    event = meeting.events.append("HUMAN_INTERVENTION_REQUIRED", {}, actor="orchestrator")
    notification = build_email_notifier(
        EmailNotificationConfig(enabled=True, transport="slurm", slurm_sbatch_command="sbatch")
    )
    assert isinstance(notification, SlurmEmailNotifier)
    assert notification.send_escalation(
        repo=meeting,
        source_event=event,
        recipient="human@example.test",
        reason_code="POLICY_NOT_CONFIGURED",
        summary="Needs attention",
    )
    assert "--mail-user=human@example.test" in captured["command"]
    assert "--mail-type=END,FAIL" in captured["command"]
    assert captured["kwargs"]["input"] == b"#!/bin/sh\nexit 0\n"
    receipt = json.loads(
        (meeting.root / "governance_private/notifications" / f"{event['event_hash']}.sent.json").read_text()
    )
    assert receipt["transport"] == "slurm"
    assert receipt["slurm_job_id"] == "123456"
    assert meeting.events.verify()


def test_missing_email_and_disabled_transport_are_audited_as_skipped(tmp_path):
    meeting = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=tmp_path,
        meeting_type=MeetingType.DELIBERATION,
        task_description="task",
        escalation_email=None,
    )
    disabled = build_email_notifier(EmailNotificationConfig(enabled=False))
    event = HumanEscalationService(meeting, disabled).request(reason_code="NEEDS_HUMAN", summary="Choose")
    events = [json.loads(line) for line in meeting.events.path.read_text().splitlines()]
    assert events[-1]["event_type"] == "NOTIFICATION_SKIPPED"
    assert events[-1]["payload"]["source_event_hash"] == event["event_hash"]
    assert meeting.events.verify()
