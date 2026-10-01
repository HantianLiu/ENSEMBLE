from pathlib import Path

from project_ensemble.config import (
    ProviderConfig,
    ResearchConfig,
    TavilyResearchConfig,
    load_config,
)
from pydantic import ValidationError
import pytest


def test_relative_project_paths_resolve_from_config_location(tmp_path):
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    path = config_dir / "ensemble.toml"
    path.write_text(
        """
[project]
workspace = "../meeting-data"
governance_docs = "../governance"
""".strip()
        + "\n"
    )
    config = load_config(path)
    assert config.source_path == path.resolve()
    assert config.project.workspace == str((tmp_path / "meeting-data").resolve())
    assert config.project.governance_docs == str((tmp_path / "governance").resolve())


def test_duplicate_selectable_models_are_rejected(tmp_path):
    path = tmp_path / "ensemble.toml"
    path.write_text(
        """
[providers.fake]
kind = "openai_compatible"
base_url = "https://unused.invalid"
api_key_env = "FAKE_API_KEY"
selectable_models = ["same", "same"]
""".strip()
        + "\n"
    )
    with pytest.raises(ValidationError, match="duplicate model IDs"):
        load_config(path)


def test_provider_output_token_limit_is_explicit_and_positive(tmp_path):
    path = tmp_path / "ensemble.toml"
    path.write_text("[governance]\n")
    assert load_config(path).governance.provider_output_token_limit is None

    path.write_text("[governance]\nprovider_output_token_limit = 8192\n")
    assert load_config(path).governance.provider_output_token_limit == 8192

    path.write_text("[governance]\nprovider_output_token_limit = 0\n")
    with pytest.raises(ValidationError):
        load_config(path)


def test_model_response_timeout_defaults_to_and_is_capped_at_twenty_minutes():
    base = {
        "kind": "openai_compatible",
        "base_url": "https://unused.invalid",
        "api_key_env": "KEY",
    }
    assert ProviderConfig(**base).timeout_seconds == 1200.0
    assert ProviderConfig(**base, timeout_seconds=300.0).timeout_seconds == 300.0
    with pytest.raises(ValidationError):
        ProviderConfig(**base, timeout_seconds=1200.1)


def test_reasoning_effort_support_is_model_scoped():
    provider = ProviderConfig(
        kind="gemini",
        base_url="https://unused.invalid",
        api_key_env="KEY",
        reasoning_effort_transport="gemini",
        reasoning_effort_map={"low": "low", "high": "high"},
        reasoning_effort_model_patterns=["gemini-3*"],
    )

    assert provider.supports_reasoning_effort("gemini-3-flash-preview", "high")
    assert not provider.supports_reasoning_effort("gemini-flash-latest", "high")
    assert not provider.supports_reasoning_effort("gemini-3-flash-preview", "medium")


def test_existing_lithos_profile_gets_kimi_k3_reasoning_effort_mapping(tmp_path):
    path = tmp_path / "ensemble.toml"
    path.write_text(
        """
[project]
workspace = "./meetings"

[providers.lithos]
kind = "openai_compatible"
display_name = "LithosAI"
base_url = "https://api.lithosai.cloud/v1"
api_key_env = "LITHOSAI_API_KEY"
""".strip() + "\n",
        encoding="utf-8",
    )

    provider = load_config(path).providers["lithos"]
    assert provider.supports_reasoning_effort("kimi-k3", "low")
    assert provider.supports_reasoning_effort("kimi-k3", "medium")
    assert provider.supports_reasoning_effort("kimi-k3", "high")
    assert not provider.supports_reasoning_effort("kimi-k2.6", "high")
    assert provider.reasoning_effort_map == {"low": "low", "medium": "high", "high": "max"}


def test_concurrency_configuration_is_model_scoped_and_defaults_to_auto():
    provider = ProviderConfig(
        kind="openai_compatible",
        base_url="https://unused.invalid",
        api_key_env="KEY",
        model_max_concurrent_requests={"fast": 4},
    )

    assert provider.configured_concurrency_limit("fast") == 4
    assert provider.configured_concurrency_limit("other") is None

    provider = ProviderConfig(
        kind="openai_compatible",
        base_url="https://unused.invalid",
        api_key_env="KEY",
        max_concurrent_requests=2,
        model_max_concurrent_requests={"fast": 4},
    )
    assert provider.configured_concurrency_limit("fast") == 4
    assert provider.configured_concurrency_limit("other") == 2


def test_concurrency_configuration_rejects_nonpositive_limits():
    base = {
        "kind": "openai_compatible",
        "base_url": "https://unused.invalid",
        "api_key_env": "KEY",
    }
    with pytest.raises(ValidationError):
        ProviderConfig(**base, max_concurrent_requests=0)
    with pytest.raises(ValidationError):
        ProviderConfig(**base, model_max_concurrent_requests={"model": 0})


