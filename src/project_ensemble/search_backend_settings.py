"""Provider-first search settings; edit only the explicitly selected backend."""
from __future__ import annotations

import os
import re
import secrets
import tomllib
from pathlib import Path

from project_ensemble.config import EnsembleConfig, ParallelResearchConfig, ResearchConfig, TavilyResearchConfig, load_config
from project_ensemble.interface_language import ui_label
from project_ensemble.providers.search_health import probe_search_backend
from project_ensemble.user_settings import (
    _write_user_config, ensure_user_config, store_secret, user_config_path,
)


_OPENALEX_FIELDS = ("openalex_base_url", "openalex_contact_email",
                    "openalex_api_key_env", "openalex_api_key_file")


def merge_search_backend_settings(config, user_config):
    """Apply explicit user overrides, preserving all unselected project policies."""
    research = config.research
    changes = {}
    for backend in user_config.research.search_backend_overrides:
        if backend == "openalex":
            changes.update({name: getattr(user_config.research, name) for name in _OPENALEX_FIELDS})
        else:
            changes[backend] = getattr(user_config.research, backend)
    # Compatibility: previous releases allowed Settings to add Parallel.
    if "parallel" not in changes and not research.parallel.enabled and user_config.research.parallel.enabled:
        changes["parallel"] = user_config.research.parallel
    return config.model_copy(update={"research": research.model_copy(update=changes)})


def effective_search_config(seed_path=None):
    user_path = user_config_path()
    if seed_path:
        current = load_config(seed_path)
        if user_path.is_file() and user_path.resolve() != Path(seed_path).expanduser().resolve():
            return merge_search_backend_settings(current, load_config(user_path))
        return current
    return load_config(user_path) if user_path.is_file() else EnsembleConfig()


def search_backend_state(research, backend):
    owner = research if backend == "openalex" else getattr(research, backend)
    env = owner.openalex_api_key_env if backend == "openalex" else owner.api_key_env
    file = owner.openalex_api_key_file if backend == "openalex" else owner.api_key_file
    try:
        key = owner.openalex_api_key() if backend == "openalex" else owner.api_key()
        error = False
    except (OSError, ValueError):
        key, error = None, True
    return {"enabled": backend == "openalex" or owner.enabled, "credential_detected": bool(key),
            "source": "environment" if env and os.environ.get(env) else "file" if file and key else "none",
            "env": env, "file": file, "credential_error": error}


