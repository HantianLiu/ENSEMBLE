from __future__ import annotations

import os
import shlex
import tomllib
from fnmatch import fnmatchcase
from typing import Any, Literal
from pathlib import Path
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from project_ensemble.paths import bundled_governance_docs


def _read_secret_assignment(
    *, env_name: str, assignment_file: str | None
) -> str | None:
    """Read one literal shell assignment without sourcing or executing the file."""

    from_environment = os.environ.get(env_name)
    if from_environment:
        return from_environment
    if not assignment_file:
        return None
    path = Path(assignment_file).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped, comments=True)
        except ValueError:
            continue
        if tokens and tokens[0] == "export":
            tokens = tokens[1:]
        if len(tokens) != 1 or "=" not in tokens[0]:
            continue
        name, value = tokens[0].split("=", 1)
        if name == env_name and value:
            return value
    return None


class ProviderConfig(BaseModel):
    kind: str
    display_name: str | None = None
    enabled: bool = True
    base_url: str = ""
    api_key_env: str = ""
    api_key_file: str | None = None
    codex_command: str = "codex"
    codex_home: str | None = None
    claude_command: str = "claude"
    timeout_seconds: float = Field(default=1200.0, gt=0, le=1200.0)
    selectable_models: list[str] | None = None
    reasoning_effort_transport: Literal["none", "openai", "gemini"] = "none"
    reasoning_effort_map: dict[str, Any] = Field(default_factory=dict)
    reasoning_effort_model_patterns: list[str] | None = None
    # Most provider model-catalog APIs do not expose account concurrency.  None
    # means "use a provider-reported value when present, otherwise one".
    max_concurrent_requests: int | None = Field(default=None, ge=1)
    model_max_concurrent_requests: dict[str, int] = Field(default_factory=dict)
    # Some OpenAI-compatible /models endpoints expose only IDs and owners.  A
    # Human-maintained, vendor-verified context limit prevents those models
    # from falling back to one global conservative ceiling.
    model_input_token_limits: dict[str, int] = Field(default_factory=dict)

    @field_validator("selectable_models")
    @classmethod
    def selectable_models_are_unique(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [model.strip() for model in value]
        if any(not model for model in normalized):
            raise ValueError("selectable_models cannot contain an empty model ID")
        if len(normalized) != len(set(normalized)):
            raise ValueError("selectable_models cannot contain duplicate model IDs")
        return normalized

    @model_validator(mode="after")
    def reasoning_transport_matches_provider(self) -> "ProviderConfig":
        if self.kind not in {"openai_compatible", "gemini", "codex_subscription", "claude_code"}:
            raise ValueError(f"unsupported provider kind {self.kind!r}")
        if self.kind in {"openai_compatible", "gemini"} and (not self.base_url or not self.api_key_env):
            raise ValueError("API providers require base_url and api_key_env")
        if self.kind == "codex_subscription" and (
            self.api_key_env or self.api_key_file or self.base_url
        ):
            raise ValueError("Codex subscription uses Codex ChatGPT login, not an API key or base URL")
        if self.kind == "claude_code" and (self.base_url or not self.selectable_models):
            raise ValueError("Claude Code requires selectable_models and no base_url")
        valid_keys = {"low", "medium", "high"}
        unknown = set(self.reasoning_effort_map) - valid_keys
        if unknown:
            raise ValueError(
                "reasoning_effort_map has unsupported normalized levels: "
                + ", ".join(sorted(unknown))
            )
        if self.reasoning_effort_transport == "none" and self.reasoning_effort_map:
            raise ValueError("reasoning_effort_map requires a non-none transport")
        if self.kind == "gemini" and self.reasoning_effort_transport not in {"none", "gemini"}:
            raise ValueError("Gemini providers require gemini reasoning transport")
        if self.kind == "openai_compatible" and self.reasoning_effort_transport not in {"none", "openai"}:
            raise ValueError("OpenAI-compatible providers require openai reasoning transport")
        if self.kind == "codex_subscription" and self.reasoning_effort_transport not in {"none", "openai"}:
            raise ValueError("Codex subscription requires openai reasoning transport")
        if self.kind == "claude_code" and self.reasoning_effort_transport not in {"none", "openai"}:
            raise ValueError("Claude Code requires openai reasoning transport")
        if any(not model_id.strip() for model_id in self.model_max_concurrent_requests):
            raise ValueError("model_max_concurrent_requests cannot contain an empty model ID")
        if any(limit < 1 for limit in self.model_max_concurrent_requests.values()):
            raise ValueError("model concurrency limits must be positive")
        if any(not model_id.strip() for model_id in self.model_input_token_limits):
            raise ValueError("model_input_token_limits cannot contain an empty model ID")
        if any(limit < 1 for limit in self.model_input_token_limits.values()):
            raise ValueError("model input token limits must be positive")
        return self

    def api_key(self) -> str | None:
        return _read_secret_assignment(
            env_name=self.api_key_env,
            assignment_file=self.api_key_file,
        )

    def supports_reasoning_effort(self, model_id: str, effort: str) -> bool:
        if effort not in self.reasoning_effort_map:
            return False
        patterns = self.reasoning_effort_model_patterns
        return patterns is None or any(fnmatchcase(model_id, pattern) for pattern in patterns)

    def configured_concurrency_limit(self, model_id: str) -> int | None:
        """Return the Human-configured in-flight request cap, if one exists."""

        return self.model_max_concurrent_requests.get(
            model_id, self.max_concurrent_requests
        )


class GovernanceConfig(BaseModel):
    provider_retries: int = 3
    provider_retry_base_delay_seconds: float = Field(default=30.0, ge=0)
    provider_output_context_fraction: float = Field(default=0.50, gt=0, le=1)
    # By default the provider decides its own output ceiling.  A positive value
    # opts into ENSEMBLE's model-specific per-call budget calculation.
    provider_output_token_limit: int | None = Field(default=None, gt=0)
    # Input safety is independent of output generation.  Advertised provider
    # context windows use the fraction; providers without metadata use the
    # explicit fallback ceiling.
    provider_input_context_fraction: float = Field(default=0.80, gt=0, le=1)
    provider_input_token_limit_fallback: int = Field(default=262_144, gt=0)
    consultative_fraction: float = 0.30
    audit_max_turns_per_model: int = 5
    fail_closed: bool = True


class ProjectConfig(BaseModel):
    workspace: str = "./workspace"
    governance_docs: str = "./docs/governance"
    model_config_file: str | None = None


class TavilyResearchConfig(BaseModel):
    """Optional general-Web supplement for the OpenAlex-first Research Desk."""

    enabled: bool = False
    base_url: str = "https://api.tavily.com"
    api_key_env: str = "TAVILY_API_KEY"
    api_key_file: str | None = None
    search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "advanced"
    max_results_per_query: int = Field(default=8, ge=1, le=20)
    chunks_per_source: int = Field(default=3, ge=1, le=3)
    max_concurrent_requests: int = Field(default=4, ge=1)

    def api_key(self) -> str | None:
        return _read_secret_assignment(
            env_name=self.api_key_env,
            assignment_file=self.api_key_file,
        )


class ResearchConfig(BaseModel):
    """System-level defaults for the shared, non-voting Research Desk."""

    retriever: Literal["openalex"] = "openalex"
    openalex_base_url: str = "https://api.openalex.org"
    openalex_contact_email: str | None = None
    openalex_api_key_env: str = "OPENALEX_API_KEY"
    openalex_api_key_file: str | None = None
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_results_per_query: int = Field(default=12, ge=1, le=50)
    max_concurrent_claim_groups: int = Field(default=4, ge=1)
    openalex_max_concurrent_requests: int = Field(default=1, ge=1)
    openalex_min_request_interval_seconds: float = Field(default=1.0, ge=0)
    max_concurrent_document_downloads: int = Field(default=4, ge=1)
    volatile_freshness_days: int = Field(default=7, ge=0)
    versioned_freshness_days: int = Field(default=30, ge=0)
    stable_freshness_days: int = Field(default=180, ge=0)
    max_source_document_bytes: int = Field(default=50_000_000, ge=1)
    tavily: TavilyResearchConfig = Field(default_factory=TavilyResearchConfig)

    def openalex_api_key(self) -> str | None:
        return _read_secret_assignment(
            env_name=self.openalex_api_key_env,
            assignment_file=self.openalex_api_key_file,
        )


class EmailNotificationConfig(BaseModel):
    enabled: bool = False
    transport: Literal["slurm", "smtp"] = "slurm"
    slurm_sbatch_command: str = "sbatch"
    slurm_partition: str | None = None
    slurm_account: str | None = None
    slurm_qos: str | None = None
    slurm_time_limit: str = "00:01:00"
    host: str | None = None
    port: int = 587
    security: Literal["starttls", "ssl"] = "starttls"
    username_env: str | None = None
    password_env: str | None = None
    from_address: str | None = None
    timeout_seconds: float = 30.0
    subject_prefix: str = "[Project ENSEMBLE]"


class NotificationsConfig(BaseModel):
    email: EmailNotificationConfig = Field(default_factory=EmailNotificationConfig)


class EnsembleConfig(BaseModel):
    _source_path: Path | None = PrivateAttr(default=None)

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    @property
    def source_path(self) -> Path | None:
        return self._source_path


def load_config(path: str | Path) -> EnsembleConfig:
    source = Path(path).expanduser().resolve()
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    model_source = source
    project_data = data.get("project") or {}
    configured_model_file = project_data.get("model_config_file")
    if configured_model_file:
        model_source = Path(str(configured_model_file)).expanduser()
        if not model_source.is_absolute():
            model_source = source.parent / model_source
        model_source = model_source.resolve()
        model_data = tomllib.loads(model_source.read_text(encoding="utf-8"))
        unexpected = set(model_data) - {"providers"}
        if unexpected:
            raise ValueError(
                "model_config_file may contain only [providers.*] tables; unexpected: "
                + ", ".join(sorted(unexpected))
            )
        if data.get("providers"):
            raise ValueError(
                "configure providers either inline or through model_config_file, not both"
            )
        data["providers"] = model_data.get("providers", {})
    config = EnsembleConfig.model_validate(data)
    config._source_path = source
    if config.project.model_config_file:
        config.project.model_config_file = str(model_source)
    for field in ("workspace", "governance_docs"):
        raw_value = getattr(config.project, field)
        if field == "governance_docs" and raw_value == "@package":
            setattr(config.project, field, str(bundled_governance_docs()))
            continue
        configured = Path(raw_value).expanduser()
        if not configured.is_absolute():
            configured = source.parent / configured
        setattr(config.project, field, str(configured.resolve()))
    for owner, field in (
        (config.research, "openalex_api_key_file"),
        (config.research.tavily, "api_key_file"),
    ):
        configured_value = getattr(owner, field)
        if configured_value:
            secret_file = Path(configured_value).expanduser()
            if not secret_file.is_absolute():
                secret_file = source.parent / secret_file
            setattr(owner, field, str(secret_file.resolve()))
    for provider in config.providers.values():
        if provider.api_key_file:
            secret_file = Path(provider.api_key_file).expanduser()
            if not secret_file.is_absolute():
                secret_file = model_source.parent / secret_file
            provider.api_key_file = str(secret_file.resolve())
    return config
