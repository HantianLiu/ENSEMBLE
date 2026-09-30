from __future__ import annotations

from project_ensemble.config import EnsembleConfig
from project_ensemble.errors import PermanentProviderError
from project_ensemble.providers.base import ProviderAdapter
from project_ensemble.providers.codex_subscription import CodexSubscriptionAdapter
from project_ensemble.providers.claude_code import ClaudeCodeAdapter
from project_ensemble.providers.gemini import GeminiAdapter
from project_ensemble.providers.openai_compat import OpenAICompatibleAdapter


def build_adapters(config: EnsembleConfig, require_keys: bool = True) -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}
    for provider_id, cfg in config.providers.items():
        if not cfg.enabled:
            continue
        if cfg.kind == "codex_subscription":
            adapters[provider_id] = CodexSubscriptionAdapter(
                provider_id,
                command=cfg.codex_command,
                codex_home=cfg.codex_home,
                timeout_seconds=cfg.timeout_seconds,
                model_input_token_limits=cfg.model_input_token_limits,
            )
            continue
        if cfg.kind == "claude_code":
            key = cfg.api_key() if cfg.api_key_env else None
            if require_keys and cfg.api_key_env and not key:
                raise PermanentProviderError(f"missing environment variable {cfg.api_key_env} for {provider_id}")
            adapters[provider_id] = ClaudeCodeAdapter(
                provider_id, command=cfg.claude_command,
                models=cfg.selectable_models or [], api_key=key,
                timeout_seconds=cfg.timeout_seconds,
            )
            continue
        key = cfg.api_key()
        if not key:
            if require_keys:
                raise PermanentProviderError(f"missing environment variable {cfg.api_key_env} for {provider_id}")
            continue
        if cfg.kind == "openai_compatible":
            effort_map = cfg.reasoning_effort_map if cfg.reasoning_effort_transport == "openai" else {}
            adapters[provider_id] = OpenAICompatibleAdapter(
                provider_id,
                cfg.base_url,
                key,
                cfg.timeout_seconds,
                effort_map,
            )
        elif cfg.kind == "gemini":
            effort_map = cfg.reasoning_effort_map if cfg.reasoning_effort_transport == "gemini" else {}
            adapters[provider_id] = GeminiAdapter(
                provider_id,
                cfg.base_url,
                key,
                cfg.timeout_seconds,
                effort_map,
            )
        else:
            raise PermanentProviderError(f"unsupported provider kind {cfg.kind!r} for {provider_id}")
    return adapters
