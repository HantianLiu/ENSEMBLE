import io
import tomllib
from types import SimpleNamespace

import httpx
import pytest

from project_ensemble import cli
from project_ensemble.config import load_config
from project_ensemble.providers.search_health import probe_search_backend
from project_ensemble.search_backend_settings import (
    configure_search_interactively, effective_search_config, save_search_backend,
    search_backend_state,
)
from project_ensemble.user_settings import ensure_user_config, save_search_backends, user_config_path


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("LAB_OA", "private-existing-oa")
    monkeypatch.setenv("LAB_TV", "private-existing-tv")
    monkeypatch.setenv("LAB_PA", "private-existing-pa")
    return save_search_backends(openalex_email="old@example.test", openalex_key_env="LAB_OA",
        tavily_enabled=True, tavily_key_env="LAB_TV",
        parallel_enabled=True, parallel_key_env="LAB_PA", parallel_mode="turbo")


def _wizard(choices, answers=()):
    choices, answers = iter(choices), iter(answers)
    menus = []
    def choose(title, options):
        menus.append((title, options))
        result = next(choices)
        assert result in dict(options)
        return result
    return SimpleNamespace(output=io.StringIO(), language="zh",
                           _choose_one=choose, input=lambda _: next(answers), menus=menus)


def test_settings_first_choose_backend_and_back_never_rewrites(configured):
    before = configured.read_bytes()
    wizard = _wizard(["back"])
    configure_search_interactively(wizard, seed_path=configured)
    assert [key for key, _ in wizard.menus[0][1]] == ["openalex", "tavily", "parallel", "back"]
    assert all("密钥已检测，API 未验证" in label for key, label in wizard.menus[0][1] if key != "back")
    assert "private-existing" not in wizard.output.getvalue()
    assert configured.read_bytes() == before


def test_keep_openalex_credentials_and_blank_email_is_noop(configured):
    before = configured.read_bytes()
    configure_search_interactively(_wizard(["openalex", "edit", "keep"], [""]), seed_path=configured)
    assert configured.read_bytes() == before


def test_edit_parallel_does_not_touch_other_backends_or_defaults(configured):
    before = tomllib.loads(configured.read_text())["research"]
    configure_search_interactively(_wizard(["parallel", "edit", "keep", "fast"], ["y"]), seed_path=configured)
    after = tomllib.loads(configured.read_text())["research"]
    assert after["tavily"] == before["tavily"]
    assert after["openalex_api_key_env"] == "LAB_OA"
    assert after["openalex_contact_email"] == "old@example.test"
    assert after["parallel"]["api_key_env"] == "LAB_PA"
    assert after["parallel"]["mode"] == "fast"


def test_cancel_key_replacement_does_not_store_or_overwrite(configured, monkeypatch):
    before = configured.read_bytes()
    monkeypatch.setattr("project_ensemble.search_backend_settings.store_secret",
                        lambda *args: pytest.fail("cancelled edit must not store a key"))
    configure_search_interactively(_wizard(["openalex", "edit", "input"], ["", "", ""]),
                                  seed_path=configured, secret_input=lambda _: "new-key")
    assert configured.read_bytes() == before


def test_health_test_never_changes_configuration(configured, monkeypatch):
    before = configured.read_bytes()
    calls = []
    def get(client, url, *, headers):
        calls.append((url, headers))
        return httpx.Response(200, json={"rate_limit": {"credits_remaining": 0}},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.Client, "get", get)
    wizard = _wizard(["openalex", "test"])
    configure_search_interactively(wizard, seed_path=configured)
    assert configured.read_bytes() == before
    assert calls[0][0].endswith("/rate-limit")
    assert calls[0][1] == {"Authorization": "Bearer private-existing-oa"}
    assert "API 验证可用" in wizard.output.getvalue()
    assert "remaining: 0" in wizard.output.getvalue()
    assert "private-existing-oa" not in wizard.output.getvalue()


