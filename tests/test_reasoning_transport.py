import httpx

from project_ensemble.domain import GenerationRequest, ReasoningEffort
from project_ensemble.providers.gemini import GeminiAdapter
from project_ensemble.providers.openai_compat import OpenAICompatibleAdapter


def _response(payload):
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "https://provider.invalid/generate"),
    )


def test_openai_compatible_adapter_maps_reasoning_effort(monkeypatch):
    adapter = OpenAICompatibleAdapter(
        "deepseek",
        "https://provider.invalid",
        "key",
        reasoning_effort_map={"medium": "high"},
    )
    captured = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda method, suffix, **kwargs: captured.update(kwargs["json"])
        or _response({"choices": [{"message": {"content": "ok"}}]}),
    )

    adapter.generate(
        GenerationRequest(
            model_id="m",
            system_text="s",
            user_text="u",
            reasoning_effort=ReasoningEffort.MEDIUM,
        )
    )

    assert captured["reasoning_effort"] == "high"


def test_siliconflow_compatible_effort_mapping_sends_xhigh(monkeypatch):
    adapter = OpenAICompatibleAdapter(
        "siliconflow",
        "https://api.siliconflow.cn/v1",
        "key",
        reasoning_effort_map={"high": "xhigh"},
    )
    captured = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda method, suffix, **kwargs: captured.update(kwargs["json"])
        or _response({"choices": [{"message": {"content": "ok"}}]}),
    )

    adapter.generate(
        GenerationRequest(
            model_id="deepseek-ai/DeepSeek-V4-Flash",
            system_text="s",
            user_text="u",
            reasoning_effort=ReasoningEffort.HIGH,
        )
    )

    assert captured["reasoning_effort"] == "xhigh"


def test_gemini_adapter_maps_reasoning_effort(monkeypatch):
    adapter = GeminiAdapter(
        "gemini",
        "https://provider.invalid",
        "key",
        reasoning_effort_map={"high": "high"},
    )
    captured = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda method, suffix, **kwargs: captured.update(kwargs["json"])
        or _response({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
    )

    adapter.generate(
        GenerationRequest(
            model_id="m",
            system_text="s",
            user_text="u",
            reasoning_effort=ReasoningEffort.HIGH,
        )
    )

    assert captured["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
