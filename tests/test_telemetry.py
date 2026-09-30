from project_ensemble.runtime.telemetry import normalize_token_usage


def normalize(usage, *, provider_id="provider"):
    return normalize_token_usage(
        usage,
        exchange_id="X-TEST",
        participant_id="R-TEST",
        provider_id=provider_id,
        model_id="model",
        stage="stage",
    )


def test_normalizes_deepseek_cache_usage_and_derives_hit_rate():
    telemetry = normalize(
        {
            "prompt_tokens": 1000,
            "prompt_cache_hit_tokens": 400,
            "prompt_cache_miss_tokens": 600,
            "completion_tokens": 200,
            "completion_tokens_details": {"reasoning_tokens": 150},
            "total_tokens": 1200,
        }
    )

    assert telemetry.prompt_tokens == 1000
    assert telemetry.cached_tokens == 400
    assert telemetry.cache_miss_tokens == 600
    assert telemetry.reasoning_tokens == 150
    assert telemetry.cache_hit_rate == 0.4


def test_normalizes_openai_and_glm_nested_cached_tokens():
    telemetry = normalize(
        {
            "prompt_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 20},
            "completion_tokens": 20,
            "total_tokens": 100,
        }
    )

    assert telemetry.cached_tokens == 20
    assert telemetry.cache_miss_tokens == 60
    assert telemetry.cache_hit_rate == 0.25


def test_glm_compatible_zero_cache_placeholder_remains_unknown():
    telemetry = normalize(
        {
            "prompt_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens": 20,
            "total_tokens": 100,
        },
        provider_id="glm",
    )

    assert telemetry.prompt_tokens == 80
    assert telemetry.cached_tokens is None
    assert telemetry.cache_miss_tokens is None
    assert telemetry.cache_hit_rate is None


def test_glm_positive_nested_cache_count_is_still_reported():
    telemetry = normalize(
        {
            "prompt_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 20},
            "completion_tokens": 20,
            "total_tokens": 100,
        },
        provider_id="glm",
    )

    assert telemetry.cached_tokens == 20
    assert telemetry.cache_miss_tokens == 60
    assert telemetry.cache_hit_rate == 0.25


def test_normalizes_top_level_cached_tokens_from_compatible_gateways():
    telemetry = normalize(
        {
            "prompt_tokens": 80,
            "cached_tokens": 20,
            "completion_tokens": 20,
            "total_tokens": 100,
        }
    )

    assert telemetry.cached_tokens == 20
    assert telemetry.cache_miss_tokens == 60
    assert telemetry.cache_hit_rate == 0.25


def test_explicit_hit_and_miss_fields_define_cache_rate_denominator():
    telemetry = normalize(
        {
            # Some compatible gateways report a prompt total with slightly
            # different accounting.  Cache-specific fields remain authoritative.
            "prompt_tokens": 1100,
            "prompt_cache_hit_tokens": 400,
            "prompt_cache_miss_tokens": 600,
        }
    )

    assert telemetry.prompt_tokens == 1100
    assert telemetry.cached_tokens == 400
    assert telemetry.cache_miss_tokens == 600
    assert telemetry.cache_hit_rate == 0.4


def test_normalizes_openai_responses_token_fields():
    telemetry = normalize(
        {
            "input_tokens": 80,
            "input_tokens_details": {"cached_tokens": 20},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 110,
        }
    )

    assert telemetry.prompt_tokens == 80
    assert telemetry.cached_tokens == 20
    assert telemetry.cache_miss_tokens == 60
    assert telemetry.completion_tokens == 30
    assert telemetry.reasoning_tokens == 10
    assert telemetry.cache_hit_rate == 0.25


def test_normalizes_gemini_usage_metadata():
    telemetry = normalize(
        {
            "promptTokenCount": 500,
            "cachedContentTokenCount": 125,
            "candidatesTokenCount": 70,
            "thoughtsTokenCount": 30,
            "totalTokenCount": 600,
        }
    )

    assert telemetry.prompt_tokens == 500
    assert telemetry.cached_tokens == 125
    assert telemetry.cache_miss_tokens == 375
    assert telemetry.completion_tokens == 70
    assert telemetry.reasoning_tokens == 30
    assert telemetry.total_tokens == 600
    assert telemetry.cache_hit_rate == 0.25


def test_missing_cache_metrics_remain_unknown_not_zero():
    telemetry = normalize({"prompt_tokens": 500, "completion_tokens": 10})

    assert telemetry.cached_tokens is None
    assert telemetry.cache_miss_tokens is None
    assert telemetry.cache_hit_rate is None
