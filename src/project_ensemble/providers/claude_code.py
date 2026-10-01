"""Tool-free, fresh Claude Code print-mode transport.

Claude Code is a CLI transport, not a delegated agent: every call uses an empty
temporary working directory and cannot read meeting files or invoke tools.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from project_ensemble.domain import GenerationRequest, GenerationResponse, ModelDescriptor
from project_ensemble.errors import PermanentProviderError, TransientProviderError
from project_ensemble.providers.base import ProviderAdapter, StreamProgressCallback


class ClaudeCodeAdapter(ProviderAdapter):
    def __init__(self, provider_id: str, *, command: str, models: list[str],
                 api_key: str | None = None, timeout_seconds: float = 1200):
        self.provider_id = provider_id
        self.command = command
        self.models = models
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _executable(self) -> str:
        executable = shutil.which(self.command)
        if executable is None:
            raise PermanentProviderError(
                f"Claude Code CLI not found: {self.command}; install it and run `claude auth login`"
            )
        return executable

    def list_models(self) -> list[ModelDescriptor]:
        self._executable()
        return [ModelDescriptor(
            provider_id=self.provider_id, model_id=model, owned_by="anthropic-claude-code",
            supported_methods=["claude/print"],
        ) for model in self.models]

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return self.generate_with_progress(request)

    def generate_with_progress(self, request: GenerationRequest,
                               on_progress: StreamProgressCallback | None = None) -> GenerationResponse:
        if request.model_id not in self.models:
            raise PermanentProviderError(f"Claude Code model not configured: {request.model_id}")
        if request.extra:
            raise PermanentProviderError("Claude Code cannot transport provider-specific extra fields")
        executable = self._executable()
        with tempfile.TemporaryDirectory(prefix="ensemble-claude-") as directory:
            system_path = Path(directory) / "system.txt"
            system_path.write_text(request.system_text, encoding="utf-8")
            os.chmod(system_path, 0o600)
            environment = os.environ.copy()
            # Subscription mode must not silently switch to separately billed API usage.
            if self.api_key is None:
                environment.pop("ANTHROPIC_API_KEY", None)
            else:
                environment["ANTHROPIC_API_KEY"] = self.api_key
            argv = [
                executable, "-p", "--bare", "--output-format", "json",
                "--no-session-persistence", "--model", request.model_id,
                "--tools", "", "--disallowedTools", "*",
                "--strict-mcp-config", "--mcp-config", "{}",
                "--disable-slash-commands", "--permission-mode", "dontAsk",
                "--system-prompt-file", str(system_path),
            ]
            if request.reasoning_effort.value != "default":
                argv.extend(("--effort", request.reasoning_effort.value))
            # The task itself travels over stdin so it is not exposed in the
            # operating system's process-argument listing.
            argv.append("Respond to the user request supplied on standard input.")
            if on_progress:
                on_progress("connected", 0, 0)
            try:
                process = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    cwd=directory, env=environment,
                )
            except OSError as exc:
                raise TransientProviderError(f"Claude Code process failed: {exc}") from exc
            deadline = time.monotonic() + self.timeout_seconds
            next_input: str | None = request.user_text
            try:
                while True:
                    if on_progress is not None:
                        on_progress("heartbeat", 0, 0)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TransientProviderError("Claude Code did not finish within the call timeout")
                    try:
                        stdout, stderr = process.communicate(
                            input=next_input, timeout=min(remaining, 0.25)
                        )
                        break
                    except subprocess.TimeoutExpired:
                        next_input = None
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            if process.returncode:
                detail = stderr.strip() or stdout.strip()
                raise PermanentProviderError(f"Claude Code exited {process.returncode}: {detail[:300]}")
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise TransientProviderError("Claude Code returned malformed JSON") from exc
            if not isinstance(payload, dict) or payload.get("is_error"):
                raise PermanentProviderError("Claude Code returned an error result")
            result = payload.get("result")
            if not isinstance(result, str) or not result.strip():
                raise TransientProviderError("Claude Code returned no final text")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            return GenerationResponse(
                text=result, provider_id=self.provider_id, model_id=request.model_id,
                request_id=str(payload.get("session_id")) if payload.get("session_id") else None,
                usage=usage,
                raw={"transport": "claude_code", "is_error": False},
            )
