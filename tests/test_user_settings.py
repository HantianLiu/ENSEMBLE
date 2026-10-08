from __future__ import annotations

import io
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_ensemble.config import ProviderConfig, load_config
from project_ensemble.interface_language import ui_text
from project_ensemble.runtime.terminal_style import supports_color
from project_ensemble.storage.meeting_index import meeting_index_path
from project_ensemble.storage.meeting_index import register_meeting, resolve_indexed_meeting
from project_ensemble.providers.claude_code import ClaudeCodeAdapter
from project_ensemble.domain import GenerationRequest
from project_ensemble.user_settings import (
    add_provider, appearance, configure_interactively, ensure_user_config,
    save_appearance, save_freshness_preset, settings_dir, store_secret,
    save_search_backends, interface_language, save_interface_language,
    update_provider, remove_provider, replace_managed_secret, removed_provider_ids,
    hidden_model_ids, set_model_visibility,
)


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_first_run_config_is_portable_and_provider_catalog_is_user_scoped(isolated_settings):
    config_path = ensure_user_config()
    assert config_path == isolated_settings / "ensemble/ensemble.toml"
    assert load_config(config_path).providers == {}
    provider = ProviderConfig(
        kind="openai_compatible", display_name="我的模型", base_url="https://example.org/v1",
        api_key_env="CUSTOM_API_KEY",
    )
    add_provider("my_model", provider)
    loaded = load_config(config_path)
    assert loaded.providers["my_model"].display_name == "我的模型"
    assert loaded.providers["my_model"].base_url == "https://example.org/v1"
    with pytest.raises(ValueError, match="已存在"):
        add_provider("my_model", provider)


def test_secret_file_and_environment_override(isolated_settings, monkeypatch):
    path = store_secret("lab", "LAB_API_KEY", "abc 'quoted' value")
    assert path.read_text(encoding="utf-8").startswith("LAB_API_KEY=")
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    provider = ProviderConfig(
        kind="openai_compatible", base_url="https://example.org/v1",
        api_key_env="LAB_API_KEY", api_key_file=str(path),
    )
    assert provider.api_key() == "abc 'quoted' value"
    monkeypatch.setenv("LAB_API_KEY", "from-global-environment")
    assert provider.api_key() == "from-global-environment"


def test_provider_management_updates_and_removes_catalog_entries(isolated_settings):
    provider = ProviderConfig(
        kind="openai_compatible", display_name="LithosAI",
        base_url="https://api.lithosai.cloud/v1", api_key_env="LITHOSAI_API_KEY",
    )
    add_provider("lithos", provider)
    updated = provider.model_copy(update={
        "display_name": "Lithos",
        "base_url": "https://api.example.test/v1",
    })
    config_path = update_provider("lithos", updated)
    saved = load_config(config_path).providers["lithos"]
    assert saved.display_name == "Lithos"
    assert saved.base_url == "https://api.example.test/v1"

    remove_provider("lithos")
    assert "lithos" not in load_config(config_path).providers
    assert "lithos" in removed_provider_ids()


def test_hidden_models_are_user_scoped_and_can_be_updated_in_batches(isolated_settings):
    set_model_visibility("siliconflow", ["Qwen/Qwen3-8B", "deepseek-ai/DeepSeek-V4"], hidden=True)
    assert hidden_model_ids("siliconflow") == {"Qwen/Qwen3-8B", "deepseek-ai/DeepSeek-V4"}
    set_model_visibility("siliconflow", ["deepseek-ai/DeepSeek-V4"], hidden=False)
    assert hidden_model_ids("siliconflow") == {"Qwen/Qwen3-8B"}
    assert hidden_model_ids("other") == set()
    assert "Qwen/Qwen3-8B" in (settings_dir() / "hidden_models.toml").read_text(encoding="utf-8")


def test_model_visibility_batch_accepts_range_and_chinese_separators(isolated_settings):
    from project_ensemble.user_settings import _select_model_visibility_batch

    answers = iter(["", "1-2，4"])
    wizard = SimpleNamespace(input=lambda _prompt: next(answers))
    _select_model_visibility_batch(
        wizard, provider_id="lab", model_ids=["a", "b", "c", "d"],
        hide=True, t=lambda zh, en: zh, output=io.StringIO(),
    )
    assert hidden_model_ids("lab") == {"a", "b", "d"}


