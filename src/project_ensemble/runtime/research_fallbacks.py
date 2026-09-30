"""Audited, request-scoped and meeting-scoped Research Desk fallback choices."""

from __future__ import annotations

import json
import secrets
import fcntl
from datetime import datetime, timezone
from pathlib import Path


class ResearchFallbacks:
    def __init__(self, repo):
        self.repo = repo
        self.root = Path("governance_private/research_fallbacks")

    def choose(
        self, *, request_id: str, source: tuple[str, str], target: tuple[str, str],
        scope: str, reason: str,
    ) -> dict:
        if scope not in {"REQUEST_ONLY", "NEXT_FAILURE_ONLY", "ON_FUTURE_FAILURES"}:
            raise ValueError("invalid Research Desk fallback scope")
        if source == target:
            raise ValueError("Research Desk fallback must use a different model")
        record_id = "RF-" + secrets.token_hex(6).upper()
        record = {
            "record_id": record_id,
            "meeting_id": self.repo.meeting_id,
            "request_id": request_id,
            "source": list(source), "target": list(target),
            "scope": scope, "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        relative = self.root / f"{record_id}.json"
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "RESEARCH_DESK_FALLBACK_CHOSEN",
            {key: record[key] for key in ("record_id", "meeting_id", "request_id", "source", "target", "scope")},
            actor="HUMAN",
        )
        return record

    def _records(self) -> list[dict]:
        directory = self.repo.root / self.root
        if not directory.is_dir():
            return []
        records = [json.loads(path.read_text(encoding="utf-8"))
                   for path in directory.glob("RF-*.json")
                   if not path.name.endswith(".used.json")]
        return sorted(records, key=lambda item: (item["created_at"], item["record_id"]))

    def request_target(self, request_id: str) -> tuple[str, str] | None:
        for record in reversed(self._records()):
            if record["scope"] == "REQUEST_ONLY" and record["request_id"] == request_id:
                return tuple(record["target"])
        return None

    def failure_target(self, source: tuple[str, str]) -> tuple[str, str] | None:
        for record in reversed(self._records()):
            if record["scope"] == "ON_FUTURE_FAILURES" and tuple(record["source"]) == source:
                return tuple(record["target"])
        return None

    def take_next_failure_target(self, source: tuple[str, str]) -> tuple[str, str] | None:
        """Atomically consume one Human-authorized fallback attempt."""
        directory = self.repo.root / self.root
        if not directory.is_dir():
            return None
        with (directory / ".next-failure.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            consumed = {path.stem.removesuffix(".used") for path in directory.glob("RF-*.used.json")}
            for record in reversed(self._records()):
                if (record["scope"] != "NEXT_FAILURE_ONLY"
                        or tuple(record["source"]) != source
                        or record["record_id"] in consumed):
                    continue
                relative = self.root / f"{record['record_id']}.used.json"
                self.repo.docs.write_once(relative, json.dumps({
                    "record_id": record["record_id"],
                    "meeting_id": self.repo.meeting_id,
                    "consumed_at": datetime.now(timezone.utc).isoformat(),
                    "policy": "ONE_FALLBACK_ATTEMPT_ON_NEXT_RESEARCH_DESK_FAILURE",
                }, indent=2, ensure_ascii=False))
                self.repo.events.append(
                    "RESEARCH_DESK_ONCE_FALLBACK_CONSUMED",
                    {"meeting_id": self.repo.meeting_id, "record_id": record["record_id"],
                     "source": list(source), "target": record["target"]},
                    actor="orchestrator",
                )
                return tuple(record["target"])
        return None

    def has_meeting_rule(self) -> bool:
        return any(record["scope"] == "ON_FUTURE_FAILURES" for record in self._records())

    def target_models(self) -> set[tuple[str, str]]:
        return {tuple(record["target"]) for record in self._records()}
