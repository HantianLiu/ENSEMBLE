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


def test_anonymous_openalex_does_not_inherit_existing_global_key(isolated_settings, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "should-not-be-used")
    path = save_search_backends(openalex_email=None, openalex_key_env="")
    assert load_config(path).research.openalex_api_key() is None


def test_wizard_can_reference_external_key_without_copying_it(isolated_settings, tmp_path):
    external = tmp_path / "external.sh"
    external.write_text("REMOTE_API_KEY='external-secret'\n", encoding="utf-8")
    options = iter(["provider", "openai_compatible", "file"])
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


def test_claude_code_requires_explicit_model_list():
    with pytest.raises(ValueError, match="selectable_models"):
        ProviderConfig(kind="claude_code")
    provider = ProviderConfig(kind="claude_code", selectable_models=["sonnet"])
    assert provider.api_key_env == ""


def test_claude_code_is_tool_free_and_does_not_put_prompt_or_key_in_argv(monkeypatch):
    seen = {}
    monkeypatch.setattr("project_ensemble.providers.claude_code.shutil.which", lambda command: "/bin/claude")

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        assert Path(kwargs["cwd"]).is_dir()
        assert Path(argv[argv.index("--system-prompt-file") + 1]).read_text() == "private rules"
        return SimpleNamespace(returncode=0, stdout='{"result":"answer","usage":{"input_tokens":9,"output_tokens":2}}', stderr="")

    monkeypatch.setattr("project_ensemble.providers.claude_code.subprocess.run", fake_run)
    adapter = ClaudeCodeAdapter("claude_lab", command="claude", models=["sonnet"], api_key="secret")
    response = adapter.generate(GenerationRequest(model_id="sonnet", system_text="private rules", user_text="question"))
    assert response.text == "answer"
    assert seen["kwargs"]["input"] == "question"
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
