"""Private stage/model usage summaries; missing usage is never reported as zero."""
from __future__ import annotations

import json
from pathlib import Path

from project_ensemble.runtime.telemetry import TokenTelemetry, normalize_token_usage

METRICS = (
    "prompt_tokens", "cached_tokens", "cache_miss_tokens", "completion_tokens",
    "reasoning_tokens", "total_tokens", "estimated_input_tokens",
    "system_characters", "user_characters",
)
SUMMARY_PATH = Path("human_private/usage_summary.json")


def build_usage_summary(root: Path) -> dict:
    records = {}
    unreadable = []
    for path in sorted((root / "governance_private/telemetry").glob("X-*.json")):
        if path.is_symlink():
            unreadable.append(str(path.relative_to(root)))
            continue
        try:
            value = TokenTelemetry.model_validate_json(path.read_text(encoding="utf-8"))
            if value.exchange_id != path.stem:
                raise ValueError("telemetry identity mismatch")
            records[value.exchange_id] = value
        except (OSError, ValueError):
            unreadable.append(str(path.relative_to(root)))
    # Also supports meetings recorded before telemetry was introduced, without
    # constructing an engine or rewriting any original exchange.
    for path in sorted((root / "governance_private/provider_exchanges").glob("X-*.json")):
        if path.stem in records:
            continue
        try:
            if path.is_symlink():
                raise ValueError("exchange is not an ordinary file")
            exchange = json.loads(path.read_text(encoding="utf-8"))
            if exchange["exchange_id"] != path.stem:
                raise ValueError("exchange identity mismatch")
            usage = exchange["response"].get("usage", {})
            records[path.stem] = normalize_token_usage(
                usage if isinstance(usage, dict) else {}, exchange_id=path.stem,
                participant_id=exchange["participant_id"], provider_id=exchange["provider_id"],
                model_id=exchange["model_id"], stage=exchange["stage"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            unreadable.append(str(path.relative_to(root)))
    grouped = {}
    for value in records.values():
        key = (value.stage, value.provider_id, value.model_id)
        grouped.setdefault(key, []).append(value)
    rows = []
    for (stage, provider, model), calls in sorted(grouped.items()):
        metrics = {}
        for name in METRICS:
            measured = [getattr(call, name) for call in calls if getattr(call, name) is not None]
            metrics[name] = {
                "known_total": sum(measured) if measured else None,
                "reported_call_count": len(measured),
                "unknown_call_count": len(calls) - len(measured),
            }
        paired = [(call.cached_tokens, call.prompt_tokens) for call in calls
                  if call.cached_tokens is not None and call.prompt_tokens is not None
                  and 0 <= call.cached_tokens <= call.prompt_tokens]
        denominator = sum(prompt for cached, prompt in paired)
        rows.append({
            "stage": stage, "provider_id": provider, "model_id": model,
            "recorded_call_count": len(calls),
            "participant_ids": sorted({call.participant_id for call in calls}),
            "metrics": metrics,
            "cache_hit_rate_on_reported_calls": (
                sum(cached for cached, prompt in paired) / denominator if denominator else None),
            "cache_usage_reported_call_count": len(paired),
            "system_sha256_values": sorted({call.system_sha256 for call in calls if call.system_sha256}),
        })
    return {
        "schema_version": 1, "recorded_call_count": len(records), "stages": rows,
        "unreadable_source_paths": sorted(set(unreadable)),
        "coverage": "RECORDED_MODEL_RESPONSES_ONLY; TRANSPORT_FAILURES_WITHOUT_USAGE_NOT_COUNTED",
        "authority": "PRIVATE_OBSERVATIONAL_TELEMETRY; NOT_REPRESENTATIVE_CONTEXT_OR_VOTE_EVIDENCE",
        "notes": [
            "Missing usage remains unknown, not zero; known_total may cover only part of the calls.",
            "Estimated input tokens are not provider measurements or billing amounts.",
            "Reasoning/cache tokens may be subsets of other totals; do not sum overlapping metrics.",
            "System digests identify full system messages, not provider cache prefix boundaries.",
        ],
    }
