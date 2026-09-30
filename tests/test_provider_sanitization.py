import json

import httpx

from project_ensemble.domain import GenerationRequest
from project_ensemble.providers.gemini import GeminiAdapter
from project_ensemble.providers.openai_compat import OpenAICompatibleAdapter


def response(data):
    request = httpx.Request("POST", "https://provider.invalid")
    return httpx.Response(200, json=data, request=request)


def test_openai_compatible_does_not_return_hidden_reasoning(monkeypatch):
    adapter = OpenAICompatibleAdapter("fake", "https://provider.invalid", "secret")
    data = {
        "id": "req",
        "choices": [
            {
                "message": {
                    "content": "final answer",
                    "reasoning_content": "private chain of thought",
                    "reasoning_details": {"secret": True},
                }
            }
        ],
    }
    monkeypatch.setattr(adapter, "_request", lambda *args, **kwargs: response(data))
    result = adapter.generate(GenerationRequest(model_id="m", system_text="s", user_text="u"))
    assert result.text == "final answer"
    assert "private chain" not in json.dumps(result.raw)
    assert "reasoning_content" not in json.dumps(result.raw)


def test_gemini_excludes_thought_parts_from_text_and_raw(monkeypatch):
    adapter = GeminiAdapter("gemini", "https://provider.invalid", "secret")
    data = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "text": "private thought"},
                        {"text": "final answer"},
                    ]
                }
            }
        ]
    }
    monkeypatch.setattr(adapter, "_request", lambda *args, **kwargs: response(data))
    result = adapter.generate(GenerationRequest(model_id="m", system_text="s", user_text="u"))
    assert result.text == "final answer"
    assert "private thought" not in json.dumps(result.raw)
    assert result.raw["candidates"][0]["content"]["parts"][0] == {"thought": True, "redacted": True}
