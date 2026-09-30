"""Human-defined ordered fallback models, scoped to one meeting."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path


class ModelFallbackOrderService:
    def __init__(self, repo):
        self.repo = repo
        self.root = Path("governance_private/model_fallback_orders")

    def set_order(self, source: tuple[str, str], targets: list[tuple[str, str]]) -> dict:
        if not 1 <= len(targets) <= 2 or len(set(targets)) != len(targets) or source in targets:
            raise ValueError("choose one or two distinct backup models, excluding the source")
        record_id = "FO-" + secrets.token_hex(6).upper()
        record = {
            "record_id": record_id, "meeting_id": self.repo.meeting_id,
            "source": list(source), "targets": [list(target) for target in targets],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        relative = self.root / f"{record_id}.json"
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "MODEL_FALLBACK_ORDER_SET",
            {"meeting_id": self.repo.meeting_id, "record_id": record_id,
             "source": record["source"], "targets": record["targets"]},
            actor="HUMAN",
        )
        return record

    def _records(self) -> list[dict]:
        directory = self.repo.root / self.root
        if not directory.is_dir():
            return []
        records = [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("FO-*.json")]
        return sorted(records, key=lambda record: (record["created_at"], record["record_id"]))

    def order_for(self, source: tuple[str, str]) -> list[tuple[str, str]]:
        for record in reversed(self._records()):
            if tuple(record["source"]) == source:
                return [tuple(target) for target in record["targets"]]
        return []

    def target_models(self) -> set[tuple[str, str]]:
        return {tuple(target) for record in self._records() for target in record["targets"]}