def test_model_visibility_manager_supports_multiple_hide_rounds_and_expanded_restore_panel(
    isolated_settings, monkeypatch,
):
    add_provider("lab", ProviderConfig(
        kind="openai_compatible", display_name="Lab",
        base_url="https://example.test/v1", api_key_env="LAB_API_KEY",
    ))
    catalog = [
        SimpleNamespace(model_id="deepseek-ai/DeepSeek-V4-Flash"),
        SimpleNamespace(model_id="Qwen/Qwen3-8B"),
    ]
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda config, providers, include_hidden=False: catalog,
    )
    choices = iter(["provider", "manage", "lab", "models", "hide", "hide", "expand", "back"])
    answers = iter(["DeepSeek", "1", "Qwen", "1", "DeepSeek", "1"])
    wizard = SimpleNamespace(
        output=io.StringIO(), language="en",
        _choose_one=lambda _title, _options: next(choices),
        input=lambda _prompt: next(answers),
    )

    configure_interactively(wizard)

    assert hidden_model_ids("lab") == {"Qwen/Qwen3-8B"}
    assert "live models" in wizard.output.getvalue()
    assert "Hidden models" in wizard.output.getvalue()


def test_saved_hidden_models_can_be_restored_after_configuration_when_provider_is_offline(
    isolated_settings, monkeypatch,
):
    add_provider("lab", ProviderConfig(
        kind="openai_compatible", display_name="Lab",
        base_url="https://example.test/v1", api_key_env="LAB_API_KEY",
    ))
    set_model_visibility("lab", ["retired-model"], hidden=True)
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline in test")),
    )
    choices = iter(["provider", "manage", "lab", "models", "expand", "back"])
    answers = iter(["retired", "1"])
    wizard = SimpleNamespace(
        output=io.StringIO(), language="en",
        _choose_one=lambda _title, _options: next(choices),
        input=lambda _prompt: next(answers),
    )

    configure_interactively(wizard)

    assert hidden_model_ids("lab") == set()
    assert "live catalog is unavailable" in wizard.output.getvalue()


def test_provider_settings_menu_can_rename_existing_provider(isolated_settings):
    add_provider("lithos", ProviderConfig(
        kind="openai_compatible", display_name="LithosAI",
        base_url="https://api.lithosai.cloud/v1", api_key_env="LITHOSAI_API_KEY",
    ))
    choices = iter(["provider", "manage", "lithos", "rename"])
    answers = iter(["Lithos Production"])
    wizard = SimpleNamespace(
        output=io.StringIO(), language="en",
        _choose_one=lambda _title, _options: next(choices),
        input=lambda _prompt: next(answers),
    )

    path = configure_interactively(wizard)

    assert load_config(path).providers["lithos"].display_name == "Lithos Production"


def test_provider_key_can_be_rotated_in_user_secret_store(isolated_settings):
    path = replace_managed_secret("lithos", "ENSEMBLE_LITHOS_MANAGED_KEY", "first-key")
    replace_managed_secret("lithos", "ENSEMBLE_LITHOS_MANAGED_KEY", "rotated-key")
    provider = ProviderConfig(
        kind="openai_compatible", base_url="https://example.test/v1",
        api_key_env="ENSEMBLE_LITHOS_MANAGED_KEY", api_key_file=str(path),
    )
    assert provider.api_key() == "rotated-key"


def test_appearance_and_freshness_are_user_scoped(isolated_settings, monkeypatch):
    assert appearance() == ("auto", 100)
    save_appearance("never", 80)
    assert appearance() == ("never", 80)
    assert not supports_color(io.StringIO())
    path = save_freshness_preset("short")
    research = load_config(path).research
    assert (research.volatile_freshness_days, research.versioned_freshness_days,
            research.stable_freshness_days) == (3, 14, 90)
    assert meeting_index_path(path) == settings_dir() / "meetings.json"


