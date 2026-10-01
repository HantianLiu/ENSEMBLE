import json
import threading
import time
from contextlib import nullcontext

import httpx
import pytest

from project_ensemble.domain import GenerationRequest
from project_ensemble.errors import ForcedModelReplacementRequested, TransientProviderError
from project_ensemble.providers.http import interruptible_stream_lines, interruptible_stream_open
from project_ensemble.providers.gemini import GeminiAdapter
from project_ensemble.providers.openai_compat import OpenAICompatibleAdapter


def _stream_response(events: list[dict | str]) -> httpx.Response:
    lines = []
    for event in events:
        body = event if isinstance(event, str) else json.dumps(event)
        lines.append(f"data: {body}\n\n")
    return httpx.Response(
        200,
        content="".join(lines).encode(),
        request=httpx.Request("POST", "https://provider.invalid/stream"),
        headers={"content-type": "text/event-stream"},
    )


class _IncompleteChunkedStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b'data: {"id":"partial","choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )


class _StreamingClient:
    def __init__(self, response: httpx.Response, *_args, **_kwargs):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, *_args, **_kwargs):
        return nullcontext(self.response)


def test_force_switch_during_silent_http_headers_does_not_wait_for_timeout():
    release = threading.Event()
    closed = threading.Event()

    class SlowStream:
        def __enter__(self):
            release.wait(timeout=2)
            return object()

        def __exit__(self, *_args):
            closed.set()

    calls = 0

    def heartbeat(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ForcedModelReplacementRequested("forced")

    started = time.monotonic()
    try:
        with pytest.raises(ForcedModelReplacementRequested):
            with interruptible_stream_open(SlowStream(), heartbeat):
                pytest.fail("silent stream should not open")
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
    assert closed.wait(timeout=1)


def test_force_switch_during_silent_http_body_does_not_wait_for_timeout():
    release = threading.Event()

    class SlowResponse:
        def iter_lines(self):
            release.wait(timeout=2)
            yield "data: [DONE]"

    calls = 0

    def heartbeat(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ForcedModelReplacementRequested("forced")

    started = time.monotonic()
    try:
        with pytest.raises(ForcedModelReplacementRequested):
            list(interruptible_stream_lines(SlowResponse(), heartbeat))
        assert time.monotonic() - started < 1.0
    finally:
        release.set()


def test_openai_compatible_stream_reports_liveness_without_retaining_reasoning(
    monkeypatch,
):
    adapter = OpenAICompatibleAdapter(
        "glm", "https://provider.invalid", "secret"
    )
    response = _stream_response(
        [
            {
                "id": "req-1",
                "choices": [
                    {"delta": {"reasoning_content": "private reasoning"}}
                ],
            },
            {"id": "req-1", "choices": [{"delta": {"content": "final "}}]},
            {"id": "req-1", "choices": [{"delta": {"content": "answer"}}]},
            {
                "id": "req-1",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 8,
                    "total_tokens": 18,
                },
            },
            "[DONE]",
        ]
    )
    monkeypatch.setattr(
        adapter,
        "_stream_request",
        lambda *_args, **_kwargs: nullcontext(response),
    )
    progress = []

    result = adapter.generate_with_progress(
        GenerationRequest(model_id="m", system_text="s", user_text="u"),
        lambda *item: progress.append(item),
    )

    assert result.text == "final answer"
    assert result.request_id == "req-1"
    assert result.usage["total_tokens"] == 18
    assert progress[0] == ("connected", 0, 0)
    assert ("reasoning", len("private reasoning"), 0) in progress
    assert progress[-1] == (
        "done",
        len("private reasoning"),
        len("final answer"),
    )
    serialized = json.dumps(result.raw)
    assert "private reasoning" not in serialized
    assert "reasoning_content" not in serialized


def test_gemini_stream_separates_thought_activity_from_final_text(monkeypatch):
    adapter = GeminiAdapter("gemini", "https://provider.invalid", "secret")
    response = _stream_response(
        [
            {
                "responseId": "gem-1",
                "candidates": [
                    {
                        "content": {
                            "parts": [{"thought": True, "text": "hidden thought"}]
                        }
                    }
                ],
            },
            {
                "responseId": "gem-1",
                "candidates": [
                    {"content": {"parts": [{"text": "final response"}]}}
                ],
            },
            {
                "responseId": "gem-1",
                "candidates": [{"finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 11,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 20,
                    "thoughtsTokenCount": 5,
                },
            },
            "[DONE]",
        ]
    )
    monkeypatch.setattr(
        adapter,
        "_stream_request",
        lambda *_args, **_kwargs: nullcontext(response),
    )
    progress = []

    result = adapter.generate_with_progress(
        GenerationRequest(model_id="m", system_text="s", user_text="u"),
        lambda *item: progress.append(item),
    )

    assert result.text == "final response"
    assert result.request_id == "gem-1"
    assert result.usage["thoughtsTokenCount"] == 5
    assert progress[0] == ("connected", 0, 0)
    assert ("reasoning", len("hidden thought"), 0) in progress
    assert progress[-1] == (
        "done",
        len("hidden thought"),
        len("final response"),
    )
    assert "hidden thought" not in json.dumps(result.raw)


@pytest.mark.parametrize(
    "adapter",
    [
        OpenAICompatibleAdapter("glm", "https://provider.invalid", "secret"),
        GeminiAdapter("gemini", "https://provider.invalid", "secret"),
    ],
)
def test_incomplete_stream_is_classified_as_transient(monkeypatch, adapter):
    response = httpx.Response(
        200,
        stream=_IncompleteChunkedStream(),
        request=httpx.Request("POST", "https://provider.invalid/stream"),
        headers={"content-type": "text/event-stream"},
    )
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *_args, **_kwargs: _StreamingClient(response),
    )

    with pytest.raises(TransientProviderError, match="peer closed connection"):
        adapter.generate_with_progress(
            GenerationRequest(model_id="m", system_text="s", user_text="u")
        )
