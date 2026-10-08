"""Audited, meeting-local technical recovery.

Technician can rank reconstructible evidence entries when a request exceeds
the provider context limit.  It cannot edit program files, governance files,
votes, or frozen source documents.  Unknown cases fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Callable

from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.runtime.prompt_contract import parse_json_prompt


EVIDENCE_LIST_KEYS = frozenset({
    "packets", "evidence_packets", "compact_evidence_index", "evidence_index",
    "research_results", "search_results", "source_documents",
})
ID_KEYS = ("packet_id", "source_id", "record_id", "id", "doi", "url")


def _item_id(item: object, index: int) -> str:
    if isinstance(item, dict):
        for key in ID_KEYS:
            if item.get(key):
                return str(item[key])
    return f"item-{index}"


def _at(value: object, path: tuple[str | int, ...]) -> object:
    for part in path:
        value = value[part]  # type: ignore[index]
    return value


def _inventory(payload: object) -> tuple[list[dict], list[tuple[tuple[str | int, ...], list]]]:
    candidates: list[dict] = []
    groups: list[tuple[tuple[str | int, ...], list]] = []

    def visit(value: object, path: tuple[str | int, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = (*path, key)
                if key in EVIDENCE_LIST_KEYS and isinstance(child, list) and len(child) >= 2:
                    group = len(groups)
                    groups.append((child_path, child))
                    for index, item in enumerate(child):
                        serialized = json.dumps(item, ensure_ascii=False)
                        candidates.append({
                            "group": group, "index": index,
                            "id": _item_id(item, index),
                            "characters": len(serialized), "preview": serialized[:160],
                        })
                else:
                    visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*path, index))

    visit(payload, ())
    return candidates, groups


def compact_evidence_request(
    *, user_text: str, fits: Callable[[str], bool],
    rank: Callable[[list[dict]], list[int]],
) -> tuple[str, dict] | None:
    """Keep every non-evidence field and every externally cited evidence item."""
    parsed = parse_json_prompt(user_text)
    if parsed is None:
        return None
    source, suffix = parsed
    candidates, groups = _inventory(source)
    if not candidates or len(candidates) > 600:
        return None
    compact = json.dumps(source, ensure_ascii=False, separators=(",", ":")) + suffix
    if fits(compact):
        return compact, {"method": "LOSSLESS_JSON_MINIFICATION", "omitted": []}

    outside = deepcopy(source)
    for path, _ in groups:
        _at(outside, path).clear()  # type: ignore[union-attr]
    outside_text = json.dumps(outside, ensure_ascii=False)
    protected = {
        number for number, candidate in enumerate(candidates)
        if not candidate["id"].startswith("item-")
        and re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(candidate['id'])}(?![A-Za-z0-9_-])",
            outside_text,
        )
    }

    priority = rank(candidates)
    if (not isinstance(priority, list) or len(priority) != len(candidates)
            or set(priority) != set(range(len(candidates)))):
        return None
    retained = set(range(len(candidates)))
    counts = {number: len(items) for number, (_, items) in enumerate(groups)}
    lookup = {(item["group"], item["index"]): number
              for number, item in enumerate(candidates)}

    def render() -> str:
        result = deepcopy(source)
        for group, (path, items) in enumerate(groups):
            selected = [item for index, item in enumerate(items)
                        if lookup[group, index] in retained]
            _at(result, path)[:] = selected  # type: ignore[index]
        omitted = [candidates[number]["id"] for number in range(len(candidates))
                   if number not in retained]
        result["_technician_context_compaction"] = {
            "partial_evidence_view": True,
            "omitted_item_count": len(omitted),
            "omitted_item_ids": omitted[:100],
            "original_request_sha256": hashlib.sha256(user_text.encode()).hexdigest(),
            "instruction": "This is a partial evidence view; do not claim exhaustive coverage.",
        }
        return json.dumps(result, ensure_ascii=False, separators=(",", ":")) + suffix

    for number in reversed(priority):
        if number in protected:
            continue
        group = candidates[number]["group"]
        if counts[group] <= 1:
            continue
        retained.remove(number)
        counts[group] -= 1
        revised = render()
        if fits(revised):
            return revised, {
                "method": "TECHNICIAN_RANKED_EVIDENCE_TRIM",
                "omitted": [candidates[i]["id"] for i in range(len(candidates)) if i not in retained],
                "protected": [candidates[i]["id"] for i in sorted(protected)],
            }
    return None


def record_context_repair(
    repo: MeetingRepository, *, participant_id: str, stage: str,
    original: str, revised: str, details: dict, technician_response: str | None,
) -> Path:
    repair_id = "TC-" + secrets.token_hex(8).upper()
    base = Path("audit_private/technician") / repair_id
    repo.docs.write_once(base / "original_request.txt", original)
    repo.docs.write_once(base / "revised_request.txt", revised)
    record = {
        "repair_id": repair_id, "participant_id": participant_id, "stage": stage,
        "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "revised_sha256": hashlib.sha256(revised.encode()).hexdigest(),
        "details": details, "technician_response": technician_response,
        "scope": "MEETING_REQUEST_ONLY; NO_PROGRAM_OR_FROZEN_DOCUMENT_EDIT",
    }
    repo.docs.write_once(base / "record.json", json.dumps(record, ensure_ascii=False, indent=2))
    repo.events.append("TECHNICIAN_CONTEXT_REPAIR_APPLIED", {
        "meeting_id": repo.meeting_id, "participant_id": participant_id,
        "stage": stage, "record_path": str(base / "record.json"),
        "omitted_item_count": len(details.get("omitted", [])),
    }, actor="orchestrator")
    return base / "record.json"
