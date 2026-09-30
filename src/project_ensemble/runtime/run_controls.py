"""Audited Human changes to future meeting-call controls.

The initialization manifest remains immutable. Each change is an append-only
Human decision and affects only calls not yet started; active calls keep their
invocation snapshot. Shrinking a limit waits for active slots to drain.
"""

from __future__ import annotations

import json
from pathlib import Path

from project_ensemble.domain import ReasoningEffort


_DIRECTORY = Path("human_private/runtime_controls")


def _records(repo) -> list[dict]:
    folder = repo.root / _DIRECTORY
    records = []
    for path in sorted(folder.glob("control-*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def record_run_control(repo, *, kind: str, target: str | None,
                       value: int | str, reason: str) -> dict:
    """Append a validated, immutable control record under the meeting lock."""
    if not reason.strip():
        raise ValueError("a runtime control change needs a Human reason")
    if kind == "model_concurrency":
        if not target or ":" not in target or not isinstance(value, int) or not 1 <= value <= 16:
            raise ValueError("model concurrency needs provider:model and a limit from 1 to 16")
    elif kind == "research_parallelism":
        if target is not None or not isinstance(value, int) or not 1 <= value <= 32:
            raise ValueError("research parallelism must be from 1 to 32")
    elif kind == "reasoning_effort":
        if not target or value not in {item.value for item in ReasoningEffort}:
            raise ValueError("reasoning effort needs a participant and a valid setting")
    elif kind == "openalex_quota_policy":
        if target is not None or value not in {"wait", "tavily"}:
            raise ValueError("OpenAlex quota policy must be wait or tavily")
    else:
        raise ValueError(f"unsupported runtime control: {kind}")
    number = len(_records(repo)) + 1
    payload = {
        "sequence": number,
        "kind": kind,
        "target": target,
        "value": value,
        "reason": reason.strip(),
        "authority": "HUMAN",
        "effect": "FUTURE_PROVIDER_CALLS_ONLY",
    }
    relative = _DIRECTORY / f"control-{number:06d}.json"
    repo.docs.write_once(relative, json.dumps(payload, ensure_ascii=False, indent=2))
    repo.events.append("MEETING_RUNTIME_CONTROL_CHANGED", {
        "meeting_id": repo.meeting_id,
        "record_path": str(relative),
        **payload,
    }, actor="HUMAN")
    return payload


def effective_model_concurrency(repo, frozen: dict[str, int]) -> dict[str, int]:
    result = {str(key): int(value) for key, value in frozen.items()}
    for record in _records(repo):
        if record["kind"] == "model_concurrency":
            result[record["target"]] = int(record["value"])
    return result


def effective_research_parallelism(repo, frozen: int | None) -> int | None:
    result = frozen
    for record in _records(repo):
        if record["kind"] == "research_parallelism":
            result = int(record["value"])
    return result


def effective_openalex_quota_policy(repo, frozen: str) -> str:
    """Apply Human's latest append-only quota decision without editing the manifest."""
    result = frozen
    for record in _records(repo):
        if record["kind"] == "openalex_quota_policy":
            result = str(record["value"])
    return result


def effective_reasoning_effort(repo, participant_id: str, frozen: ReasoningEffort) -> ReasoningEffort:
    result = frozen
    for record in _records(repo):
        if record["kind"] == "reasoning_effort" and record["target"] == participant_id:
            result = ReasoningEffort(record["value"])
    return result
