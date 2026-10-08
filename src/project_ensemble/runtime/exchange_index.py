"""Rebuildable private replay index. Frozen exchanges remain authoritative."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable


def request_digest(participant: str, stage: str, system: object, user: str) -> str:
    return hashlib.sha256(json.dumps(
        [participant, stage, system, user], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class ExchangeReplayIndex:
    """Index request digests, not answers or institutional decisions.

    Synchronize file metadata on lookup, decoding only new/changed exchanges.
    No WAL (the workspace may be on NFS). SQL/cache failures fall back to the
    original scan, and every candidate is revalidated by MeetingEngine.
    """

    def __init__(self, root: Path, canonicalize: Callable):
        self.root = root
        self.canonicalize = canonicalize
        self.path = root / "governance_private/replay_cache/exchanges_v2.sqlite3"
        self.lock = threading.RLock()

    def _connect(self):
        for parent in (self.path.parent, self.path.parent.parent):
            if parent.is_symlink():
                raise OSError("replay index parent must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("replay index must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS exchanges ("
                "name TEXT PRIMARY KEY, mtime INTEGER NOT NULL, size INTEGER NOT NULL,"
                "exact_digest TEXT, canonical_digest TEXT)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS exact_lookup ON exchanges(exact_digest)")
            connection.execute("CREATE INDEX IF NOT EXISTS canonical_lookup ON exchanges(canonical_digest)")
        except BaseException:
            connection.close()
            raise
        return connection

    def _row(self, path, record, stat):
        request = record["request"]
        participant, stage = record["participant_id"], record["stage"]
        system, user = request["system_text"], request["user_text"]
        if not all(isinstance(value, str) for value in (participant, stage, system, user)):
            raise ValueError("invalid request")
        sections = self.canonicalize(system)
        return (
            path.name, stat.st_mtime_ns, stat.st_size,
            request_digest(participant, stage, system, user),
            request_digest(participant, stage, sections, user) if sections is not None else None,
        )

    def add(self, path: Path, record: dict) -> None:
        try:
            with self.lock:
                connection = self._connect()
                try:
                    with connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO exchanges VALUES (?, ?, ?, ?, ?)",
                            self._row(path, record, path.stat()),
                        )
                finally:
                    connection.close()
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            pass  # Optional acceleration must never pause an otherwise valid call.

    def candidates(self, participant: str, stage: str, system: str, user: str) -> list[Path] | None:
        try:
            with self.lock:
                connection = self._connect()
                try:
                    with connection:
                        known = {
                            name: (mtime, size)
                            for name, mtime, size in connection.execute(
                                "SELECT name, mtime, size FROM exchanges")
                        }
                        seen = set()
                        source = self.root / "governance_private/provider_exchanges"
                        for path in source.glob("X-*.json"):
                            if path.is_symlink():
                                continue
                            stat = path.stat()
                            seen.add(path.name)
                            if known.get(path.name) == (stat.st_mtime_ns, stat.st_size):
                                continue
                            try:
                                row = self._row(path, json.loads(path.read_text(encoding="utf-8")), stat)
                            except (ValueError, KeyError, TypeError):
                                row = (path.name, stat.st_mtime_ns, stat.st_size, None, None)
                            connection.execute(
                                "INSERT OR REPLACE INTO exchanges VALUES (?, ?, ?, ?, ?)", row)
                        connection.executemany(
                            "DELETE FROM exchanges WHERE name = ?", ((name,) for name in known.keys() - seen))
                        exact = request_digest(participant, stage, system, user)
                        sections = self.canonicalize(system)
                        canonical = (request_digest(participant, stage, sections, user)
                                     if sections is not None else None)
                        rows = connection.execute(
                            "SELECT name FROM exchanges WHERE exact_digest = ? OR canonical_digest = ? "
                            "ORDER BY mtime DESC", (exact, canonical),
                        ).fetchall()
                        return [source / name for (name,) in rows
                                if Path(name).name == name and name.startswith("X-") and name.endswith(".json")]
                finally:
                    connection.close()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return None
