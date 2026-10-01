"""Codex App Server transport using a user's ChatGPT-managed Codex entitlement.

This is deliberately not an OpenAI API adapter.  Each invocation uses a fresh,
ephemeral Codex thread in an empty temporary directory, with built-in tools
disabled.  The account mode is checked through the documented app-server API;
an API-key login is rejected rather than silently billing the Platform account.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from project_ensemble.domain import GenerationRequest, GenerationResponse, ModelDescriptor
from project_ensemble.errors import PermanentProviderError, TransientProviderError
from project_ensemble.providers.base import ProviderAdapter, StreamProgressCallback


_DISABLED_FEATURES = (
    "apps", "browser_use", "browser_use_external", "computer_use", "hooks",
    "plugins", "shell_tool", "skill_search", "unified_exec", "view_image",
)
_TOOL_ITEM_TYPES = {
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    "collabToolCall", "webSearch", "imageView", "browserUse", "computerUse",
}


def _isolated_codex_home(cwd: str, configured_home: str | None) -> Path:
    """Reuse local ChatGPT auth without importing personal Codex instructions.

    The per-call home is removed with the outer TemporaryDirectory. A symlink
    grants the child CLI access to its existing login but never copies tokens
    into a meeting artifact or another persistent configuration file.
    """

    if configured_home is not None:
        return Path(configured_home).expanduser().resolve()
    account_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    credential = account_home / "auth.json"
    if not credential.is_file():
        raise PermanentProviderError(
            "No local Codex ChatGPT login was found; run `codex login` or set "
            "codex_home to a separately signed-in clean Codex directory"
        )
    isolated = Path(cwd) / ".codex-home"
    isolated.mkdir(mode=0o700)
    (isolated / "auth.json").symlink_to(credential.resolve())
    return isolated


class _AppServer:
    def __init__(
        self,
        command: str,
        timeout_seconds: float,
        cwd: str,
        codex_home: str | None = None,
        model_context_window: int | None = None,
    ):
        executable = shutil.which(command)
        if executable is None:
            raise PermanentProviderError(f"Codex CLI not found: {command}; install it and run `codex login`")
        self.timeout_seconds = timeout_seconds
        self._next_id = 1
        self._events: queue.Queue[dict | None] = queue.Queue()
        self._pending_events: deque[dict] = deque()
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(_isolated_codex_home(cwd, codex_home))
        # A Platform key must never supersede a ChatGPT subscription login.
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("CODEX_API_KEY", None)
        argv = [
            executable, "app-server", "--stdio",
            "--config", 'model_provider="openai"',
            "--config", 'web_search="disabled"',
            "--config", "mcp_servers={}",
            "--config", "project_doc_max_bytes=0",
        ]
        if model_context_window is not None:
            argv.extend(("--config", f"model_context_window={model_context_window}"))
        for feature in _DISABLED_FEATURES:
            argv.extend(("--disable", feature))
        try:
            self.process = subprocess.Popen(
                argv, cwd=cwd, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
        except OSError as exc:
            raise PermanentProviderError(f"cannot launch Codex App Server: {exc}") from exc
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self.request("initialize", {
            "clientInfo": {
                "name": "project_ensemble", "title": "Project ENSEMBLE", "version": "0.7.3",
            }
        }, timeout=20)
        self.send({"method": "initialized", "params": {}})

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    self._events.put(event)
        finally:
            self._events.put(None)

    def send(self, message: dict) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TransientProviderError("Codex App Server connection closed") from exc

    def next_event(self, deadline: float) -> dict:
        while True:
            poll = getattr(self, "_poll_callback", None)
            if poll is not None:
                poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransientProviderError("Codex App Server did not respond before timeout")
            try:
                event = self._events.get(timeout=min(remaining, 0.25) if poll else remaining)
            except queue.Empty as exc:
                if poll is not None and time.monotonic() < deadline:
                    continue
                raise TransientProviderError("Codex App Server did not respond before timeout") from exc
            if event is None:
                raise TransientProviderError("Codex App Server closed before completing the request")
            return event

    def request(self, method: str, params: dict, *, timeout: float | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        self.send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        while True:
            event = self.next_event(deadline)
            if event.get("id") != request_id:
                self._pending_events.append(event)
                continue
            if "error" in event:
                error = event["error"]
                message = error.get("message", "unknown app-server error") if isinstance(error, dict) else str(error)
                raise PermanentProviderError(f"Codex {method} failed: {message}")
            result = event.get("result")
            if not isinstance(result, dict):
                raise TransientProviderError(f"Codex {method} returned no result")
            return result

    def next_notification(self, deadline: float) -> dict:
        if self._pending_events:
            return self._pending_events.popleft()
        return self.next_event(deadline)

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)

    def __enter__(self) -> "_AppServer":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class CodexSubscriptionAdapter(ProviderAdapter):
    # Observed turn/start request limit. Keep a safety margin for transport
    # framing; this is separate from the model's token context window.
    maximum_input_characters = 1_000_000
    def __init__(
        self, provider_id: str, *, command: str = "codex",
        codex_home: str | None = None, timeout_seconds: float = 1200,
        model_input_token_limits: dict[str, int] | None = None,
    ):
        self.provider_id = provider_id
        self.command = command
        self.codex_home = codex_home
        self.model_input_token_limits = model_input_token_limits or {}
        self.timeout_seconds = timeout_seconds

    def _check_isolation_and_auth(self, server: _AppServer) -> None:
        account = server.request("account/read", {"refreshToken": False}, timeout=20).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise PermanentProviderError(
                "Codex subscription provider requires ChatGPT login; run `codex login`. "
                "API-key authentication is deliberately rejected to avoid Platform billing."
            )
        mcp = server.request("mcpServerStatus/list", {"limit": 1}, timeout=20)
        if mcp.get("data"):
            raise PermanentProviderError(
                "Codex integration requires no active MCP servers; use an isolated Codex login/configuration"
            )

    def list_models(self) -> list[ModelDescriptor]:
        with tempfile.TemporaryDirectory(prefix="ensemble-codex-") as cwd:
            with _AppServer(self.command, self.timeout_seconds, cwd, self.codex_home) as server:
                self._check_isolation_and_auth(server)
                result: list[ModelDescriptor] = []
                cursor: str | None = None
                while True:
                    params: dict[str, Any] = {"limit": 100, "includeHidden": False}
                    if cursor is not None:
                        params["cursor"] = cursor
                    page = server.request("model/list", params, timeout=30)
                    for entry in page.get("data", []):
                        if not isinstance(entry, dict):
                            continue
                        model_id = entry.get("model") or entry.get("id")
                        if isinstance(model_id, str) and model_id:
                            result.append(ModelDescriptor(
                                provider_id=self.provider_id,
                                model_id=model_id,
                                input_token_limit=self.model_input_token_limits.get(model_id),
                                owned_by="openai-codex-subscription",
                                supported_methods=["codex/app-server"],
                                raw={
                                    "displayName": entry.get("displayName"),
                                    "supportedReasoningEfforts": entry.get("supportedReasoningEfforts", []),
                                },
                            ))
                    cursor = page.get("nextCursor")
                    if not cursor:
                        return result

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return self.generate_with_progress(request)

    def generate_with_progress(
        self, request: GenerationRequest, on_progress: StreamProgressCallback | None = None,
    ) -> GenerationResponse:
        if len(request.user_text) > self.maximum_input_characters:
            raise PermanentProviderError(
                f"Codex input contains {len(request.user_text)} characters, above the "
                f"{self.maximum_input_characters}-character safety budget"
            )
        if request.extra:
            raise PermanentProviderError("Codex subscription adapter cannot transport provider-specific extra fields")
        with tempfile.TemporaryDirectory(prefix="ensemble-codex-") as cwd:
            with _AppServer(
                self.command, self.timeout_seconds, cwd, self.codex_home,
                model_context_window=self.model_input_token_limits.get(request.model_id),
            ) as server:
                if on_progress is not None:
                    server._poll_callback = lambda: on_progress("heartbeat", 0, 0)
                self._check_isolation_and_auth(server)
                thread_result = server.request("thread/start", {
                    "model": request.model_id,
                    "modelProvider": "openai",
                    "cwd": str(Path(cwd).resolve()),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "baseInstructions": request.system_text,
                    "serviceName": "project_ensemble",
                }, timeout=30)
                thread = thread_result.get("thread") or {}
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread.get("ephemeral"):
                    raise PermanentProviderError("Codex did not create an ephemeral isolated thread")
                if thread_result.get("instructionSources"):
                    raise PermanentProviderError(
                        "Codex loaded unexpected local instruction files; configure codex_home "
                        "as a separate signed-in Codex directory without AGENTS.md"
                    )
                turn_params: dict[str, Any] = {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": request.user_text}],
                    "model": request.model_id,
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "approvalPolicy": "never",
                }
                if request.reasoning_effort.value != "default":
                    turn_params["effort"] = request.reasoning_effort.value
                turn_result = server.request("turn/start", turn_params, timeout=30)
                turn = turn_result.get("turn") or {}
                turn_id = turn.get("id")
                if not isinstance(turn_id, str):
                    raise TransientProviderError("Codex turn/start returned no turn ID")
                if on_progress is not None:
                    on_progress("connected", 0, 0)
                final_text = ""
                reasoning_chars = 0
                content_chars = 0
                usage: dict[str, Any] = {}
                deadline = time.monotonic() + self.timeout_seconds
                while True:
                    event = server.next_notification(deadline)
                    method = event.get("method")
                    params = event.get("params") or {}
                    if not isinstance(params, dict):
                        continue
                    if method in {"item/started", "item/completed"}:
                        item = params.get("item") or {}
                        if isinstance(item, dict):
                            if item.get("type") in _TOOL_ITEM_TYPES:
                                raise PermanentProviderError(
                                    "Codex attempted a tool call during a sealed ENSEMBLE model response"
                                )
                            if (method == "item/completed" and item.get("type") == "agentMessage"
                                    and item.get("phase") in {"final_answer", None}):
                                final_text = str(item.get("text") or "")
                    elif method == "item/agentMessage/delta":
                        content_chars += len(str(params.get("delta") or ""))
                        if on_progress is not None:
                            on_progress("content", reasoning_chars, content_chars)
                    elif method == "item/reasoning/summaryTextDelta":
                        reasoning_chars += len(str(params.get("delta") or ""))
                        if on_progress is not None:
                            on_progress("reasoning", reasoning_chars, content_chars)
                    elif method == "thread/tokenUsage/updated":
                        token_usage = params.get("tokenUsage") or {}
                        last = token_usage.get("last") or {}
                        if isinstance(last, dict):
                            usage = {
                                "prompt_tokens": last.get("inputTokens"),
                                "completion_tokens": last.get("outputTokens"),
                                "total_tokens": last.get("totalTokens"),
                                "cached_tokens": last.get("cachedInputTokens"),
                                "completion_tokens_details": {
                                    "reasoning_tokens": last.get("reasoningOutputTokens"),
                                },
                            }
                    elif method == "turn/completed":
                        completed = params.get("turn") or {}
                        if completed.get("id") != turn_id:
                            continue
                        if completed.get("status") != "completed":
                            error = completed.get("error") or {}
                            message = error.get("message", "turn did not complete") if isinstance(error, dict) else str(error)
                            raise TransientProviderError(f"Codex turn failed: {message}")
                        if not final_text:
                            raise TransientProviderError("Codex completed without a final response")
                        if on_progress is not None:
                            on_progress("done", reasoning_chars, len(final_text))
                        return GenerationResponse(
                            text=final_text,
                            provider_id=self.provider_id,
                            model_id=request.model_id,
                            request_id=turn_id,
                            usage={key: value for key, value in usage.items() if value is not None},
                            raw={
                                "transport": "codex_app_server",
                                "auth_mode": "chatgpt_subscription",
                                "temperature_control_supported": False,
                                "output_token_cap_supported": False,
                            },
                        )
