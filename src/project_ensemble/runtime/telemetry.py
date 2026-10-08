from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class TokenTelemetry(BaseModel):
    """Provider-neutral token accounting for one completed model response."""

    model_config = ConfigDict(extra="forbid")

    exchange_id: str
    participant_id: str
    provider_id: str
    model_id: str
    stage: str
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    cache_miss_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_rate: float | None = None
    # Estimates and fingerprints are separate from provider-reported usage.
    system_characters: int | None = None
    user_characters: int | None = None
    estimated_input_tokens: int | None = None
    system_sha256: str | None = None
    request_sha256: str | None = None


def _integer(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def normalize_token_usage(
    usage: dict[str, Any],
    *,
    exchange_id: str,
    participant_id: str,
    provider_id: str,
    model_id: str,
    stage: str,
) -> TokenTelemetry:
    """Normalize OpenAI-compatible, DeepSeek, Kimi, GLM, and Gemini usage fields."""

    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    input_details = usage.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}

    prompt = _integer(usage, "prompt_tokens", "input_tokens", "promptTokenCount")
    cached_source: str | None = None
    cached = _integer(
        usage,
        "prompt_cache_hit_tokens",
        "cachedContentTokenCount",
    )
    if cached is not None:
        cached_source = "explicit_hit_count"
    if cached is None:
        cached = _integer(prompt_details, "cached_tokens")
        if cached is not None:
            cached_source = "prompt_tokens_details.cached_tokens"
    if cached is None:
        cached = _integer(input_details, "cached_tokens")
        if cached is not None:
            cached_source = "input_tokens_details.cached_tokens"
    if cached is None:
        # Kimi and some OpenAI-compatible gateways report this at the top level.
        cached = _integer(usage, "cached_tokens")
        if cached is not None:
            cached_source = "cached_tokens"
    cache_miss = _integer(usage, "prompt_cache_miss_tokens", "cache_miss_tokens")

    # GLM's OpenAI-compatible endpoint currently emits
    # ``prompt_tokens_details.cached_tokens: 0`` even when cache telemetry is not
    # documented or otherwise reported.  Treating that compatibility placeholder
    # as an observed cache miss fabricates a 0.0% hit rate.  Preserve the raw
    # provider exchange for audit, but keep normalized cache telemetry unknown.
    # A positive nested count, or explicit hit/miss fields, remains reportable.
    if (
        provider_id.casefold() == "glm"
        and cached_source == "prompt_tokens_details.cached_tokens"
        and cached == 0
        and cache_miss is None
    ):
        cached = None
        cached_source = None

    if prompt is None and cached is not None and cache_miss is not None:
        prompt = cached + cache_miss
    if (
        cache_miss is None
        and prompt is not None
        and cached is not None
        and cached <= prompt
    ):
        cache_miss = prompt - cached

    completion = _integer(
        usage,
        "completion_tokens",
        "output_tokens",
        "candidatesTokenCount",
    )
    reasoning = _integer(completion_details, "reasoning_tokens")
    if reasoning is None:
        reasoning = _integer(output_details, "reasoning_tokens")
    if reasoning is None:
        reasoning = _integer(usage, "thoughtsTokenCount")
    total = _integer(usage, "total_tokens", "totalTokenCount")
    # Explicit cache hit/miss fields describe the cache-eligible input directly and
    # therefore form the most reliable denominator.  Otherwise OpenAI-compatible,
    # GLM, and Gemini all define their prompt/input total as including cached tokens.
    cache_denominator = None
    if cached is not None and cache_miss is not None:
        cache_denominator = cached + cache_miss
    elif cached is not None and prompt is not None and cached <= prompt:
        cache_denominator = prompt
    cache_hit_rate = (
        cached / cache_denominator
        if cached is not None and cache_denominator not in (None, 0)
        else None
    )

    return TokenTelemetry(
        exchange_id=exchange_id,
        participant_id=participant_id,
        provider_id=provider_id,
        model_id=model_id,
        stage=stage,
        prompt_tokens=prompt,
        cached_tokens=cached,
        cache_miss_tokens=cache_miss,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        total_tokens=total,
        cache_hit_rate=cache_hit_rate,
    )
