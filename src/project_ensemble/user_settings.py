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


def settings_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("APPDATA")
        return Path(root).expanduser() / "ensemble" if root else Path.home() / "AppData/Roaming/ensemble"
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".config") / "ensemble"


def user_config_path() -> Path:
    return settings_dir() / "ensemble.toml"


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
        if isinstance(value, dict) and key in {"tavily", "email"}:
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
    return config_path


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
    _write_user_config(path, data)
    load_config(path)
    return path


def appearance() -> tuple[str, int]:
    try:
        data = tomllib.loads((settings_dir() / "appearance.toml").read_text(encoding="utf-8"))["appearance"]
        color, width = data.get("color", "auto"), data.get("width", 100)
        if color in {"auto", "always", "never"} and width in {80, 100, 120}:
            return color, width
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    return "auto", 100


def configure_interactively(wizard, *, seed_path: str | Path | None = None, secret_input: Callable[[str], str] | None = None) -> Path | None:
    """Guide one settings action. None means back to the home menu."""
    output: TextIO = wizard.output
    def t(chinese: str, english: str) -> str:
        return ui_label(chinese, english, getattr(wizard, "language", "zh"))
    action = wizard._choose_one("设置", [
        ("language", "语言：界面语言及未来会议正文语言默认值"),
        ("appearance", "界面外观"),
        ("provider", "模型供应商"),
        ("search", "联网搜索后端：OpenAlex / Tavily"),
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
        print(t("OpenAlex 是学术检索主后端；Tavily 可选作网页检索补充。", "OpenAlex is the primary academic search backend; Tavily can supplement it with web search."), file=output)
        email = wizard.input("OpenAlex 联系邮箱（可留空）: ").strip() or None
        openalex_source = wizard._choose_one("OpenAlex 凭据", [
            ("anonymous", "匿名访问（可能受到较低的服务限额约束）"),
            ("env", "引用已有环境变量"),
            ("file", "引用已有外部密钥文件"),
            ("input", "输入并保存到本机用户目录"),
        ])
        openalex_env = "" if openalex_source == "anonymous" else "OPENALEX_API_KEY"
        openalex_file = None
        if openalex_source != "anonymous":
            openalex_env = wizard.input("OpenAlex 环境变量名 [OPENALEX_API_KEY]: ").strip() or openalex_env
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", openalex_env):
                raise ValueError("无效的环境变量名")
            if openalex_source == "file":
                openalex_file = wizard.input("OpenAlex 外部密钥文件路径: ").strip()
                if not Path(openalex_file).expanduser().is_file():
                    raise ValueError("OpenAlex 密钥文件不存在")
                openalex_file = str(Path(openalex_file).expanduser().resolve())
            elif openalex_source == "input":
                secret = (secret_input or getpass.getpass)("OpenAlex API 密钥（不回显）: ")
                openalex_file = str(store_secret("openalex", openalex_env, secret))
        tavily_enabled = wizard._choose_one("网页检索补充", [
            ("no", "不启用 Tavily；仅用 OpenAlex"),
            ("yes", "启用 Tavily；同时使用 OpenAlex 与 Tavily"),
        ]) == "yes"
        tavily_env = "TAVILY_API_KEY"
        tavily_file = None
        if tavily_enabled:
            tavily_env = wizard.input("Tavily 环境变量名 [TAVILY_API_KEY]: ").strip() or tavily_env
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", tavily_env):
                raise ValueError("无效的环境变量名")
            source = wizard._choose_one("Tavily 凭据", [
                ("env", "引用已有环境变量"),
                ("file", "引用已有外部密钥文件"),
                ("input", "输入并保存到本机用户目录"),
            ])
            if source == "file":
                tavily_file = wizard.input("Tavily 外部密钥文件路径: ").strip()
                if not Path(tavily_file).expanduser().is_file():
                    raise ValueError("Tavily 密钥文件不存在")
                tavily_file = str(Path(tavily_file).expanduser().resolve())
            elif source == "input":
                secret = (secret_input or getpass.getpass)("Tavily API 密钥（不回显）: ")
                tavily_file = str(store_secret("tavily", tavily_env, secret))
        path = save_search_backends(
            openalex_email=email, openalex_key_env=openalex_env,
            openalex_key_file=openalex_file, tavily_enabled=tavily_enabled,
            tavily_key_env=tavily_env, tavily_key_file=tavily_file,
            seed_path=seed_path,
        )
        print(t(f"联网检索设置已保存：{path}；只影响新会议。", f"Search settings saved to {path}; affects new meetings only."), file=output)
        return path
    existing_path = user_config_path() if user_config_path().is_file() else seed_path
    if existing_path:
        existing_providers = load_config(existing_path).providers
        if existing_providers:
            print(t("已配置供应商（本向导新增代号，不覆盖已有条目）：", "Configured providers (this wizard adds a new ID; it does not overwrite existing entries):"), file=output)
            for existing_id, existing in existing_providers.items():
                state = t("已启用", "enabled") if existing.enabled else t("未启用", "disabled")
                print(f"  {existing_id} · {existing.display_name or existing_id} · {state}", file=output)
    kind = wizard._choose_one("调用方式", [
        ("openai_compatible", "OpenAI 兼容 API（DeepSeek、GLM、Kimi 等）"),
        ("gemini", "Google Gemini 原生 API"),
        ("codex_subscription", "Codex CLI；使用本机 ChatGPT 登录，不输入 API 密钥"),
        ("claude_code", "Claude Code CLI；可选本机登录或 API 密钥"),
        ("back", "返回设置"),
    ])
    if kind == "back":
        return None
    name = wizard.input("供应商显示名称（可用中文）: ").strip()
    if not name:
        raise ValueError("供应商显示名称不能为空")
    provider_id = wizard.input("供应商代号（小写英文，会议记录将使用它，如 my_glm）: ").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", provider_id):
        raise ValueError("供应商代号须以小写英文字母开头，只能包含小写字母、数字、-、_")
    if existing_path and provider_id in existing_providers:
        raise ValueError(f"供应商代号 {provider_id} 已存在；请选择另一代号")
    fields: dict = {"kind": kind, "display_name": name, "timeout_seconds": 1200.0}
    pending_secret: tuple[str, str, str] | None = None
    if kind in {"openai_compatible", "gemini"}:
        default_url = "https://generativelanguage.googleapis.com/v1beta" if kind == "gemini" else ""
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
        default_env = re.sub(r"[^A-Z0-9_]", "_", provider_id.upper()) + "_API_KEY"
        env_name = wizard.input(t(f"环境变量名称 [{default_env}]: ", f"Environment variable name [{default_env}]: ")).strip() or default_env
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
    return path