def test_parallel_probe_needs_explicit_paid_authorization(configured, monkeypatch):
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: pytest.fail("no paid consent"))
    result = probe_search_backend(load_config(configured).research, "parallel")
    assert result["status"] == "NOT_TESTED"
    wizard = _wizard(["parallel", "test"], [""])
    configure_search_interactively(wizard, seed_path=configured)
    assert "未发出计费请求" in wizard.output.getvalue()


@pytest.mark.parametrize("status,expected", [(401, "AUTH_FAILED"), (403, "AUTH_FAILED"),
                                           (429, "RATE_LIMITED"), (503, "UNAVAILABLE")])
def test_diagnostics_redact_response_and_distinguish_limits(configured, monkeypatch, status, expected):
    monkeypatch.setattr(httpx.Client, "get", lambda client, url, **kwargs: httpx.Response(
        status, json={"error": "private-existing-oa"}, request=httpx.Request("GET", url)))
    result = probe_search_backend(load_config(configured).research, "openalex")
    assert result["status"] == expected
    assert "private-existing" not in str(result)


def test_only_explicit_search_override_applies_to_project(configured, tmp_path):
    project = tmp_path / "project.toml"
    project.write_text('[research]\nopenalex_api_key_env="PROJECT_OA"\n'
                       '[research.tavily]\nenabled=true\napi_key_env="PROJECT_TV"\n')
    # No explicit override yet: existing project credentials win.
    assert cli._load_config(project).research.openalex_api_key_env == "PROJECT_OA"
    save_search_backend("openalex", {"openalex_api_key_env": "LAB_OA"}, seed_path=project)
    loaded = cli._load_config(project)
    assert loaded.research.openalex_api_key_env == "LAB_OA"
    assert loaded.research.tavily.api_key_env == "PROJECT_TV"
    assert effective_search_config(project).research.openalex_api_key_env == "LAB_OA"


def test_disabling_active_project_backend_retains_all_its_options(configured, tmp_path):
    project = tmp_path / "active.toml"
    project.write_text('[research.tavily]\nenabled=true\nbase_url="https://custom.test"\n'
                       'api_key_env="LAB_TV"\nmax_results_per_query=3\nextract_enabled=true\n')
    configure_search_interactively(_wizard(["tavily", "disable"], ["y"]), seed_path=project)
    owner = cli._load_config(project).research.tavily
    assert not owner.enabled
    assert owner.base_url == "https://custom.test"
    assert owner.api_key_env == "LAB_TV"
    assert owner.max_results_per_query == 3
    assert owner.extract_enabled


def test_new_secret_is_separate_and_blank_env_retains_custom_name(configured):
    path = configure_search_interactively(_wizard(["openalex", "edit", "input"], ["", "", "y"]),
                                         seed_path=configured, secret_input=lambda _: "new-key")
    owner = load_config(path).research
    assert owner.openalex_api_key_env == "LAB_OA"
    assert owner.openalex_api_key_file
    assert "new-key" not in path.read_text()
    # Environment precedence is visible: the existing env value still wins.
    assert search_backend_state(owner, "openalex")["source"] == "environment"


def test_initialization_can_manage_api_then_select_engine(configured, monkeypatch):
    from project_ensemble.startup import TerminalWizard
    answers = iter(["4", "3"])
    wizard = TerminalWizard(input_fn=lambda _: next(answers), output=io.StringIO())
    config = load_config(configured)
    calls = []
    monkeypatch.setattr("project_ensemble.search_backend_settings.configure_search_interactively",
                        lambda *args, **kwargs: calls.append(kwargs["seed_path"]))
    assert wizard._choose_general_search_engine(config) == "parallel"
    assert calls == [configured]
    assert "API 配置不会自动授权会议调用" in wizard.output.getvalue()


def test_initialization_returning_from_settings_does_not_enable_search(configured, monkeypatch):
    from project_ensemble.startup import TerminalWizard
    answers = iter(["4", ""])
    wizard = TerminalWizard(input_fn=lambda _: next(answers), output=io.StringIO())
    monkeypatch.setattr("project_ensemble.search_backend_settings.configure_search_interactively",
                        lambda *args, **kwargs: None)
    assert wizard._choose_general_search_engine(load_config(configured)) == "disabled"
