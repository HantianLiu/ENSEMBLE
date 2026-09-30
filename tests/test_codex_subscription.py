from __future__ import annotations

from collections import deque

import pytest

from project_ensemble.domain import GenerationRequest, ReasoningEffort
from project_ensemble.errors import PermanentProviderError
from project_ensemble.providers import codex_subscription


class FakeAppServer:
    account_type = "chatgpt"
    instruction_sources = []
    calls = []
    context_windows = []

    def __init__(
        self, command, timeout_seconds, cwd, codex_home=None,
        model_context_window=None,
    ):
        self.command = command
        self.codex_home = codex_home
        self.model_context_window = model_context_window
        type(self).context_windows.append(model_context_window)
        self.events = iter((
            {"method": "thread/tokenUsage/updated", "params": {
                "tokenUsage": {"last": {
                    "inputTokens": 100, "cachedInputTokens": 70,
                    "outputTokens": 20, "reasoningOutputTokens": 5, "totalTokens": 120,
                }}
            }},
            {"method": "item/completed", "params": {
                "item": {"type": "agentMessage", "phase": "final_answer", "text": "答案"}
            }},
            {"method": "turn/completed", "params": {
                "turn": {"id": "turn-1", "status": "completed"}
            }},
        ))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def request(self, method, params, *, timeout=None):
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": {"type": self.account_type}}
        if method == "mcpServerStatus/list":
            return {"data": []}
        if method == "model/list":
            return {"data": [{"model": "gpt-test", "displayName": "Test"}], "nextCursor": None}
        if method == "thread/start":
            return {"thread": {"id": "thread-1", "ephemeral": True},
                    "instructionSources": self.instruction_sources}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(method)

    def next_notification(self, _deadline):
        return next(self.events)


def test_codex_subscription_uses_chatgpt_ephemeral_isolated_thread(monkeypatch):
    FakeAppServer.account_type = "chatgpt"
    FakeAppServer.instruction_sources = []
    FakeAppServer.calls = []
    monkeypatch.setattr(codex_subscription, "_AppServer", FakeAppServer)
    adapter = codex_subscription.CodexSubscriptionAdapter(
        "codex", codex_home="/isolated/codex"
    )
    assert [model.model_id for model in adapter.list_models()] == ["gpt-test"]
    response = adapter.generate(GenerationRequest(
        model_id="gpt-test", system_text="制度", user_text="问题",
        reasoning_effort=ReasoningEffort.HIGH,
    ))
    assert response.text == "答案"
    assert response.usage["cached_tokens"] == 70
    thread = next(params for method, params in FakeAppServer.calls if method == "thread/start")
    turn = next(params for method, params in FakeAppServer.calls if method == "turn/start")
    assert thread["ephemeral"] is True
    assert thread["baseInstructions"] == "制度"
    assert turn["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn["effort"] == "high"


def test_codex_subscription_uses_configured_model_context_for_discovery_and_calls(monkeypatch):
    FakeAppServer.account_type = "chatgpt"
    FakeAppServer.instruction_sources = []
    FakeAppServer.context_windows = []
    monkeypatch.setattr(codex_subscription, "_AppServer", FakeAppServer)
    adapter = codex_subscription.CodexSubscriptionAdapter(
        "codex", model_input_token_limits={"gpt-test": 872000}
    )

    assert adapter.list_models()[0].input_token_limit == 872000
    adapter.generate(GenerationRequest(model_id="gpt-test", system_text="s", user_text="u"))

    assert FakeAppServer.context_windows == [None, 872000]


def test_codex_subscription_rejects_api_key_login_and_local_instructions(monkeypatch):
    monkeypatch.setattr(codex_subscription, "_AppServer", FakeAppServer)
    adapter = codex_subscription.CodexSubscriptionAdapter("codex")
    FakeAppServer.account_type = "apiKey"
    with pytest.raises(PermanentProviderError, match="ChatGPT login"):
        adapter.list_models()
    FakeAppServer.account_type = "chatgpt"
    FakeAppServer.instruction_sources = [{"path": "AGENTS.md"}]
    with pytest.raises(PermanentProviderError, match="codex_home"):
        adapter.generate(GenerationRequest(model_id="gpt-test", system_text="s", user_text="u"))
    FakeAppServer.instruction_sources = []


def test_app_server_keeps_early_turn_notifications_while_waiting_for_response():
    server = codex_subscription._AppServer.__new__(codex_subscription._AppServer)
    server._next_id = 1
    server.timeout_seconds = 1
    server._pending_events = deque()
    messages = iter((
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "早到的答案"}}},
        {"id": 1, "result": {"turn": {"id": "t"}}},
    ))
    server.send = lambda _message: None
    server.next_event = lambda _deadline: next(messages)
    assert server.request("turn/start", {}) == {"turn": {"id": "t"}}
    assert server.next_notification(999999999)["method"] == "item/completed"


def test_default_codex_home_links_only_existing_auth_not_personal_instructions(
    monkeypatch, tmp_path,
):
    account_home = tmp_path / "personal"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    (account_home / "AGENTS.md").write_text("personal instructions", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    call_directory = tmp_path / "call"
    call_directory.mkdir()
    isolated = codex_subscription._isolated_codex_home(str(call_directory), None)
    assert (isolated / "auth.json").is_symlink()
    assert (isolated / "auth.json").resolve() == account_home / "auth.json"
    assert not (isolated / "AGENTS.md").exists()