def test_model_input_token_limits_are_positive_and_model_scoped():
    base = {
        "kind": "openai_compatible",
        "base_url": "https://unused.invalid",
        "api_key_env": "KEY",
    }
    provider = ProviderConfig(
        **base, model_input_token_limits={"deepseek-flash": 1_048_576}
    )
    assert provider.model_input_token_limits["deepseek-flash"] == 1_048_576
    with pytest.raises(ValidationError):
        ProviderConfig(**base, model_input_token_limits={"deepseek-flash": 0})


def test_tavily_key_uses_environment_then_explicit_assignment_file(tmp_path, monkeypatch):
    secret = tmp_path / "tavily.sh"
    secret.write_text("export TAVILY_API_KEY='file-secret'\n", encoding="utf-8")
    config = TavilyResearchConfig(enabled=True, api_key_file=str(secret))

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert config.api_key() == "file-secret"

    monkeypatch.setenv("TAVILY_API_KEY", "environment-secret")
    assert config.api_key() == "environment-secret"


def test_openalex_key_uses_environment_then_explicit_assignment_file(tmp_path, monkeypatch):
    secret = tmp_path / "openalex.sh"
    secret.write_text("export OPENALEX_API_KEY='file-secret'\n", encoding="utf-8")
    config = ResearchConfig(openalex_api_key_file=str(secret))

    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    assert config.openalex_api_key() == "file-secret"

    monkeypatch.setenv("OPENALEX_API_KEY", "environment-secret")
    assert config.openalex_api_key() == "environment-secret"


def test_research_concurrency_controls_are_separate_and_positive():
    config = ResearchConfig()
    assert config.max_concurrent_claim_groups == 4
    assert config.openalex_max_concurrent_requests == 1
    assert config.max_concurrent_document_downloads == 4
    assert config.tavily.max_concurrent_requests == 4

    for field in (
        "max_concurrent_claim_groups",
        "openalex_max_concurrent_requests",
        "max_concurrent_document_downloads",
    ):
        with pytest.raises(ValidationError):
            ResearchConfig(**{field: 0})
    with pytest.raises(ValidationError):
        TavilyResearchConfig(max_concurrent_requests=0)


def test_tavily_key_file_does_not_execute_shell(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-exist"
    secret = tmp_path / "tavily.sh"
    secret.write_text(
        f"export TAVILY_API_KEY=$(touch {marker})\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    assert TavilyResearchConfig(api_key_file=str(secret)).api_key() is None
    assert not marker.exists()


def test_relative_tavily_key_file_resolves_from_config_location(tmp_path):
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    path = config_dir / "ensemble.toml"
    path.write_text(
        "[research.tavily]\napi_key_file = '../secrets/tavily.sh'\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.research.tavily.api_key_file == str(
        (tmp_path / "secrets/tavily.sh").resolve()
    )


def test_relative_openalex_key_file_resolves_from_config_location(tmp_path):
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    path = config_dir / "ensemble.toml"
    path.write_text(
        "[research]\nopenalex_api_key_file = '../secrets/openalex.sh'\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.research.openalex_api_key_file == str(
        (tmp_path / "secrets/openalex.sh").resolve()
    )


def test_external_model_config_and_provider_key_file_are_portable(tmp_path, monkeypatch):
    configuration = tmp_path / "configuration"
    configuration.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "fake.sh").write_text("export FAKE_API_KEY='from-file'\n")
    (configuration / "model_config.toml").write_text(
        """
[providers.fake]
kind = "openai_compatible"
base_url = "https://unused.invalid"
api_key_env = "FAKE_API_KEY"
api_key_file = "../secrets/fake.sh"
""".strip()
        + "\n"
    )
    main = configuration / "ensemble.toml"
    main.write_text(
        "[project]\nmodel_config_file = './model_config.toml'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FAKE_API_KEY", raising=False)

    config = load_config(main)

    assert config.project.model_config_file == str(
        (configuration / "model_config.toml").resolve()
    )
    assert config.providers["fake"].api_key() == "from-file"


def test_external_model_config_rejects_inline_provider_ambiguity(tmp_path):
    (tmp_path / "models.toml").write_text(
        "[providers.external]\nkind='openai_compatible'\nbase_url='https://x.invalid'\napi_key_env='X'\n"
    )
    path = tmp_path / "ensemble.toml"
    path.write_text(
        """
[project]
model_config_file = "models.toml"
[providers.inline]
kind = "openai_compatible"
base_url = "https://y.invalid"
api_key_env = "Y"
""".strip()
        + "\n"
    )
    with pytest.raises(ValueError, match="either inline"):
        load_config(path)


def test_package_governance_sentinel_is_current_directory_independent(
    tmp_path, monkeypatch
):
    configuration = tmp_path / "configuration"
    configuration.mkdir()
    path = configuration / "ensemble.toml"
    path.write_text("[project]\ngovernance_docs='@package'\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_config(path)

    governance = Path(config.project.governance_docs)
    assert governance.is_absolute()
    assert (governance / "01_constitution").is_dir()