def test_interface_language_is_user_scoped_and_does_not_change_meeting_config(isolated_settings):
    assert interface_language() is None
    path = save_interface_language("en")
    assert path == settings_dir() / "interface.toml"
    assert interface_language() == "en"
    assert "language" not in ensure_user_config().read_text(encoding="utf-8")
    save_interface_language("zh")
    assert interface_language() == "zh"
    with pytest.raises(ValueError, match="zh or en"):
        save_interface_language("fr")


def test_settings_can_switch_interface_language(isolated_settings):
    choices = iter(["language", "interface", "en"])
    wizard = SimpleNamespace(
        output=io.StringIO(), language="zh",
        _choose_one=lambda _title, _options: next(choices),
    )
    configure_interactively(wizard)
    assert wizard.language == "en"
    assert interface_language() == "en"


def test_interface_copy_translates_numbered_setup_prompts_but_not_content():
    assert ui_text("\n9/10 输入任务描述: ", "en") == "\n9/10 Describe the task: "
    assert ui_text("1/10 选择会议类型", "en") == "1/10 Choose a meeting type"
    assert ui_text("原始研究命题 α", "en") == "原始研究命题 α"
    assert ui_text("GLM · 凭据已检测", "en") == "GLM · credentials detected"
    assert ui_text("输入上限 872000 tokens; 接口 codex/app-server", "en") == "Input limit 872000 tokens; Interface codex/app-server"


def test_search_backend_settings_preserve_external_key_references(isolated_settings, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    external = tmp_path / "openalex.sh"
    external.write_text("OPENALEX_API_KEY='academic-key'\n", encoding="utf-8")
    path = save_search_backends(
        openalex_email="reader@example.org", openalex_key_file=str(external),
        tavily_enabled=True, tavily_key_env="MY_TAVILY_KEY",
    )
    loaded = load_config(path)
    assert loaded.research.openalex_api_key() == "academic-key"
    assert loaded.research.openalex_contact_email == "reader@example.org"
    assert loaded.research.tavily.enabled
    assert loaded.research.tavily.api_key_env == "MY_TAVILY_KEY"
    assert "academic-key" not in path.read_text(encoding="utf-8")


def test_parallel_settings_round_trip_and_existing_configuration_is_preserved(isolated_settings, tmp_path, monkeypatch):
    monkeypatch.delenv("MY_PARALLEL_KEY", raising=False)
    key = tmp_path / "parallel-key.sh"
    key.write_text("MY_PARALLEL_KEY='fixture-secret'\n", encoding="utf-8")
    path = save_search_backends(
        openalex_email=None, parallel_enabled=True,
        parallel_key_env="MY_PARALLEL_KEY", parallel_key_file=str(key), parallel_mode="turbo",
    )
    cfg = load_config(path)
    assert cfg.research.parallel.enabled
    assert cfg.research.parallel.mode == "turbo"
    assert cfg.research.parallel.max_results_per_query == 10
    assert cfg.research.parallel.api_key() == "fixture-secret"
    assert "fixture-secret" not in path.read_text()
    save_search_backends(openalex_email=None, tavily_enabled=True)
    assert load_config(path).research.parallel.mode == "turbo"


def test_anonymous_openalex_does_not_inherit_existing_global_key(isolated_settings, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "should-not-be-used")
    path = save_search_backends(openalex_email=None, openalex_key_env="")
    assert load_config(path).research.openalex_api_key() is None


def test_wizard_can_reference_external_key_without_copying_it(isolated_settings, tmp_path, monkeypatch):
    external = tmp_path / "external.sh"
    external.write_text("REMOTE_API_KEY='external-secret'\n", encoding="utf-8")
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline in test")),
    )
    options = iter(["provider", "add", "openai_compatible", "file", "back"])
    answers = iter(["实验网关", "lab", "https://gateway.example/v1", "REMOTE_API_KEY", str(external)])
    wizard = SimpleNamespace(
        output=io.StringIO(), _choose_one=lambda _title, _options: next(options),
        input=lambda _prompt: next(answers),
    )
    path = configure_interactively(wizard)
    provider = load_config(path).providers["lab"]
    assert provider.api_key() == "external-secret"
    assert provider.api_key_file == str(external)
    assert "external-secret" not in path.read_text(encoding="utf-8")
    assert not (settings_dir() / "secrets/lab.sh").exists()


