"""Audited Human changes to future meeting-call controls.

The initialization manifest remains immutable. Each change is an append-only
Human decision and affects only calls not yet started; active calls keep their
invocation snapshot. Shrinking a limit waits for active slots to drain.
"""

from __future__ import annotations

import json
from pathlib import Path

from project_ensemble.domain import ReasoningEffort
from project_ensemble.research.search_policy import general_search_allowed, general_search_engine


_DIRECTORY = Path("human_private/runtime_controls")


def _records(repo) -> list[dict]:
    folder = repo.root / _DIRECTORY
    records = []
    for path in sorted(folder.glob("control-*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def record_run_control(repo, *, kind: str, target: str | None,
                       value: int | str | bool, reason: str | None = None) -> dict:
    """Append a validated, immutable control record under the meeting lock."""
    if kind == "model_concurrency":
        if not target or ":" not in target or not isinstance(value, int) or not 1 <= value <= 16:
            raise ValueError("model concurrency needs provider:model and a limit from 1 to 16")
    elif kind == "research_parallelism":
        if target is not None or not isinstance(value, int) or not 1 <= value <= 32:
            raise ValueError("research parallelism must be from 1 to 32")
    elif kind == "reasoning_effort":
        if not target or value not in {item.value for item in ReasoningEffort}:
            raise ValueError("reasoning effort needs a participant and a valid setting")
    elif kind in {"general_search_allowed", "institutional_access_allowed"}:
        if target is not None or not isinstance(value, bool):
            raise ValueError("search/source access needs a meeting-wide boolean permission")
    elif kind == "general_search_engine":
        if target is not None or not isinstance(value, str) or value not in {"tavily", "parallel", "disabled"}:
            raise ValueError("general search engine must be tavily, parallel or disabled")
    elif kind == "openalex_quota_policy":
        if target is not None or value not in {"wait", "tavily", "parallel"}:
            raise ValueError("OpenAlex quota policy must be wait, tavily or parallel")
        if value in {"tavily", "parallel"} and not general_search_allowed(repo):
            raise ValueError("本会议当前禁止通用搜索；请先在会议设置中允许通用搜索，再调整额度策略")
        if value in {"tavily", "parallel"} and value != general_search_engine(repo):
            raise ValueError("额度回退引擎必须与本会议选择的通用搜索引擎一致")
    else:
        raise ValueError(f"unsupported runtime control: {kind}")
    number = len(_records(repo)) + 1
    payload = {
        "sequence": number,
        "kind": kind,
        "target": target,
        "value": value,
        "reason": (reason.strip() or None) if reason is not None else None,
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
    if not general_search_allowed(repo):
        return "wait"
    result = frozen
    for record in _records(repo):
        if record["kind"] == "openalex_quota_policy":
            result = str(record["value"])
    if result in {"tavily", "parallel"} and result != general_search_engine(repo):
        return "wait"
    return result


def effective_reasoning_effort(repo, participant_id: str, frozen: ReasoningEffort) -> ReasoningEffort:
    result = frozen
    for record in _records(repo):
        if record["kind"] == "reasoning_effort" and record["target"] == participant_id:
            result = ReasoningEffort(record["value"])
    return result
