from __future__ import annotations

import json
import os
import re
import shutil
import smtplib
import ssl
import subprocess
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from project_ensemble.config import EmailNotificationConfig
from project_ensemble.errors import NotificationConfigurationError, NotificationDeliveryError
from project_ensemble.storage.meeting import MeetingRepository


_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def validate_email_address(value: str) -> str:
    value = value.strip()
    if not value or "\r" in value or "\n" in value or len(value) > 254 or not _EMAIL_RE.fullmatch(value):
        raise ValueError("enter one valid email address without a display name")
    local, domain = value.rsplit("@", 1)
    labels = domain.split(".")
    if (
        len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or len(labels) < 2
        or ".." in value
        or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels)
    ):
        raise ValueError("enter one valid email address without a display name")
    return value


class EmailNotifier:
    """SMTP transport for minimal, non-substantive human escalation messages."""

    def __init__(self, config: EmailNotificationConfig):
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def transport_name(self) -> str:
        return "smtp"

    def configuration_problems(self) -> list[str]:
        cfg = self.config
        problems: list[str] = []
        if not cfg.enabled:
            problems.append("email notifications are disabled")
        if not cfg.host:
            problems.append("SMTP host is not configured")
        if not cfg.from_address:
            problems.append("SMTP from_address is not configured")
        else:
            try:
                validate_email_address(cfg.from_address)
            except ValueError as exc:
                problems.append(f"SMTP from_address is invalid: {exc}")
        for label, env_name in (("username", cfg.username_env), ("password", cfg.password_env)):
            if env_name and not os.environ.get(env_name):
                problems.append(f"SMTP {label} environment variable {env_name} is missing")
        if bool(cfg.username_env) != bool(cfg.password_env):
            problems.append("SMTP username_env and password_env must be configured together")
        if "\r" in cfg.subject_prefix or "\n" in cfg.subject_prefix:
            problems.append("email subject_prefix contains a forbidden line break")
        return problems

    def assert_ready(self) -> None:
        problems = self.configuration_problems()
        if problems:
            raise NotificationConfigurationError("; ".join(problems))

    def _success_receipt(self, repo: MeetingRepository, source_hash: str) -> Path:
        return repo.root / "governance_private" / "notifications" / f"{source_hash}.sent.json"

    def send_escalation(
        self,
        *,
        repo: MeetingRepository,
        source_event: dict[str, Any],
        recipient: str,
        reason_code: str,
        summary: str,
    ) -> bool:
        """Send once per source event hash; return False when already delivered."""
        self.assert_ready()
        recipient = validate_email_address(recipient)
        source_hash = str(source_event["event_hash"])
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("source event hash is invalid")
        if self._success_receipt(repo, source_hash).exists():
            return False

        safe_reason = " ".join(reason_code.split())[:80] or "HUMAN_INTERVENTION_REQUIRED"
        safe_summary = " ".join(summary.split())[:1000]
        message = EmailMessage()
        if self.config.from_address:
            message["From"] = self.config.from_address
        message["To"] = recipient
        message["Subject"] = f"{self.config.subject_prefix} {repo.meeting_id}: {safe_reason}"
        message.set_content(
            "Project ENSEMBLE requires human attention.\n\n"
            f"Meeting: {repo.meeting_id}\n"
            f"Reason: {safe_reason}\n"
            f"Summary: {safe_summary}\n\n"
            "No ballot contents, model identities, prompts, or hidden governance data are included.\n"
            f"Workspace: {repo.root}\n"
        )

        try:
            delivery_metadata = self._send(message)
        except Exception as exc:
            failure = {
                "source_event_hash": source_hash,
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
            }
            failure_name = f"{source_hash}.failed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
            repo.docs.write_once(
                Path("governance_private/notifications") / failure_name,
                json.dumps(failure, indent=2),
            )
            repo.events.append("NOTIFICATION_FAILED", failure, actor="orchestrator")
            raise NotificationDeliveryError(
                f"meeting {repo.meeting_id} is paused, but its escalation email could not be delivered"
            ) from exc

        receipt = {
            "source_event_hash": source_hash,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "transport": self.transport_name,
            **delivery_metadata,
        }
        repo.docs.write_once(
            Path("governance_private/notifications") / f"{source_hash}.sent.json",
            json.dumps(receipt, indent=2),
        )
        repo.events.append("NOTIFICATION_SENT", receipt, actor="orchestrator")
        return True

    def _send(self, message: EmailMessage) -> dict[str, Any]:
        cfg = self.config
        username = os.environ.get(cfg.username_env) if cfg.username_env else None
        password = os.environ.get(cfg.password_env) if cfg.password_env else None
        context = ssl.create_default_context()
        if cfg.security == "ssl":
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout_seconds, context=context) as smtp:
                if username is not None and password is not None:
                    smtp.login(username, password)
                smtp.send_message(message)
            return {}
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_seconds) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if username is not None and password is not None:
                    smtp.login(username, password)
                smtp.send_message(message)
            return {}


class SlurmEmailNotifier(EmailNotifier):
    """Trigger cluster-managed email by submitting a minimal Slurm job."""

    @property
    def transport_name(self) -> str:
        return "slurm"

    def configuration_problems(self) -> list[str]:
        cfg = self.config
        problems: list[str] = []
        if not cfg.enabled:
            problems.append("email notifications are disabled")
        if not cfg.slurm_sbatch_command:
            problems.append("Slurm sbatch command is not configured")
        elif shutil.which(cfg.slurm_sbatch_command) is None and not Path(cfg.slurm_sbatch_command).is_file():
            problems.append(f"Slurm sbatch command was not found: {cfg.slurm_sbatch_command}")
        if not cfg.slurm_time_limit:
            problems.append("Slurm notification time limit is not configured")
        return problems

    def _send(self, message: EmailMessage) -> dict[str, Any]:
        recipient = validate_email_address(str(message["To"]))
        subject = str(message["Subject"])
        job_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", subject).strip("-")[:120] or "ENSEMBLE-alert"
        cfg = self.config
        command = [
            cfg.slurm_sbatch_command,
            "--parsable",
            f"--job-name={job_name}",
            f"--mail-user={recipient}",
            "--mail-type=END,FAIL",
            f"--time={cfg.slurm_time_limit}",
            "--ntasks=1",
            "--cpus-per-task=1",
            "--mem=16M",
            "--output=/dev/null",
            "--error=/dev/null",
            "--export=NONE",
        ]
        for flag, value in (
            ("--partition", cfg.slurm_partition),
            ("--account", cfg.slurm_account),
            ("--qos", cfg.slurm_qos),
        ):
            if value:
                command.append(f"{flag}={value}")
        completed = subprocess.run(
            command,
            input=b"#!/bin/sh\nexit 0\n",
            capture_output=True,
            check=True,
            timeout=cfg.timeout_seconds,
        )
        job_id = completed.stdout.decode("utf-8", errors="replace").strip().split(";", 1)[0]
        if not job_id.isdigit():
            raise RuntimeError("sbatch did not return a numeric job ID")
        return {"slurm_job_id": job_id}


def build_email_notifier(config: EmailNotificationConfig) -> EmailNotifier:
    if config.transport == "slurm":
        return SlurmEmailNotifier(config)
    return EmailNotifier(config)
