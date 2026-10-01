from __future__ import annotations

"""Audited, future-only runtime replacements for a meeting.

The immutable meeting manifest remains the identity record.  A replacement is
an additional immutable record and is therefore safe to apply while resuming a
paused meeting: historical provider exchanges are never rewritten.
"""

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from project_ensemble.storage.meeting import MeetingRepository


class ModelReplacementRecord(BaseModel):
    replacement_id: str
    meeting_id: str
    participant_id: str
    from_provider_id: str
    from_model_id: str
    to_provider_id: str
    to_model_id: str
    reason: str = Field(min_length=1, max_length=2000)
    created_at: str
    record_path: str


def _base_runtime(repo: MeetingRepository, participant_id: str) -> tuple[str, str]:
    manifest_path = repo.root / "identity_private" / "meeting_manifest.json"
    if participant_id.startswith("FAST_PLANNER_") and participant_id[13:].isdigit():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        index = int(participant_id[13:]) - 1
        planners = manifest.get("fast_planner_models") or []
        if 0 <= index < len(planners):
            return str(planners[index][0]), str(planners[index][1])
        raise ValueError(f"meeting has no configured runtime for {participant_id}")
    if participant_id in {"CHAIR", "RESEARCH_DESK", "WRITER", "TECHNICIAN"}:
        if not manifest_path.exists():
            raise ValueError("meeting has no private runtime manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = {
            "CHAIR": "chair_model", "RESEARCH_DESK": "research_model", "WRITER": "writer_model",
            "TECHNICIAN": "technician_model",
        }[participant_id]
        runtime = manifest.get(key)
        if runtime is None:
            raise ValueError(f"meeting has no configured runtime for {participant_id}")
        return str(runtime[0]), str(runtime[1])

    registry = repo.root / "identity_private" / "representative_registry.json"
    if registry.exists():
        records = json.loads(registry.read_text(encoding="utf-8"))
        for record in records:
            if record.get("representative_id") == participant_id:
                runtime = record["runtime"]
                return str(runtime["provider_id"]), str(runtime["model_id"])
    audit_registry = repo.root / "identity_private" / "audit_member_registry.json"
    if audit_registry.exists():
        records = json.loads(audit_registry.read_text(encoding="utf-8"))
        for record in records:
            if record.get("audit_member_id") == participant_id:
                return str(record["provider_id"]), str(record["model_id"])
    raise ValueError(f"unknown meeting participant {participant_id}")


def _replacement_records(repo: MeetingRepository, participant_id: str | None = None) -> list[dict[str, Any]]:
    root = repo.root / "governance_private" / "model_replacements"
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in root.glob("MR-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if participant_id is None or record.get("participant_id") == participant_id:
            records.append(record)
    records.sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("replacement_id", ""))))
    return records


def current_runtime_for(repo: MeetingRepository, participant_id: str) -> tuple[str, str]:
    """Return the latest audited runtime, falling back to the frozen identity."""

    runtime = _base_runtime(repo, participant_id)
    for record in _replacement_records(repo, participant_id):
        runtime = (str(record["to_provider_id"]), str(record["to_model_id"]))
    return runtime


def has_runtime_replacement(repo: MeetingRepository, participant_id: str) -> bool:
    return bool(_replacement_records(repo, participant_id))


def latest_runtime_replacement_at_ns(repo: MeetingRepository, participant_id: str) -> int:
    """Old exchanges cannot satisfy a request made after a model replacement.

    This is a time boundary rather than a model-name check: an explicitly
    configured, one-shot fallback may legitimately answer under another model.
    """

    records = _replacement_records(repo, participant_id)
    if not records:
        return 0
    try:
        created = datetime.fromisoformat(records[-1]["created_at"])
        return int(created.timestamp()) * 1_000_000_000 + created.microsecond * 1_000
    except (KeyError, TypeError, ValueError):
        return 0


def replacement_model_pairs(repo: MeetingRepository) -> set[tuple[str, str]]:
    return {
        (str(record["to_provider_id"]), str(record["to_model_id"]))
        for record in _replacement_records(repo)
    }


def active_replacement_model_pairs(repo: MeetingRepository) -> set[tuple[str, str]]:
    """Return only replacement runtimes that participants currently use.

    Replacement records are an append-only audit trail.  Older targets remain
    useful for explaining history, but must not be treated as live models when
    preparing a resumed run (a participant may have moved through several
    providers since then).
    """

    participant_ids = {
        str(record["participant_id"])
        for record in _replacement_records(repo)
        if record.get("participant_id")
    }
    return {current_runtime_for(repo, participant_id) for participant_id in participant_ids}