def save_search_backend(backend, updates, *, seed_path=None):
    """Single-backend atomic configuration update; no other credentials are reset."""
    path = ensure_user_config(seed_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    research = data.setdefault("research", {})
    allowed = set(_OPENALEX_FIELDS) if backend == "openalex" else (
        set(ParallelResearchConfig.model_fields) if backend == "parallel" else
        set(TavilyResearchConfig.model_fields) if backend == "tavily" else set())
    if not allowed or not set(updates) <= allowed:
        raise ValueError("invalid search backend update")
    if backend == "openalex":
        research.update(updates)
    else:
        research.setdefault(backend, {}).update(updates)
    research["search_backend_overrides"] = sorted(set(
        research.get("search_backend_overrides", [])) | {backend})
    ResearchConfig.model_validate(research)
    _write_user_config(path, data)
    return path


def configure_search_interactively(wizard, *, seed_path=None, secret_input=None):
    output = wizard.output
    def t(zh, en):
        return ui_label(zh, en, getattr(wizard, "language", "zh"))
    config = effective_search_config(seed_path)
    states = {name: search_backend_state(config.research, name)
              for name in ("openalex", "tavily", "parallel")}
    options = []
    print(t("仅检查本机配置和凭据来源；尚未联网验证。选择一家供应商后可检测或修改，不会依次重配其他供应商。",
            "Local configuration and credentials only; not yet API-verified. Select one provider to test or edit; other providers are left untouched."), file=output)
    for name, state in states.items():
        status = t("已启用", "enabled") if state["enabled"] else t("未启用", "disabled")
        credential = t("凭据读取失败", "credential read failed") if state["credential_error"] else (
            t("密钥已检测，API 未验证", "key detected, API unverified") if state["credential_detected"]
            else t("未检测到密钥", "no key detected"))
        options.append((name, f"{name.title()} · {status} · {credential}"))
    options.append(("back", t("返回，不修改", "Back without changes")))
    backend = wizard._choose_one(t("选择搜索供应商", "Choose search provider"), options)
    if backend == "back":
        return None
    state = states[backend]
    print(t(f"当前凭据来源：{state['source']}；环境变量：{state['env'] or '无'}；密钥文件：{state['file'] or '无'}",
            f"Current credential source: {state['source']}; environment: {state['env'] or 'none'}; key file: {state['file'] or 'none'}"), file=output)
    actions = [("test", t("检测当前 API（不改配置）", "Test current API without changing configuration")),
               ("edit", t("修改此供应商（其他供应商不变）", "Edit this provider only"))]
    if backend != "openalex":
        actions.append(("disable", t("停用此供应商", "Disable this provider")))
    actions.append(("back", t("返回，不修改", "Back without changes")))
    action = wizard._choose_one(t("供应商操作", "Provider action"), actions)
    if action == "back":
        return None
    if action == "test":
        paid = False
        if backend == "parallel":
            answer = wizard.input(t("Parallel 检测需要一次可能计费的 fast 搜索（最多 1 条）；允许吗？[y/N]: ",
                                    "Parallel testing requires one potentially billed fast search (at most 1 result). Allow? [y/N]: ")).strip().lower()
            paid = answer in {"y", "yes"}
        result = probe_search_backend(config.research, backend, allow_paid_probe=paid)
        labels = {
            "AVAILABLE": t("API 验证可用", "API verified available"),
            "NO_CREDENTIAL": t("无可用密钥；未发出请求", "No usable key; no request sent"),
            "NOT_TESTED": t("未检测；未发出计费请求", "Not tested; no billed request sent"),
            "AUTH_FAILED": t("认证失败；配置保留", "Authentication failed; configuration retained"),
            "RATE_LIMITED": t("接口限流；不能据此判断密钥失效", "Rate-limited; does not establish an invalid key"),
            "UNAVAILABLE": t("连接或服务异常；不能据此判断密钥失效", "Connection/service error; does not establish an invalid key"),
            "UNEXPECTED_RESPONSE": t("响应格式异常，未确认可用", "Unexpected response; availability unconfirmed"),
        }
        print(f"{backend.title()} · {labels[result['status']]}", file=output)
        for key in ("remaining", "usage", "limit"):
            if key in result:
                print(f"  {key}: {result[key]}", file=output)
        return None
    if action == "disable":
        confirmed = wizard.input(t(f"仅停用 {backend.title()}，保留密钥与其他供应商配置？[y/N]: ",
                                   f"Disable only {backend.title()}, retaining credentials and other providers? [y/N]: ")).strip().lower()
        if confirmed not in {"y", "yes"}:
            return None
        owner = getattr(config.research, backend)
        return save_search_backend(backend, {**owner.model_dump(), "enabled": False}, seed_path=seed_path)
    choices = [("keep", t("保留当前凭据（默认，不覆盖）", "Keep current credentials (default, no overwrite)")),
               ("env", t("引用环境变量", "Use an environment variable")),
               ("file", t("引用已有密钥文件", "Use an existing key file")),
               ("input", t("输入新密钥，另存为新文件", "Enter a new key; save to a new file"))]
    if backend == "openalex":
        choices.append(("anonymous", t("明确改为匿名访问（不用已有密钥）", "Explicitly switch to anonymous access")))
    choices.append(("back", t("返回，不修改", "Back without changes")))
    source = wizard._choose_one(t("凭据设置", "Credential settings"), choices)
    if source == "back":
        return None
    updates, secret = {}, None
    owner = config.research if backend == "openalex" else getattr(config.research, backend)
    prefix = "openalex_" if backend == "openalex" else ""
    env, file = state["env"], state["file"]
    if source == "anonymous":
        env, file = "", None
    elif source in {"env", "file", "input"}:
        default_env = env or f"{backend.upper()}_API_KEY"
        env = wizard.input(t(f"环境变量名 [{default_env}]（回车保留）: ",
                             f"Environment variable [{default_env}] (Enter keeps it): ")).strip() or default_env
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env):
            print(t("环境变量名无效；配置未改变。", "Invalid environment name; nothing changed."), file=output)
            return None
        if source == "env":
            file = None
            if not os.environ.get(env):
                print(t("此环境变量当前未设置；保存引用不会自动建立密钥。", "This environment variable is unset; saving its name does not create a key."), file=output)
        elif source == "file":
            entered = wizard.input(t("密钥文件路径（回车保留当前文件；无当前文件则取消）: ",
                                     "Key file (Enter keeps current file, or cancels if none): ")).strip()
            file = entered or file
            if not file or not Path(file).expanduser().is_file():
                print(t("没有可用文件；配置未改变。", "No usable file; nothing changed."), file=output)
                return None
            file = str(Path(file).expanduser().resolve())
        else:
            import getpass
            secret = (secret_input or getpass.getpass)(t("新 API 密钥（不回显；留空取消）: ", "New API key (hidden; blank cancels): "))
            if not secret or "\n" in secret or "\r" in secret:
                print(t("已取消；配置未改变。", "Cancelled; nothing changed."), file=output)
                return None
        updates.update({prefix + "api_key_env": env, prefix + "api_key_file": file})
    if source == "anonymous":
        updates.update(openalex_api_key_env=env, openalex_api_key_file=file)
    if backend == "openalex":
        current_email = owner.openalex_contact_email
        entered = wizard.input(t(f"联系邮箱 [{current_email or '未配置'}]（回车保留；/clear 清除）: ",
                                 f"Contact email [{current_email or 'none'}] (Enter keeps; /clear removes): ")).strip()
        if entered:
            updates["openalex_contact_email"] = None if entered == "/clear" else entered
    else:
        updates["enabled"] = True
        if backend == "parallel":
            mode = wizard._choose_one(t("Parallel 档位", "Parallel mode"), [
                ("keep", t(f"保留当前 {owner.mode}", f"Keep current {owner.mode}")),
                ("fast", "fast"), ("turbo", "turbo")])
            if mode != "keep":
                updates["mode"] = mode
    if not updates and not secret:
        print(t("设置未改变。", "Nothing changed."), file=output)
        return None
    if source in {"file", "input"} and env and os.environ.get(env):
        print(t("注意：当前进程已设置同名环境变量，它优先于密钥文件；保存文件不会覆盖环境变量。",
                "Note: an existing environment value takes precedence over the key file. Saving a file does not overwrite it."), file=output)
    confirmed = wizard.input(t(f"保存对 {backend.title()} 的修改？其他供应商和密钥不变。[y/N]: ",
                               f"Save changes to {backend.title()} only? Other providers and keys stay unchanged. [y/N]: ")).strip().lower()
    if confirmed not in {"y", "yes"}:
        print(t("已取消；配置未改变。", "Cancelled; nothing changed."), file=output)
        return None
    # Keep effective custom endpoint/options from the active project when
    # making this provider a user override, not the defaults of another config.
    if backend == "openalex":
        saved = {name: getattr(owner, name) for name in _OPENALEX_FIELDS}
    else:
        saved = owner.model_dump()
    saved.update(updates)
    if secret:
        saved[prefix + "api_key_file"] = str(store_secret(
            f"{backend}-{secrets.token_hex(6)}", env, secret))
    path = save_search_backend(backend, saved, seed_path=seed_path)
    print(t(f"仅 {backend.title()} 设置已保存；其他供应商未修改。",
            f"Only {backend.title()} settings saved; other providers unchanged."), file=output)
    return path