def test_wizard_adds_lithosai_preset_without_manual_toml(isolated_settings, monkeypatch):
    monkeypatch.setenv("LITHOSAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda *args, **kwargs: [SimpleNamespace(model_id="mock-model")],
    )
    choices = iter(["provider", "add", "lithosai", "env", "back"])
    wizard = SimpleNamespace(
        output=io.StringIO(),
        _choose_one=lambda _title, _options: next(choices),
    )

    path = configure_interactively(wizard)
    provider = load_config(path).providers["lithos"]

    assert provider.kind == "openai_compatible"
    assert provider.display_name == "LithosAI"
    assert provider.base_url == "https://api.lithosai.cloud/v1"
    assert provider.api_key_env == "LITHOSAI_API_KEY"
    assert provider.api_key() == "test-key"


def test_wizard_adds_siliconflow_with_model_scoped_reasoning_effort(isolated_settings, monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda *args, **kwargs: [SimpleNamespace(model_id="deepseek-ai/DeepSeek-V4-Flash")],
    )
    choices = iter(["provider", "add", "siliconflow", "env", "back"])
    wizard = SimpleNamespace(
        output=io.StringIO(), language="en",
        _choose_one=lambda _title, _options: next(choices),
    )

    path = configure_interactively(wizard)
    provider = load_config(path).providers["siliconflow"]

    assert provider.base_url == "https://api.siliconflow.cn/v1"
    assert provider.api_key_env == "SILICONFLOW_API_KEY"
    assert provider.reasoning_effort_map == {"high": "xhigh"}
    assert provider.supports_reasoning_effort("deepseek-ai/DeepSeek-V4-Flash", "high")
    assert not provider.supports_reasoning_effort("Qwen/Qwen3-8B", "high")
    assert "Fetching this provider's current model catalog" in wizard.output.getvalue()


def test_claude_code_requires_explicit_model_list():
    with pytest.raises(ValueError, match="selectable_models"):
        ProviderConfig(kind="claude_code")
    provider = ProviderConfig(kind="claude_code", selectable_models=["sonnet"])
    assert provider.api_key_env == ""


def test_claude_code_is_tool_free_and_does_not_put_prompt_or_key_in_argv(monkeypatch):
    seen = {}
    monkeypatch.setattr("project_ensemble.providers.claude_code.shutil.which", lambda command: "/bin/claude")

    class FakeProcess:
        returncode = None

        def __init__(self, argv, **kwargs):
            seen.update(argv=argv, kwargs=kwargs)
            assert Path(kwargs["cwd"]).is_dir()
            assert Path(argv[argv.index("--system-prompt-file") + 1]).read_text() == "private rules"

        def communicate(self, input=None, timeout=None):
            seen["input"] = input
            self.returncode = 0
            return '{"result":"answer","usage":{"input_tokens":9,"output_tokens":2}}', ""

        def poll(self):
            return self.returncode

    monkeypatch.setattr("project_ensemble.providers.claude_code.subprocess.Popen", FakeProcess)
    adapter = ClaudeCodeAdapter("claude_lab", command="claude", models=["sonnet"], api_key="secret")
    response = adapter.generate(GenerationRequest(model_id="sonnet", system_text="private rules", user_text="question"))
    assert response.text == "answer"
    assert seen["input"] == "question"
    assert seen["kwargs"]["env"]["ANTHROPIC_API_KEY"] == "secret"
    assert "secret" not in " ".join(seen["argv"])
    assert "--bare" in seen["argv"] and "--disallowedTools" in seen["argv"]


def test_meeting_index_resolves_unique_title_across_config_paths(isolated_settings, tmp_path):
    root = tmp_path / "LR-ABCDEF12"
    (root / "public").mkdir(parents=True)
    (root / "public/meeting_manifest.json").write_text(
        '{"meeting_id":"LR-ABCDEF12","title":"我的文献会议"}', encoding="utf-8"
    )
    first_config = tmp_path / "first.toml"
    second_config = tmp_path / "second.toml"
    register_meeting(root, first_config)
    assert resolve_indexed_meeting("我的文献会议", second_config) == root
    assert meeting_index_path(first_config) == meeting_index_path(second_config)
