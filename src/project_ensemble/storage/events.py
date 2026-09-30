from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_ensemble.errors import EventChainError

GENESIS = "0" * 64


def _canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class HashChainEventLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._append_lock = threading.Lock()

    def _last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS
        last = None
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)
        return last["event_hash"] if last else GENESIS

    def append(self, event_type: str, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        with self._append_lock:
            body = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type,
                "actor": actor,
                "payload": payload,
                "prev_hash": self._last_hash(),
            }
            body["event_hash"] = hashlib.sha256(_canonical(body)).hexdigest()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n")
            return body

    def verify(self) -> bool:
        prev = GENESIS
        if not self.path.exists():
            return True
        with self.path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                event = json.loads(line)
                claimed = event.pop("event_hash", None)
                if event.get("prev_hash") != prev:
                    raise EventChainError(f"broken prev_hash at line {lineno}")
                actual = hashlib.sha256(_canonical(event)).hexdigest()
                if claimed != actual:
                    raise EventChainError(f"invalid event_hash at line {lineno}")
                prev = claimed
        return True
