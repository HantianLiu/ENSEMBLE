import httpx

from project_ensemble.providers.gemini import GeminiAdapter
from project_ensemble.providers.openai_compat import OpenAICompatibleAdapter


def test_openai_compatible_catalog_accepts_only_explicit_concurrency_field(monkeypatch):
    adapter = OpenAICompatibleAdapter("provider", "https://unused.invalid", "secret")
    response = httpx.Response(
        200,
        json={
            "data": [
                {"id": "explicit", "max_concurrent_requests": 3},
                {"id": "throughput-only", "requests_per_minute": 600},
            ]
        },
    )
    monkeypatch.setattr(adapter, "_request", lambda *_args, **_kwargs: response)

    models = {model.model_id: model for model in adapter.list_models()}

    assert models["explicit"].max_concurrent_requests == 3
    assert models["throughput-only"].max_concurrent_requests is None


def test_gemini_catalog_can_accept_future_explicit_concurrency_field(monkeypatch):
    adapter = GeminiAdapter("gemini", "https://unused.invalid", "secret")
    response = httpx.Response(
        200,
        json={
            "models": [
                {
                    "name": "models/future-model",
                    "supportedGenerationMethods": ["generateContent"],
                    "maxConcurrentRequests": 2,
                }
            ]
        },
    )
    monkeypatch.setattr(adapter, "_request", lambda *_args, **_kwargs: response)

    model = adapter.list_models()[0]

    assert model.max_concurrent_requests == 2
    assert adapter.reported_concurrency_limit("future-model") == 2
