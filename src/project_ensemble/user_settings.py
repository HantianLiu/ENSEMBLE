"""User-scoped first-run configuration; never writes credentials into a meeting.

The wizard keeps the canonical model configuration in the user's config directory.
Existing source-checkout configuration is copied on first use, not edited.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, TextIO
from urllib.parse import urlparse

from project_ensemble.config import EnsembleConfig, ProviderConfig, load_config
from project_ensemble.interface_language import ui_label
from project_ensemble.selection_input import parse_number_selection


def settings_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("APPDATA")
        return Path(root).expanduser() / "ensemble" if root else Path.home() / "AppData/Roaming/ensemble"
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".config") / "ensemble"


def user_config_path() -> Path:
    return settings_dir() / "ensemble.toml"


def _removed_providers_path() -> Path:
    return settings_dir() / "removed_providers.toml"


def _hidden_models_path() -> Path:
    return settings_dir() / "hidden_models.toml"


def removed_provider_ids() -> set[str]:
    try:
        data = tomllib.loads(_removed_providers_path().read_text(encoding="utf-8"))
        return set(data.get("catalog", {}).get("removed", []))
    except (OSError, tomllib.TOMLDecodeError, AttributeError, TypeError):
        return set()


def _save_removed_provider_ids(provider_ids: set[str]) -> None:
    rendered = "[catalog]\nremoved = " + _value(sorted(provider_ids)) + "\n"
    tomllib.loads(rendered)
    _atomic_write(_removed_providers_path(), rendered)


def _clear_provider_removal(provider_id: str) -> None:
    removed = removed_provider_ids()
    if provider_id in removed:
        removed.remove(provider_id)
        _save_removed_provider_ids(removed)


def hidden_model_ids(provider_id: str | None = None) -> set[str] | dict[str, set[str]]:
    """Return user-hidden model IDs, scoped by provider and independent of meetings."""
    try:
        data = tomllib.loads(_hidden_models_path().read_text(encoding="utf-8"))
        raw = data.get("hidden_models", {})
        if not isinstance(raw, dict):
            return set() if provider_id is not None else {}
        result = {
            str(key): {str(model_id) for model_id in model_ids if isinstance(model_id, str) and model_id}
            for key, model_ids in raw.items()
            if isinstance(model_ids, list)
        }
    except (OSError, tomllib.TOMLDecodeError, AttributeError, TypeError):
        result = {}
    if provider_id is not None:
        return result.get(provider_id, set())
    return result


def set_model_visibility(provider_id: str, model_ids: list[str], *, hidden: bool) -> Path:
    """Hide or reveal a batch of provider models for future model-selection menus."""
    if not provider_id or any(not isinstance(model_id, str) or not model_id.strip() for model_id in model_ids):
        raise ValueError("provider and model IDs must be non-empty")
    catalog = hidden_model_ids()
    assert isinstance(catalog, dict)
    selected = set(model_ids)
    current = catalog.setdefault(provider_id, set())
    if hidden:
        current.update(selected)
    else:
        current.difference_update(selected)
    if not current:
        catalog.pop(provider_id, None)
    lines = ["[hidden_models]"]
    for key, values in sorted(catalog.items()):
        lines.append(f"{json.dumps(key, ensure_ascii=False)} = {_value(sorted(values))}")
    rendered = "\n".join(lines) + "\n"
    tomllib.loads(rendered)
    path = _hidden_models_path()
    _atomic_write(path, rendered)
    return path


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(str(key), ensure_ascii=False)} = {_value(item)}" for key, item in value.items() if item is not None) + " }"
    raise TypeError(f"unsupported TOML value {type(value).__name__}")


def _table(path: str, mapping: dict) -> str:
    lines = [f"[{path}]"]
    nested = []
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, dict) and key in {"tavily", "parallel", "email"}:
            nested.append((key, value))
        else:
            lines.append(f"{json.dumps(str(key), ensure_ascii=False)} = {_value(value)}")
    for key, value in nested:
        lines.extend(("", _table(f"{path}.{key}", value)))
    return "\n".join(lines)


def _atomic_write(path: Path, content: str, *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging = tempfile.mkstemp(prefix=".ensemble-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(staging, 0o600 if secret else 0o644)
        os.replace(staging, path)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def ensure_user_config(seed_path: str | Path | None = None) -> Path:
    """Create an independent user configuration, retaining current provider settings."""
    target = user_config_path()
    if target.is_file():
        return target
    config = load_config(seed_path) if seed_path else EnsembleConfig()
    data = config.model_dump()
    data["project"]["governance_docs"] = "@package"
    data["project"]["model_config_file"] = "./model_config.toml"
    if seed_path:
        # load_config has already resolved this path relative to the source file.
        data["project"]["workspace"] = config.project.workspace
    model_path = target.parent / "model_config.toml"
    provider_text = "# User-level model configuration; do not commit secrets.\n\n"
    provider_text += "\n\n".join(_table(f"providers.{json.dumps(pid)}", value) for pid, value in data.pop("providers").items())
    config_text = "# User-level ENSEMBLE configuration.\n\n"
    config_text += "\n\n".join(_table(key, value) for key, value in data.items()) + "\n"
    _atomic_write(model_path, provider_text + "\n")
    _atomic_write(target, config_text)
    load_config(target)
    if seed_path:
        # Move only index metadata; source meetings and their frozen configuration
        # remain untouched. The import is local to avoid a module-level cycle.
        from project_ensemble.storage.meeting_index import indexed_meetings, register_meeting

        for entry in indexed_meetings(seed_path):
            register_meeting(entry.path, target)
    return target


def add_provider(provider_id: str, provider: ProviderConfig, *, seed_path: str | Path | None = None) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", provider_id):
        raise ValueError("供应商代号须以小写英文字母开头，只能包含小写字母、数字、-、_")
    config_path = ensure_user_config(seed_path)
    model_path = config_path.parent / "model_config.toml"
    existing = tomllib.loads(model_path.read_text(encoding="utf-8"))
    if provider_id in existing.get("providers", {}):
        raise ValueError(f"供应商代号 {provider_id} 已存在；请使用新代号以免覆盖已有会议配置")
    updated = model_path.read_text(encoding="utf-8").rstrip()
    updated += "\n\n" + _table(f"providers.{json.dumps(provider_id)}", provider.model_dump()) + "\n"
    # Validate before replacing the currently usable catalog.
    tomllib.loads(updated)
    _atomic_write(model_path, updated)
    load_config(config_path)
    _clear_provider_removal(provider_id)
    return config_path


def _write_provider_catalog(config_path: Path, providers: dict[str, dict]) -> Path:
    model_path = config_path.parent / "model_config.toml"
    rendered = "# User-level model configuration; do not commit secrets.\n\n"
    rendered += "\n\n".join(
        _table(f"providers.{json.dumps(provider_id)}", fields)
        for provider_id, fields in providers.items()
    ) + "\n"
    tomllib.loads(rendered)
    _atomic_write(model_path, rendered)
    load_config(config_path)
    return config_path


def update_provider(
    provider_id: str, provider: ProviderConfig, *, seed_path: str | Path | None = None
) -> Path:
    config_path = ensure_user_config(seed_path)
    model_path = config_path.parent / "model_config.toml"
    data = tomllib.loads(model_path.read_text(encoding="utf-8"))
    data.setdefault("providers", {})[provider_id] = provider.model_dump(exclude_none=True)
    path = _write_provider_catalog(config_path, data["providers"])
    _clear_provider_removal(provider_id)
    return path


def remove_provider(provider_id: str, *, seed_path: str | Path | None = None) -> Path:
    config_path = ensure_user_config(seed_path)
    model_path = config_path.parent / "model_config.toml"
    data = tomllib.loads(model_path.read_text(encoding="utf-8"))
    data.setdefault("providers", {}).pop(provider_id, None)
    path = _write_provider_catalog(config_path, data["providers"])
    removed = removed_provider_ids()
    removed.add(provider_id)
    _save_removed_provider_ids(removed)
    return path


def replace_managed_secret(provider_id: str, env_name: str, secret: str) -> Path:
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
        raise ValueError("环境变量名只能包含大写字母、数字和下划线")
    if not secret or "\n" in secret or "\r" in secret:
        raise ValueError("API 密钥不能为空或含换行")
    path = settings_dir() / "secrets" / f"{provider_id}.sh"
    _atomic_write(path, f"{env_name}={shlex.quote(secret)}\n", secret=True)
    return path


def store_secret(provider_id: str, env_name: str, secret: str) -> Path:
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
        raise ValueError("环境变量名只能包含大写字母、数字和下划线")
    if not secret or "\n" in secret or "\r" in secret:
        raise ValueError("API 密钥不能为空或含换行")
    path = settings_dir() / "secrets" / f"{provider_id}.sh"
    if path.exists():
        raise ValueError(f"密钥文件已存在，未覆盖：{path}")
    _atomic_write(path, f"{env_name}={shlex.quote(secret)}\n", secret=True)
    return path


def save_appearance(color: str, width: int) -> Path:
    if color not in {"auto", "always", "never"} or width not in {80, 100, 120}:
        raise ValueError("无效的外观选项")
    path = settings_dir() / "appearance.toml"
    _atomic_write(path, f'[appearance]\ncolor = "{color}"\nwidth = {width}\n')
    return path


def interface_language() -> str | None:
    """Return the user-level UI language; None means first-run selection is due."""
    try:
        data = tomllib.loads((settings_dir() / "interface.toml").read_text(encoding="utf-8"))
        language = data["interface"]["language"]
        return language if language in {"zh", "en"} else None
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None


def report_language_default() -> str:
    """Default for future report-language menus; never rewrites existing meetings."""
    try:
        data = tomllib.loads((settings_dir() / "report_language.toml").read_text(encoding="utf-8"))
        language = data["report"]["language"]
        return language if language in {"zh", "en", "fr"} else "zh"
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return "zh"


def save_report_language_default(language: str) -> Path:
    if language not in {"zh", "en", "fr"}:
        raise ValueError("unsupported report language")
    path = settings_dir() / "report_language.toml"
    _atomic_write(path, f'[report]\nlanguage = "{language}"\n')
    return path


def save_interface_language(language: str) -> Path:
    if language not in {"zh", "en"}:
        raise ValueError("interface language must be zh or en")
    path = settings_dir() / "interface.toml"
    _atomic_write(path, f'[interface]\nlanguage = "{language}"\n')
    return path


FRESHNESS_PRESETS: dict[str, tuple[int, int, int]] = {
    "short": (3, 14, 90),
    "standard": (7, 30, 180),
    "long": (14, 60, 365),
}


def save_freshness_preset(preset: str, *, seed_path: str | Path | None = None) -> Path:
    if preset not in FRESHNESS_PRESETS:
        raise ValueError("未知证据包有效期档位")
    path = ensure_user_config(seed_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    volatile, versioned, stable = FRESHNESS_PRESETS[preset]
    research = data.setdefault("research", {})
    research["volatile_freshness_days"] = volatile
    research["versioned_freshness_days"] = versioned
    research["stable_freshness_days"] = stable
    _write_user_config(path, data)
    load_config(path)
    return path


def _write_user_config(path: Path, data: dict) -> None:
    rendered = "# User-level ENSEMBLE configuration.\n\n"
    rendered += "\n\n".join(_table(key, value) for key, value in data.items()) + "\n"
    tomllib.loads(rendered)
    _atomic_write(path, rendered)


def save_search_backends(
    *, openalex_email: str | None, openalex_key_env: str = "OPENALEX_API_KEY",
    openalex_key_file: str | None = None, tavily_enabled: bool = False,
    tavily_key_env: str = "TAVILY_API_KEY", tavily_key_file: str | None = None,
    parallel_enabled: bool | None = None, parallel_key_env: str = "PARALLEL_API_KEY",
    parallel_key_file: str | None = None, parallel_mode: str = "fast",
    seed_path: str | Path | None = None,
) -> Path:
    path = ensure_user_config(seed_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    research = data.setdefault("research", {})
    research["retriever"] = "openalex"
    research["openalex_contact_email"] = openalex_email
    research["openalex_api_key_env"] = openalex_key_env
    research["openalex_api_key_file"] = openalex_key_file
    tavily = research.setdefault("tavily", {})
    tavily["enabled"] = tavily_enabled
    tavily["api_key_env"] = tavily_key_env
    tavily["api_key_file"] = tavily_key_file
    if parallel_enabled is not None:
        if parallel_mode not in {"fast", "turbo"}:
            raise ValueError("Parallel mode must be fast or turbo")
        parallel = research.setdefault("parallel", {})
        parallel.update(enabled=parallel_enabled, api_key_env=parallel_key_env,
                        api_key_file=parallel_key_file, mode=parallel_mode,
                        max_results_per_query=10)
    _write_user_config(path, data)
    load_config(path)
    return path


def _settings_discovery_config(config_path: Path, seed_path: str | Path | None):
    config = load_config(config_path)
    if seed_path:
        source = load_config(seed_path)
        providers = dict(source.providers)
        providers.update(config.providers)
        config = config.model_copy(update={"providers": providers})
    return config


def _select_model_visibility_batch(
    wizard,
    *,
    provider_id: str,
    model_ids: list[str],
    hide: bool,
    t,
    output: TextIO,
) -> None:
    candidates = sorted(set(model_ids))
    if not candidates:
        print(t("没有符合条件的模型。", "No matching models."), file=output)
        return
    query = wizard.input(t(
        "按模型 ID 搜索（留空查看全部，输入 q 返回）: ",
        "Search model IDs (blank for all, q to return): ",
    )).strip()
    if query.lower() in {"q", "quit", "b", "back"}:
        return
    if query:
        candidates = [model_id for model_id in candidates if query.casefold() in model_id.casefold()]
    if not candidates:
        print(t("没有匹配模型；隐藏状态未改变。", "No models matched; visibility is unchanged."), file=output)
        return

    page_size = 40
    page = 0
    while True:
        page_count = (len(candidates) + page_size - 1) // page_size
        page = min(max(page, 0), page_count - 1)
        page_models = candidates[page * page_size:(page + 1) * page_size]
        print(t(
            f"\n模型窗口 · 匹配 {len(candidates)} 个 · 第 {page + 1}/{page_count} 页",
            f"\nModel window · {len(candidates)} matches · page {page + 1}/{page_count}",
        ), file=output)
        for index, model_id in enumerate(page_models, 1):
            print(f"  {index:>2}. {model_id}", file=output)
        action = wizard.input(t(
            "输入编号（空格/逗号分隔，1-3 表示连续编号），n 下一页，p 上一页，q 返回: ",
            "Enter numbers (spaces/commas; 1-3 selects a range), n next, p previous, q back: ",
        )).strip().lower()
        if action in {"q", "b", "back", ""}:
            return
        if action == "n":
            page = (page + 1) % page_count
            continue
        if action == "p":
            page = (page - 1) % page_count
            continue
        indexes = parse_number_selection(action, len(page_models))
        if not indexes:
            print(t("编号无效；状态未改变。", "Invalid selection; visibility is unchanged."), file=output)
            continue
        selected = [page_models[index - 1] for index in sorted(set(indexes))]
        set_model_visibility(provider_id, selected, hidden=hide)
        print(t(
            f"已{'隐藏' if hide else '恢复显示'} {len(selected)} 个模型；可继续另一轮筛选。",
            f"{len(selected)} model(s) {'hidden' if hide else 'restored'}; you can run another round.",
        ), file=output)
        return


def _manage_model_visibility_interactively(
    wizard,
    *,
    provider_id: str,
    seed_path: str | Path | None,
    t,
    output: TextIO,
    initial_catalog: list | None = None,
) -> Path | None:
    """Fetch the live catalog, then manage a persistent provider-scoped hidden list."""
    config_path = ensure_user_config(seed_path)
    catalog = list(initial_catalog) if initial_catalog is not None else None
    catalog_available = catalog is not None
    while True:
        if catalog is None:
            try:
                from project_ensemble.startup import discover_models

                config = _settings_discovery_config(config_path, seed_path)
                catalog = discover_models(config, [provider_id], include_hidden=True)
                catalog_available = True
            except Exception as exc:
                print(t(
                    f"实时模型目录暂时无法读取：{exc}。仍可修改已保存的隐藏清单，或在此重试。",
                    f"The live model catalog could not be fetched: {exc}. You can still edit the saved hidden list or retry here.",
                ), file=output)
                catalog = []
                catalog_available = False
        all_ids = sorted({str(model.model_id) for model in catalog})
        hidden = hidden_model_ids(provider_id)
        assert isinstance(hidden, set)
        current_ids = set(all_ids)
        visible_count = len(current_ids - hidden)
        if catalog_available:
            print(t(
                f"\n{provider_id} 模型目录：实时返回 {len(all_ids)} 个，可见 {visible_count} 个，隐藏 {len(hidden)} 个。",
                f"\n{provider_id} catalog: {len(all_ids)} live models, {visible_count} visible, {len(hidden)} hidden.",
            ), file=output)
        else:
            print(t(
                f"\n{provider_id} 隐藏清单：已保存 {len(hidden)} 个模型；当前目录不可用。",
                f"\n{provider_id} hidden list: {len(hidden)} saved models; the live catalog is unavailable.",
            ), file=output)
        options = []
        if catalog_available:
            options.append(("hide", t("搜索并隐藏模型（可批量、多轮）", "Search and hide models (bulk, repeatable)")))
        options.extend([
            ("expand", t(f"展开隐藏模型窗口（{len(hidden)} 个）", f"Expand hidden-model window ({len(hidden)})")),
            ("refresh", t("重新抓取供应商模型目录", "Refresh provider model catalog")),
            ("back", t("完成，返回", "Done, go back")),
        ])
        action = wizard._choose_one(t("模型可见性", "Model visibility"), options)
        if action == "back":
            return config_path
        if action == "refresh":
            catalog = None
            continue
        if action == "hide":
            candidates = sorted(current_ids - hidden)
            _select_model_visibility_batch(
                wizard, provider_id=provider_id, model_ids=candidates,
                hide=True, t=t, output=output,
            )
            continue
        if action == "expand":
            if not hidden:
                print(t("隐藏列表为空。", "The hidden list is empty."), file=output)
                continue
            print(t(
                "\n已隐藏模型（带 † 表示当前未出现在供应商目录中）：" if catalog_available else "\n已隐藏模型：",
                "\nHidden models († means not currently returned by the provider):" if catalog_available else "\nHidden models:",
            ), file=output)
            for index, model_id in enumerate(sorted(hidden), 1):
                marker = " †" if catalog_available and model_id not in current_ids else ""
                print(f"  {index:>3}. {model_id}{marker}", file=output)
            _select_model_visibility_batch(
                wizard, provider_id=provider_id, model_ids=sorted(hidden),
                hide=False, t=t, output=output,
            )


def appearance() -> tuple[str, int]:
    try:
        data = tomllib.loads((settings_dir() / "appearance.toml").read_text(encoding="utf-8"))["appearance"]
        color, width = data.get("color", "auto"), data.get("width", 100)
        if color in {"auto", "always", "never"} and width in {80, 100, 120}:
            return color, width
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    return "auto", 100


def _manage_provider_interactively(wizard, *, seed_path, secret_input, t, output):
    config_path = ensure_user_config(seed_path)
    config = load_config(config_path)
    providers = dict(load_config(seed_path).providers) if seed_path else {}
    providers.update(config.providers)
    providers = {key: value for key, value in providers.items() if key not in removed_provider_ids()}
    if not providers:
        print(t("目前没有已配置的供应商。", "No providers are configured."), file=output)
        return config_path
    choices = [
        (provider_id, f"{provider.display_name or provider_id} · {provider_id} · {provider.kind}")
        for provider_id, provider in providers.items()
    ]
    choices.append(("back", t("返回供应商设置", "Back to provider settings")))
    provider_id = wizard._choose_one(t("选择要管理的供应商", "Choose a provider to manage"), choices)
    if provider_id == "back":
        return None
    provider = providers[provider_id]
    action = wizard._choose_one(t(f"管理 {provider.display_name or provider_id}", f"Manage {provider.display_name or provider_id}"), [
        ("rename", t("修改显示名称", "Change display name")),
        ("url", t("修改 API 基础地址", "Change API base URL")),
        ("key", t("更换 API 密钥", "Replace API key")),
        ("models", t("模型可见性：实时抓取、隐藏或恢复模型", "Model visibility: fetch, hide, or restore models")),
        ("delete", t("删除供应商配置", "Delete provider configuration")),
        ("back", t("返回供应商列表", "Back to provider list")),
    ])
    if action == "back":
        return None
    if action == "models":
        return _manage_model_visibility_interactively(
            wizard, provider_id=provider_id, seed_path=seed_path, t=t, output=output,
        )
    if action == "rename":
        display_name = wizard.input(t("新的显示名称: ", "New display name: ")).strip()
        if not display_name:
            raise ValueError("供应商显示名称不能为空")
        updated = provider.model_copy(update={"display_name": display_name})
        path = update_provider(provider_id, updated, seed_path=seed_path)
        print(t("显示名称已保存。", "Display name saved."), file=output)
        return path
    if action == "url":
        if provider.kind not in {"openai_compatible", "gemini"}:
            print(t("此调用方式没有可编辑的 API 基础地址。", "This provider type has no editable API base URL."), file=output)
            return config_path
        url = wizard.input(t("新的 API 基础地址: ", "New API base URL: ")).strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("API 基础地址必须是完整的 http(s) URL")
        updated = provider.model_copy(update={"base_url": url})
        path = update_provider(provider_id, updated, seed_path=seed_path)
        print(t("API 基础地址已保存；后续会议和模型切换将使用新地址。", "API base URL saved; future meetings and model switches will use it."), file=output)
        return path
    if action == "key":
        if provider.kind == "codex_subscription":
            print(t("Codex 使用本机登录，不配置 API 密钥。", "Codex uses local login and has no API key."), file=output)
            return config_path
        env_name = "ENSEMBLE_" + re.sub(r"[^A-Z0-9_]", "_", provider_id.upper()) + "_MANAGED_KEY"
        secret = (secret_input or getpass.getpass)(t("新 API 密钥（输入不回显）: ", "New API key (input hidden): "))
        updated = provider.model_copy(update={
            "api_key_env": env_name,
            "api_key_file": str(settings_dir() / "secrets" / f"{provider_id}.sh"),
        })
        # Validate before replacing the managed secret.
        updated = ProviderConfig.model_validate(updated.model_dump())
        replace_managed_secret(provider_id, env_name, secret)
        path = update_provider(provider_id, updated, seed_path=seed_path)
        print(t("新密钥已安全保存到本机用户密钥库；不会写入会议目录。", "New key saved in the local user secret store; it is not written to meeting files."), file=output)
        return path
    if action == "delete":
        answer = wizard.input(t(
            f"确认删除供应商 {provider.display_name or provider_id}（{provider_id}）？依赖该供应商的未完成会议需重新添加配置后才能恢复。输入 DELETE {provider_id} 确认: ",
            f"Delete provider {provider.display_name or provider_id} ({provider_id})? Unfinished meetings that use it will need the provider re-added before they can resume. Type DELETE {provider_id} to confirm: ",
        )).strip()
        if answer != f"DELETE {provider_id}":
            print(t("已取消；配置未更改。", "Cancelled; configuration unchanged."), file=output)
            return config_path
        path = remove_provider(provider_id, seed_path=seed_path)
        print(t("供应商配置已删除；本机密钥文件（如有）保留，避免误删其他用途的凭据。", "Provider configuration deleted; any local key file is retained to avoid removing credentials used elsewhere."), file=output)
        return path
    return None


def configure_interactively(wizard, *, seed_path: str | Path | None = None, secret_input: Callable[[str], str] | None = None) -> Path | None:
    """Guide one settings action. None means back to the home menu."""
    output: TextIO = wizard.output
    def t(chinese: str, english: str) -> str:
        return ui_label(chinese, english, getattr(wizard, "language", "zh"))
    action = wizard._choose_one("设置", [
        ("language", "语言：界面语言及未来会议正文语言默认值"),
        ("appearance", "界面外观"),
        ("provider", "模型供应商"),
        ("search", "联网搜索后端：OpenAlex / Tavily / Parallel（查看、检测或单独修改）"),
        ("freshness", "证据包有效期"),
        ("back", "返回首页"),
    ])
    if action == "back":
        return None
    if action == "language":
        target = wizard._choose_one("语言设置 / Language settings", [
            ("interface", "界面语言（中文 / English；不改变正文）"),
            ("report", "未来会议的报告语言默认值（中文 / English / français；每次仍可单独选择）"),
            ("back", "返回设置"),
        ])
        if target == "back":
            return None
        if target == "interface":
            language = wizard._choose_one("界面语言 / Interface language", [
                ("zh", "中文"), ("en", "English"),
            ])
            path = save_interface_language(language)
            if hasattr(wizard, "language"):
                wizard.language = language
            print(t("界面语言已保存；现有会议正文不变。",
                    "Interface language saved; existing meeting documents are unchanged."), file=output)
        else:
            language = wizard._choose_one("未来报告默认成文语言", [
                ("zh", "中文"), ("en", "English"), ("fr", "français"),
            ])
            path = save_report_language_default(language)
            print(t("报告语言默认值已保存；只影响未来会议的初始选项。",
                    "Default report language saved; only future meeting menus are affected."), file=output)
        return path
    if action == "appearance":
        color = wizard._choose_one("颜色", [("auto", "自动检测终端"), ("always", "始终使用颜色"), ("never", "不使用颜色")])
        width = wizard._choose_one("表格宽度", [("80", "紧凑（80 列）"), ("100", "标准（100 列）"), ("120", "宽屏（120 列）")])
        path = save_appearance(color, int(width))
        print(t(f"外观设置已保存：{path}；新建的界面立即生效。", f"Appearance saved to {path}; new screens use it immediately."), file=output)
        return path
    if action == "freshness":
        preset = wizard._choose_one("证据包缓存最大复用时长", [
            ("short", "短：时效事实 3 天／版本资料 14 天／稳定学术事实 90 天"),
            ("standard", "标准：7／30／180 天（推荐）"),
            ("long", "长：14／60／365 天；复用更多，但过期资料风险较高"),
        ])
        path = save_freshness_preset(preset, seed_path=seed_path)
        print(t(f"已保存到 {path}；只影响以后创建的会议，不改动已冻结会议。", f"Saved to {path}; affects only future meetings, not frozen ones."), file=output)
        return path
    if action == "search":
        from project_ensemble.search_backend_settings import configure_search_interactively
        return configure_search_interactively(wizard, seed_path=seed_path, secret_input=secret_input)
    provider_action = wizard._choose_one("模型供应商", [
        ("add", t("添加供应商", "Add a provider")),
        ("manage", t("管理已有供应商：改名、修改网址、更换密钥、模型可见性或删除", "Manage providers: rename, URL, key, model visibility, or delete")),
        ("back", t("返回设置", "Back to Settings")),
    ])
    if provider_action == "back":
        return None
    if provider_action == "manage":
        return _manage_provider_interactively(
            wizard, seed_path=seed_path, secret_input=secret_input, t=t, output=output
        )
    existing_path = user_config_path() if user_config_path().is_file() else seed_path
    if existing_path:
        existing_providers = load_config(existing_path).providers
        if existing_providers:
            print(t("已配置供应商（本向导新增代号，不覆盖已有条目）：", "Configured providers (this wizard adds a new ID; it does not overwrite existing entries):"), file=output)
            for existing_id, existing in existing_providers.items():
                print(f"  {existing_id} · {existing.display_name or existing_id}", file=output)
    kind = wizard._choose_one("调用方式", [
        ("lithosai", "LithosAI（OpenAI 兼容 API；自动填入官方地址）"),
        ("siliconflow", "SiliconFlow（OpenAI 兼容 API；自动配置推理档位）"),
        ("openai_compatible", "OpenAI 兼容 API（DeepSeek、GLM、Kimi 等）"),
        ("gemini", "Google Gemini 原生 API"),
        ("codex_subscription", "Codex CLI；使用本机 ChatGPT 登录，不输入 API 密钥"),
        ("claude_code", "Claude Code CLI；可选本机登录或 API 密钥"),
        ("back", "返回设置"),
    ])
    if kind == "back":
        return None
    lithosai_preset = kind == "lithosai"
    siliconflow_preset = kind == "siliconflow"
    if lithosai_preset or siliconflow_preset:
        kind = "openai_compatible"
        name = "LithosAI" if lithosai_preset else "SiliconFlow"
        provider_id = "lithos" if lithosai_preset else "siliconflow"
        if existing_path and provider_id in existing_providers:
            print(t(
                f"{name} 已配置；没有覆盖现有设置。",
                f"{name} is already configured; existing settings were left unchanged.",
            ), file=output)
            return existing_path
    else:
        name = wizard.input("供应商显示名称（可用中文）: ").strip()
        if not name:
            raise ValueError("供应商显示名称不能为空")
        provider_id = wizard.input("供应商代号（小写英文，会议记录将使用它，如 my_glm）: ").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", provider_id):
        raise ValueError("供应商代号须以小写英文字母开头，只能包含小写字母、数字、-、_")
    if existing_path and provider_id in existing_providers:
        raise ValueError(f"供应商代号 {provider_id} 已存在；请选择另一代号")
    fields: dict = {"kind": kind, "display_name": name, "timeout_seconds": 1200.0}
    if lithosai_preset:
        fields.update({
            "reasoning_effort_transport": "openai",
            "reasoning_effort_map": {"low": "low", "medium": "high", "high": "max"},
            "reasoning_effort_model_patterns": ["*kimi-k3*"],
        })
    if siliconflow_preset:
        fields.update({
            "reasoning_effort_transport": "openai",
            "reasoning_effort_map": {"high": "xhigh"},
            "reasoning_effort_model_patterns": [
                "Pro/deepseek-ai/DeepSeek-V4",
                "deepseek-ai/DeepSeek-V4-Flash",
                "Pro/zai-org/GLM-5.2",
            ],
        })
    pending_secret: tuple[str, str, str] | None = None
    if kind in {"openai_compatible", "gemini"}:
        default_url = (
            "https://generativelanguage.googleapis.com/v1beta" if kind == "gemini"
            else "https://api.lithosai.cloud/v1" if lithosai_preset
            else "https://api.siliconflow.cn/v1" if siliconflow_preset
            else ""
        )
        if lithosai_preset or siliconflow_preset:
            url = default_url
            print(t(f"{name} API 地址：{url}", f"{name} API base URL: {url}"), file=output)
        else:
            url = wizard.input(t(
                f"API 基础地址{f' [默认 {default_url}]' if default_url else ''}: ",
                f"API base URL{f' [default {default_url}]' if default_url else ''}: ",
            )).strip() or default_url
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("API 基础地址必须是完整的 http(s) URL，例如 https://host/v1")
        fields["base_url"] = url
    if kind == "claude_code":
        fields["claude_command"] = wizard.input("Claude CLI 命令 [claude]: ").strip() or "claude"
    if kind == "codex_subscription":
        fields["codex_command"] = wizard.input("Codex CLI 命令 [codex]: ").strip() or "codex"
    if kind == "claude_code":
        auth = wizard._choose_one("Claude Code 凭据", [("login", "使用现有 claude auth login 登录"), ("key", "使用 API 密钥")])
    else:
        auth = "login" if kind == "codex_subscription" else "key"
    if auth == "key":
        default_env = (
            "LITHOSAI_API_KEY" if lithosai_preset else
            "SILICONFLOW_API_KEY" if siliconflow_preset else
            re.sub(r"[^A-Z0-9_]", "_", provider_id.upper()) + "_API_KEY"
        )
        env_name = default_env if (lithosai_preset or siliconflow_preset) else (
            wizard.input(t(f"环境变量名称 [{default_env}]: ", f"Environment variable name [{default_env}]: ")).strip()
            or default_env
        )
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
            raise ValueError("环境变量名只能包含大写字母、数字和下划线")
        fields["api_key_env"] = env_name
        source = wizard._choose_one("密钥来源", [
            ("env", "引用现有全局环境变量；不保存密钥"),
            ("file", "引用已有的外部密钥文件；不复制密钥"),
            ("input", "现在输入密钥，保存至本机用户配置目录"),
        ])
        if source == "file":
            fields["api_key_file"] = wizard.input("密钥文件路径（包含 NAME=value；不会执行文件）: ").strip()
            if not Path(fields["api_key_file"]).expanduser().is_file():
                raise ValueError("密钥文件不存在")
            fields["api_key_file"] = str(Path(fields["api_key_file"]).expanduser().resolve())
        elif source == "input":
            secret = (secret_input or getpass.getpass)("API 密钥（输入不回显）: ")
            fields["api_key_file"] = str(settings_dir() / "secrets" / f"{provider_id}.sh")
            pending_secret = (provider_id, env_name, secret)
        elif not os.environ.get(env_name):
            print(t(f"提示：当前进程尚未检测到 {env_name}；使用前请设置该环境变量。", f"Note: {env_name} is not set in this process; set it before using this provider."), file=output)
    if kind == "claude_code":
        raw = wizard.input("可用模型 ID（逗号分隔，须来自你的 Claude Code 账户）: ").strip()
        fields["selectable_models"] = [part.strip() for part in raw.split(",") if part.strip()]
    if kind == "codex_subscription":
        fields["max_concurrent_requests"] = 1
    provider = ProviderConfig.model_validate(fields)
    if pending_secret:
        store_secret(*pending_secret)
    path = add_provider(provider_id, provider, seed_path=seed_path)
    print(t(f"供应商 {name} 已加入：{path.parent / 'model_config.toml'}", f"Provider {name} added to {path.parent / 'model_config.toml'}"), file=output)
    print(t("密钥值不会写入会议文件或项目目录。运行 ensemble doctor 检查配置。", "Secret values are not written into meeting files or the project directory. Run ensemble doctor to check configuration."), file=output)
    if kind in {"openai_compatible", "gemini"}:
        print(t("现在抓取该供应商当前可用的模型；隐藏项会保存在本机设置，可之后多轮调整。",
                "Fetching this provider's current model catalog; hidden models are saved in local settings and can be adjusted in multiple rounds."), file=output)
        return _manage_model_visibility_interactively(
            wizard, provider_id=provider_id, seed_path=seed_path, t=t, output=output,
        )
    return path
