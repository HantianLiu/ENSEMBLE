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


class GeminiAdapter(ProviderAdapter):
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
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _request(self, method: str, suffix: str, **kwargs) -> httpx.Response:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                return client.request(method, f"{self.base_url}/{suffix.lstrip('/')}", headers=self.headers, **kwargs)
        # Include protocol-level disconnects during streamed-body iteration;
        # providers occasionally close a chunked response before its terminal
        # event, which is safe to retry as a fresh generation request.
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
        models: list[ModelDescriptor] = []
        page_token: str | None = None
        while True:
            params = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            data = checked_json(self._request("GET", "models", params=params))
            for item in data.get("models", []):
                if not isinstance(item, dict) or "name" not in item:
                    continue
                name = str(item["name"])
                model_id = name.removeprefix("models/")
                self._record_model_concurrency(model_id, item)
                models.append(ModelDescriptor(
                    provider_id=self.provider_id,
                    model_id=model_id,
                    input_token_limit=item.get("inputTokenLimit"),
                    output_token_limit=item.get("outputTokenLimit"),
                    max_concurrent_requests=self.reported_concurrency_limit(model_id),
                    supported_methods=item.get("supportedGenerationMethods") or [],
                    raw=item,
                ))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return models

    def reported_concurrency_limit(self, model_id: str) -> int | None:
        return self._reported_concurrency_limits.get(model_id)

    def _record_model_concurrency(self, model_id: str, metadata: dict) -> None:
        # The documented Gemini schema currently has no such field, but retain
        # forward-compatible support if the service later adds an explicit one.
        for key in ("maxConcurrentRequests", "max_concurrent_requests"):
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                self._reported_concurrency_limits[model_id] = value
                return

    def _payload(self, request: GenerationRequest) -> dict:
        payload: dict = {
            "systemInstruction": {"parts": [{"text": request.system_text}]},
            "contents": [{"role": "user", "parts": [{"text": request.user_text}]}],
        }
        extra = dict(request.extra)
        configured_generation = extra.pop("generationConfig", {})
        if configured_generation is not None and not isinstance(configured_generation, dict):
            raise PermanentProviderError("Gemini generationConfig extra must be an object")
        payload.update(extra)
        gen_cfg: dict = dict(configured_generation or {})
        if request.temperature is not None:
            gen_cfg["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            gen_cfg["maxOutputTokens"] = request.max_output_tokens
        if request.reasoning_effort.value != "default":
            mapped = self.reasoning_effort_map.get(request.reasoning_effort.value)
            if mapped is None:
                raise PermanentProviderError(
                    f"{self.provider_id} has no mapping for reasoning effort "
                    f"{request.reasoning_effort.value!r}"
                )
            gen_cfg["thinkingConfig"] = {"thinkingLevel": mapped}
        if gen_cfg:
            payload["generationConfig"] = gen_cfg
        return payload

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = self._payload(request)
        data = checked_json(self._request("POST", f"models/{request.model_id}:generateContent", json=payload))
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(
                str(p.get("text", ""))
                for p in parts
                if isinstance(p, dict) and p.get("thought") is not True
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise TransientProviderError("Gemini response missing candidate text") from exc
        return GenerationResponse(
            text=text,
            provider_id=self.provider_id,
            model_id=request.model_id,
            request_id=None,
            usage=data.get("usageMetadata") or {},
            raw=without_hidden_reasoning(data),
        )

    def generate_with_progress(
        self,
        request: GenerationRequest,
        on_progress: StreamProgressCallback | None = None,
    ) -> GenerationResponse:
        payload = self._payload(request)
        reasoning_chars = 0
        content_parts: list[str] = []
        content_chars = 0
        usage: dict = {}
        request_id: str | None = None
        finish_reason: str | None = None
        saw_event = False
        suffix = f"models/{request.model_id}:streamGenerateContent"
        with self._stream_request(suffix, params={"alt": "sse"}, json=payload, on_progress=on_progress) as response:
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
                    raise TransientProviderError("Gemini returned malformed SSE JSON") from exc
                if not isinstance(chunk, dict):
                    raise TransientProviderError("Gemini returned unexpected SSE event type")
                saw_event = True
                if chunk.get("responseId") is not None:
                    request_id = str(chunk["responseId"])
                if isinstance(chunk.get("usageMetadata"), dict):
                    usage = chunk["usageMetadata"]
                candidates = chunk.get("candidates") or []
                if candidates and isinstance(candidates[0], dict):
                    candidate = candidates[0]
                    if candidate.get("finishReason") is not None:
                        finish_reason = str(candidate["finishReason"])
                    content = candidate.get("content") or {}
                    parts = content.get("parts") if isinstance(content, dict) else []
                    for part in parts or []:
                        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                            continue
                        if part.get("thought") is True:
                            reasoning_chars += len(part["text"])
                        else:
                            content_parts.append(part["text"])
                            content_chars += len(part["text"])
                if on_progress is not None:
                    state = "content" if content_chars else "reasoning" if reasoning_chars else "activity"
                    on_progress(state, reasoning_chars, content_chars)
        if not saw_event:
            raise TransientProviderError("Gemini returned an empty SSE stream")
        if on_progress is not None:
            on_progress("done", reasoning_chars, content_chars)
        text = "".join(content_parts)
        normalized_finish = "length" if finish_reason == "MAX_TOKENS" else finish_reason
        raw = {
            "responseId": request_id,
            "candidates": [
                {
                    "content": {"parts": [{"text": text}]},
                    "finish_reason": normalized_finish,
                }
            ],
            "usageMetadata": usage,
        }
        return GenerationResponse(
            text=text,
            provider_id=self.provider_id,
            model_id=request.model_id,
            request_id=request_id,
            usage=usage,
            raw=raw,
        )
