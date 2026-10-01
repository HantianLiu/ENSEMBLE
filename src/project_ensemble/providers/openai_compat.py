from __future__ import annotations

import json
from contextlib import contextmanager

import httpx
from project_ensemble.domain import GenerationRequest, GenerationResponse, ModelDescriptor
from project_ensemble.errors import PermanentProviderError, TransientProviderError
from project_ensemble.providers.base import ProviderAdapter, StreamProgressCallback
from project_ensemble.providers.http import (
    checked_json, checked_status, interruptible_stream_lines, interruptible_stream_open,
)
from project_ensemble.providers.sanitize import without_hidden_reasoning


class OpenAICompatibleAdapter(ProviderAdapter):
    def __init__(
        self,
        provider_id: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 1200.0,
        reasoning_effort_map: dict[str, object] | None = None,
    ):
        self.provider_id = provider_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort_map = reasoning_effort_map or {}
        self._reported_concurrency_limits: dict[str, int] = {}

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _request(self, method: str, suffix: str, **kwargs) -> httpx.Response:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                return client.request(method, f"{self.base_url}/{suffix.lstrip('/')}", headers=self.headers, **kwargs)
        # ``TransportError`` also covers protocol-level disconnects raised
        # while a streamed body is being consumed (for example an incomplete
        # chunked response).  Those failures are transient and must enter the
        # ordinary provider retry policy instead of terminating the meeting.
        except httpx.TransportError as exc:
            raise TransientProviderError(str(exc)) from exc

    @contextmanager
    def _stream_request(self, suffix: str, *, on_progress=None, **kwargs):
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                stream = client.stream(
                    "POST",
                    f"{self.base_url}/{suffix.lstrip('/')}",
                    headers=self.headers,
                    **kwargs,
                )
                with interruptible_stream_open(stream, on_progress) as response:
                    yield response
        except httpx.TransportError as exc:
            raise TransientProviderError(str(exc)) from exc

    def list_models(self) -> list[ModelDescriptor]:
        data = checked_json(self._request("GET", "models"))
        result = []
        for item in data.get("data", []):
            if not isinstance(item, dict) or "id" not in item:
                continue
            model_id = str(item["id"])
            self._record_model_concurrency(model_id, item)
            result.append(ModelDescriptor(
                provider_id=self.provider_id,
                model_id=model_id,
                owned_by=item.get("owned_by"),
                input_token_limit=item.get("context_length"),
                max_concurrent_requests=self.reported_concurrency_limit(model_id),
                supported_methods=["chat/completions"],
                raw=item,
            ))
        return result

    def reported_concurrency_limit(self, model_id: str) -> int | None:
        return self._reported_concurrency_limits.get(model_id)

    def _record_model_concurrency(self, model_id: str, metadata: dict) -> None:
        # These are explicit vendor extensions occasionally returned by
        # OpenAI-compatible model catalogs.  RPM/TPM fields are deliberately ignored.
        for key in (
            "max_concurrent_requests",
            "maxConcurrentRequests",
            "concurrency_limit",
        ):
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                self._reported_concurrency_limits[model_id] = value
                return

    def _payload(self, request: GenerationRequest) -> dict:
        payload: dict = {
            "model": request.model_id,
            "messages": [
                {"role": "system", "content": request.system_text},
                {"role": "user", "content": request.user_text},
            ],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        payload.update(request.extra)
        if request.reasoning_effort.value != "default":
            mapped = self.reasoning_effort_map.get(request.reasoning_effort.value)
            if mapped is None:
                raise PermanentProviderError(
                    f"{self.provider_id} has no mapping for reasoning effort "
                    f"{request.reasoning_effort.value!r}"
                )
            payload["reasoning_effort"] = mapped
        return payload

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = self._payload(request)
        data = checked_json(self._request("POST", "chat/completions", json=payload))
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TransientProviderError("provider response missing choices[0].message.content") from exc
        return GenerationResponse(
            text=text or "",
            provider_id=self.provider_id,
            model_id=request.model_id,
            request_id=data.get("id"),
            usage=data.get("usage") or {},
            raw=without_hidden_reasoning(data),
        )

    def generate_with_progress(
        self,
        request: GenerationRequest,
        on_progress: StreamProgressCallback | None = None,
    ) -> GenerationResponse:
        payload = self._payload(request)
        payload["stream"] = True
        reasoning_chars = 0
        content_parts: list[str] = []
        content_chars = 0
        usage: dict = {}
        request_id: str | None = None
        finish_reason: str | None = None
        saw_event = False
        with self._stream_request("chat/completions", json=payload, on_progress=on_progress) as response:
            checked_status(response)
            if on_progress is not None:
                on_progress("connected", 0, 0)
            for line in interruptible_stream_lines(response, on_progress):
                stripped = line.strip()
                if not stripped or stripped.startswith("event:"):
                    continue
                if stripped.startswith(":"):
                    if on_progress is not None:
                        on_progress("activity", reasoning_chars, content_chars)
                    continue
                if not stripped.startswith("data:"):
                    continue
                body = stripped[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise TransientProviderError("provider returned malformed SSE JSON") from exc
                if not isinstance(chunk, dict):
                    raise TransientProviderError("provider returned unexpected SSE event type")
                saw_event = True
                request_id = str(chunk.get("id")) if chunk.get("id") is not None else request_id
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    choice = choices[0]
                    if choice.get("finish_reason") is not None:
                        finish_reason = str(choice["finish_reason"])
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict):
                        reasoning = self._delta_text(
                            delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or delta.get("thinking")
                        )
                        content = self._delta_text(delta.get("content"))
                        if reasoning:
                            reasoning_chars += len(reasoning)
                        if content:
                            content_parts.append(content)
                            content_chars += len(content)
                if on_progress is not None:
                    state = "content" if content_chars else "reasoning" if reasoning_chars else "activity"
                    on_progress(state, reasoning_chars, content_chars)
        if not saw_event:
            raise TransientProviderError("provider returned an empty SSE stream")
        if on_progress is not None:
            on_progress("done", reasoning_chars, content_chars)
        text = "".join(content_parts)
        raw = {
            "id": request_id,
            "choices": [
                {
                    "message": {"content": text},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }
        return GenerationResponse(
            text=text,
            provider_id=self.provider_id,
            model_id=request.model_id,
            request_id=request_id,
            usage=usage,
            raw=raw,
        )

    @staticmethod
    def _delta_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                str(item.get("text", ""))
                for item in value
                if isinstance(item, dict)
            )
        return ""