def meeting_participant_ids(repo: MeetingRepository) -> list[str]:
    """List runtime-bearing identities without exposing their private metadata."""

    ids: list[str] = []
    manifest_path = repo.root / "identity_private" / "meeting_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("chair_model") is not None:
            ids.append("CHAIR")
        if manifest.get("research_enabled") and manifest.get("research_model") is not None:
            ids.append("RESEARCH_DESK")
        if manifest.get("writer_model") is not None:
            ids.append("WRITER")
        if manifest.get("technician_model") is not None:
            ids.append("TECHNICIAN")
        ids.extend(f"FAST_PLANNER_{index}" for index, _ in enumerate(
            manifest.get("fast_planner_models") or [], start=1,
        ))
    registry = repo.root / "identity_private" / "representative_registry.json"
    if registry.exists():
        ids.extend(
            str(record["representative_id"])
            for record in json.loads(registry.read_text(encoding="utf-8"))
        )
    audit_registry = repo.root / "identity_private" / "audit_member_registry.json"
    if audit_registry.exists():
        ids.extend(
            str(record["audit_member_id"])
            for record in json.loads(audit_registry.read_text(encoding="utf-8"))
        )
    return ids


class ModelReplacementService:
    def __init__(self, repo: MeetingRepository):
        self.repo = repo

    def replace(
        self,
        *,
        participant_id: str,
        provider_id: str,
        model_id: str,
        reason: str,
    ) -> ModelReplacementRecord:
        participant_id = participant_id.strip()
        provider_id = provider_id.strip()
        model_id = model_id.strip()
        reason = reason.strip()
        if not participant_id:
            raise ValueError("participant ID cannot be empty")
        if not provider_id or not model_id:
            raise ValueError("replacement runtime must include provider and model")
        if not reason:
            raise ValueError("a reason is required for an audited model replacement")
        from_provider_id, from_model_id = current_runtime_for(self.repo, participant_id)
        if (from_provider_id, from_model_id) == (provider_id, model_id):
            raise ValueError("replacement runtime is identical to the participant's current runtime")

        replacement_id = "MR-" + secrets.token_hex(6).upper()
        created_at = datetime.now(timezone.utc).isoformat()
        private_relative = Path("governance_private/model_replacements") / f"{replacement_id}.json"
        public_relative = Path("public/model_replacements") / f"{replacement_id}.json"
        payload = {
            "replacement_id": replacement_id,
            "meeting_id": self.repo.meeting_id,
            "participant_id": participant_id,
            "from_provider_id": from_provider_id,
            "from_model_id": from_model_id,
            "to_provider_id": provider_id,
            "to_model_id": model_id,
            "reason": reason,
            "created_at": created_at,
            "record_path": str(private_relative),
        }
        self.repo.docs.write_once(private_relative, json.dumps(payload, indent=2, ensure_ascii=False))
        # The public record intentionally excludes the Human's rationale, but
        # exposes the runtime transition so the meeting's execution history is
        # interpretable without opening governance-private material.
        self.repo.docs.write_once(
            public_relative,
            json.dumps(
                {
                    key: payload[key]
                    for key in (
                        "replacement_id",
                        "meeting_id",
                        "participant_id",
                        "from_provider_id",
                        "from_model_id",
                        "to_provider_id",
                        "to_model_id",
                        "created_at",
                    )
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        self.repo.events.append(
            "MODEL_RUNTIME_REPLACED",
            {
                "meeting_id": self.repo.meeting_id,
                "replacement_id": replacement_id,
                "participant_id": participant_id,
                "from_provider_id": from_provider_id,
                "from_model_id": from_model_id,
                "to_provider_id": provider_id,
                "to_model_id": model_id,
                "record_path": str(private_relative),
                "public_record_path": str(public_relative),
                "effective": "FUTURE_INVOCATIONS_ONLY",
            },
            actor="HUMAN",
        )
        return ModelReplacementRecord(**payload)

    def replace_all_using(
        self,
        *,
        from_provider_id: str,
        from_model_id: str,
        provider_id: str,
        model_id: str,
        reason: str,
    ) -> list[ModelReplacementRecord]:
        source = (from_provider_id.strip(), from_model_id.strip())
        targets = [
            participant_id
            for participant_id in meeting_participant_ids(self.repo)
            if current_runtime_for(self.repo, participant_id) == source
        ]
        if not targets:
            raise ValueError(
                f"no active participant currently uses {source[0]}:{source[1]}"
            )
        return [
            self.replace(
                participant_id=participant_id,
                provider_id=provider_id,
                model_id=model_id,
                reason=reason,
            )
            for participant_id in targets
        ]
