from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from project_ensemble import __version__
from project_ensemble.chair_qa import ChairQuestionService, latest_draft
from project_ensemble.publication_corrigendum import ChairCorrigendumService
from project_ensemble.orchestration.supplementary_rendering import (
    complete_render_source, render_additional_formats,
)
from project_ensemble.config import load_config
from project_ensemble.user_settings import (
    configure_interactively, ensure_user_config, interface_language,
    removed_provider_ids, user_config_path,
)
from project_ensemble.interface_language import ui_label, ui_text
from project_ensemble.domain import (
    DeliverableType,
    InheritanceMode,
    MeetingPhase,
    MeetingType,
    ReasoningEffort,
)
from project_ensemble.errors import (
    EnsembleError,
    FeatureNotImplementedError,
    ModelReplacementRequested,
    OpenAlexQueryRejected,
    PermanentProviderError,
    PolicyNotConfiguredError,
    ProviderContentRejectedError,
    ResearchQualityControlError,
)
from project_ensemble.notifications.email import build_email_notifier
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.consultations import (
    HumanConsultationService,
    ScienceConsultationAuthorityService,
    consultation_option_label,
    normalize_consultation_decision,
)
from project_ensemble.orchestration.budgets import (
    add_replacement_input_context_budgets,
    add_replacement_output_budgets,
    budget_map,
    input_context_budget_map,
    load_or_freeze_input_context_budgets,
    load_or_freeze_meeting_budgets,
)
from project_ensemble.orchestration.escalation import HumanEscalationService
from project_ensemble.orchestration.general_principle import GeneralPrincipleRunner
from project_ensemble.orchestration.literature_report import LiteratureReportPlanningRunner
from project_ensemble.orchestration.literature_report_execution import (
    LiteratureReportExecutionRunner,
)
from project_ensemble.orchestration.scholarly_rendering import (
    ScholarlyRenderingRunner,
    reissue_structured_scholarly_publication,
)
from project_ensemble.paths import bundled_historical_governance_docs
from project_ensemble.providers.registry import build_adapters
from project_ensemble.research.desk import ResearchDesk
from project_ensemble.research.exploration import ResearchExplorationService
from project_ensemble.research.cache import ResearchPacketCache
from project_ensemble.research.models import FreshnessClass, ResearchRequest, ResearchStage
from project_ensemble.research.models import CacheInvalidationAuthority, CacheInvalidationReason
from project_ensemble.research.openalex import OpenAlexRetriever
from project_ensemble.research.retrievers import CompositeRetriever, PolicyResearchRetriever, TavilyRetriever
from project_ensemble.research.rounds import ResearchRoundRunner
from project_ensemble.research.documents import HttpDocumentFetcher
from project_ensemble.runtime.progress import (
    ConsoleProgressReporter,
    NullProgressReporter,
    shutdown_active_control_listeners,
)
from project_ensemble.runtime.consultation_view import (
    local_science_review_context,
    render_outline_consultation,
    render_outline_detail,
    render_local_science_consultation,
    render_science_consultation,
)
from project_ensemble.runtime.terminal_style import rule_width
from project_ensemble.runtime.model_replacements import (
    ModelReplacementService,
    current_runtime_for,
    meeting_participant_ids,
)
from project_ensemble.startup import (
    StartupSelection,
    StartupWizardCancelled,
    TerminalWizard,
    assert_models_were_discovered,
    config_with_providers_enabled,
    discover_models,
    enable_utf8_terminal_erase,
    natural_meeting_title,
    start_meeting,
    terminal_input,
)
from project_ensemble.storage.events import HashChainEventLog
from project_ensemble.storage.human_outputs import ensure_visible_link
from project_ensemble.storage.legacy_migration import migrate_v06_meeting
from project_ensemble.storage.meeting import (
    MeetingRepository, directory_digest, provisional_rendering_text,
)
from project_ensemble.storage.meeting_index import (
    discover_local_meetings,
    indexed_meetings,
    inspect_meeting,
    meeting_index_path,
    meeting_is_complete,
    register_meeting,
    resolve_indexed_meeting,
)
from project_ensemble.storage.meeting_management import (
    compact_meeting,
    launch_background_deletion,
    plan_meeting_archive,
)


def _ui(chinese: str, english: str) -> str:
    """Translate fixed terminal copy, never source or model-authored text."""
    return ui_label(chinese, english, interface_language() or "zh")


def _option_ui(option: str) -> str:
    return ui_text(consultation_option_label(option), interface_language() or "zh")


def _config_path(value: str | None, repo: MeetingRepository | None = None) -> str:
    if value:
        return value
    from_environment = os.environ.get("ENSEMBLE_CONFIG")
    if from_environment:
        return from_environment
    if repo is not None:
        return str(repo.config_path())
    user_config = user_config_path()
    if user_config.is_file():
        return str(user_config)
    # Preserve source-checkout behavior until a user-level configuration exists.
    bundled = Path(__file__).resolve().parents[2] / "ensemble.toml"
    if bundled.is_file():
        return str(bundled)
    raise ValueError("configuration path required: use --config or set ENSEMBLE_CONFIG")


def _load_config(path: str | Path):
    """Load the selected runtime config plus user-managed provider additions.

    User-managed provider entries contribute custom APIs and override matching
    provider IDs from the selected config. This makes Settings edits and newly
    added providers available even when ENSEMBLE_CONFIG points at a project
    config, without changing that file's meeting/research policies.
    """
    config = load_config(path)
    if not hasattr(config, "providers"):
        return config
    user_path = user_config_path()
    try:
        same_file = user_path.resolve() == Path(path).expanduser().resolve()
    except OSError:
        same_file = False
    if not same_file and user_path.is_file():
        user_config = load_config(user_path)
        providers = dict(config.providers)
        providers.update(user_config.providers)
    else:
        providers = dict(config.providers)
    removed = removed_provider_ids()
    if removed:
        providers = {provider_id: provider for provider_id, provider in providers.items()
                     if provider_id not in removed}
    config = config.model_copy(update={"providers": providers})
    return config


def _meeting_runtime_provider_ids(repo: MeetingRepository) -> set[str]:
    """Providers needed by this meeting, including runtime replacements."""
    provider_ids = set()
    for participant in meeting_participant_ids(repo):
        try:
            provider_id, _ = current_runtime_for(repo, participant)
        except (ValueError, FileNotFoundError):
            continue
        provider_ids.add(provider_id)
    return provider_ids


def _build_meeting_adapters(cfg, repo: MeetingRepository, *, require_keys: bool = True):
    """Build only providers actually used by this meeting, regardless of legacy enabled flags."""
    provider_ids = _meeting_runtime_provider_ids(repo)
    meeting_cfg = config_with_providers_enabled(cfg, sorted(provider_ids))
    meeting_cfg = meeting_cfg.model_copy(update={
        "providers": {provider_id: meeting_cfg.providers[provider_id] for provider_id in provider_ids}
    })
    return build_adapters(meeting_cfg, require_keys=require_keys)


def _load_adapter_for_provider(cfg, provider_id: str):
    """Build one provider adapter in memory without enabling it globally."""
    provider_cfg = config_with_providers_enabled(cfg, [provider_id])
    selected = provider_cfg.model_copy(update={"providers": {provider_id: provider_cfg.providers[provider_id]}})
    return build_adapters(selected, require_keys=True)[provider_id]


_RESUMABLE_COMMANDS = {"run-general", "run-report", "run-research", "run-render", "run-audit"}


def _resume_command(args) -> str | None:
    """Build the canonical command that resumes an already-created meeting.

    Legacy ``run-general`` / ``run-report`` / ``run-research`` entry points
    remain accepted, but newly generated helpers use ``open`` so the meeting's
    frozen type, deliverable and configuration determine the correct runner.
    """

    command_name = getattr(args, "_resume_cmd", None) or getattr(args, "cmd", None)
    if command_name not in _RESUMABLE_COMMANDS:
        return None
    meeting = getattr(args, "_resume_meeting", None) or getattr(args, "meeting", None)
    if not meeting:
        return None
    command = ["ensemble", "resume", str(Path(meeting).expanduser().resolve())]
    # Preserve explicit overrides that affect how a resumed run is interpreted.
    options = [("--config", "config"), ("--governance-docs", "governance_docs")]
    for option, attribute in options:
        value = getattr(args, attribute, None)
        if value:
            command.extend((option, str(Path(value).expanduser().resolve())))
    max_output_tokens = getattr(args, "max_output_tokens", None)
    if command_name in {"run-general", "run-report", "run-render"} and max_output_tokens is not None:
        command.extend(("--max-output-tokens", str(max_output_tokens)))
    if command_name in {"run-general", "run-report", "run-render"} and getattr(args, "no_progress", False):
        command.append("--no-progress")
    return " ".join(shlex.quote(part) for part in command)


def _failure_meeting_root(args) -> Path | None:
    """Find an already-created meeting without consulting mutable indexes."""
    candidate = getattr(args, "_resume_meeting", None) or getattr(args, "meeting", None)
    if candidate is None and getattr(args, "cmd", None) in {"open", "resume"}:
        candidate = getattr(args, "selector", None)
    if not candidate:
        return None
    try:
        root = Path(candidate).expanduser().resolve()
        return root if (root / "public/meeting_manifest.json").is_file() else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _record_unhandled_failure(root: Path, exc: Exception, args) -> Path | None:
    """Keep diagnostics private and append-only; never mask the original failure."""
    try:
        folder = root / "audit_private/runtime_recovery"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                         + "-" + uuid4().hex[:12] + ".json")
        payload = {
            "error_type": type(exc).__name__, "message": str(exc),
            "command": getattr(args, "cmd", None),
            "resume_command": _resume_command(args),
            "traceback": "".join(traceback.format_exception(exc))[-30000:],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        with path.open("x", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return None


def _recover_unhandled_failure(args, exc: Exception) -> int:
    """Never leave a saved meeting at an unexplained, unfixable CLI exit.

    This does not pretend that arbitrary bad data is safe to rewrite.  The
    Human may retry the same frozen checkpoint, replace a runtime, or pause
    with an exact diagnostic and resume command.  No frozen record is removed.
    """
    root = _failure_meeting_root(args)
    if root is None:
        print(_ui(f"操作未完成：{type(exc).__name__}: {exc}",
                  f"Operation did not complete: {type(exc).__name__}: {exc}"), file=sys.stderr)
        print(_ui("尚未定位到已创建的会议；请检查输入或配置后重试。",
                  "No created meeting was identified; correct the input or configuration and retry."), file=sys.stderr)
        return 2
    diagnostic = _record_unhandled_failure(root, exc, args)
    command = _resume_command(args) or "ensemble resume " + shlex.quote(str(root))
    while True:
        print(_ui("\n┌─ 会议遇到未处理故障 · 进度已保留 ─────────────────────────",
                  "\n┌─ Meeting needs recovery · saved progress preserved ─────────────"), file=sys.stderr)
        print(_ui(f"│ 故障：{type(exc).__name__}: {str(exc)[:1000]}",
                  f"│ Failure: {type(exc).__name__}: {str(exc)[:1000]}"), file=sys.stderr)
        if diagnostic is not None:
            print(_ui(f"│ 私有诊断记录：{diagnostic}",
                      f"│ Private diagnostic: {diagnostic}"), file=sys.stderr)
        print(_ui(f"│ 恢复命令：{command}", f"│ Resume command: {command}"), file=sys.stderr)
        print(_ui("│ 已冻结内容不覆盖；本次未完成步骤可以重新提交。",
                  "│ Frozen work is unchanged; the unfinished step can be submitted again."), file=sys.stderr)
        print(_ui("│ 1. 从冻结进度重试；若错误重现，可再选择其他处理方式",
                  "│ 1. Retry from the frozen checkpoint"), file=sys.stderr)
        print(_ui("│ 2. 更换模型或调整运行参数，再从冻结进度重试",
                  "│ 2. Change a model or runtime setting, then retry"), file=sys.stderr)
        print(_ui("│ 3. 保持暂停；保留诊断和恢复命令，稍后人工排障",
                  "│ 3. Stay paused with diagnostics and a resume command"), file=sys.stderr)
        print("└──────────────────────────────────────────────────────────", file=sys.stderr)
        if not sys.stdin.isatty():
            print(_ui("当前不是交互终端；会议保持暂停。请在终端运行上述恢复命令选择处理。",
                      "No interactive terminal is attached; run the resume command above to choose recovery."), file=sys.stderr)
            return 2
        try:
            choice = terminal_input(_ui("选择 1–3（回车保持暂停）: ",
                                        "Choose 1–3 (Enter to stay paused): ")).strip()
        except (EOFError, KeyboardInterrupt):
            return 130
        except Exception as input_error:
            print(_ui(f"无法读取恢复选择：{input_error}；会议保持暂停。",
                      f"Could not read a recovery choice: {input_error}; meeting stays paused."), file=sys.stderr)
            return 2
        if choice in {"", "3", "b", "q"}:
            return 2
        if choice == "2":
            try:
                repo = MeetingRepository(root)
                cfg = _load_config(_config_path(getattr(args, "config", None), repo))
                replacement = _interactive_model_replacement(repo=repo, cfg=cfg)
            except (KeyboardInterrupt, EOFError):
                return 130
            except Exception as replacement_error:
                print(_ui(f"设置未改变：{replacement_error}；可以重试或保持暂停。",
                          f"Settings unchanged: {replacement_error}; retry or stay paused."), file=sys.stderr)
                continue
            if replacement is None:
                continue
            choice = "1"
        if choice != "1":
            print(_ui("请输入菜单中的编号。", "Enter a listed option."), file=sys.stderr)
            continue
        try:
            # `home` and `start` have already created this workspace.  Never
            # rerun initialization merely because the subsequent meeting run
            # failed; reopen the saved meeting instead.
            result = (_open_meeting(args, root)
                      if getattr(args, "cmd", None) in {"home", "start"}
                      else args.func(args))
            return result if isinstance(result, int) else 0
        except (KeyboardInterrupt, EOFError):
            print(_ui("已停止重试；已落盘进度保持不变。", "Retry stopped; saved progress is unchanged."), file=sys.stderr)
            return 130
        except Exception as retry_error:
            exc = retry_error
            diagnostic = _record_unhandled_failure(root, retry_error, args)


def _write_resume_script(args) -> tuple[str, tuple[Path, ...]] | None:
    """Write a local executable resume helper for an interrupted meeting.

    The helper is deliberately written in the process' current directory, as
    opposed to the meeting workspace, so it is visible exactly where the user
    launched ``ensemble``.  Failure to write the convenience script must never
    hide the durable meeting state or turn a safe Ctrl+C into a new error.
    """

    command = _resume_command(args)
    if command is None:
        return None
    meeting = getattr(args, "_resume_meeting", None) or getattr(args, "meeting", None)
    meeting_path = Path(meeting).expanduser().resolve()
    meeting_id = meeting_path.name
    meeting_title = meeting_id
    manifest = meeting_path / "public" / "meeting_manifest.json"
    try:
        if manifest.exists():
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            meeting_id = str(payload.get("meeting_id") or meeting_id)
            meeting_title = " ".join(str(payload.get("title") or meeting_id).split())
    except (OSError, json.JSONDecodeError):
        # The path name is still a useful fallback during an interrupted start.
        pass
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", meeting_id).strip("._") or "meeting"
    script_paths = (
        Path.cwd() / f"{safe_id}_resume.sh",
        Path.cwd() / "resume.sh",
    )
    content = (
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"# 会议 ID：{meeting_id}\n"
        f"# 会议标题：{meeting_title}\n"
        f"printf '%s\\n' {shlex.quote(f'恢复会议：{meeting_id} · {meeting_title}')}\n"
        f"exec {command}\n"
    )
    for script_path in script_paths:
        temporary = script_path.with_name(f".{script_path.name}.{os.getpid()}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o700)
        os.replace(temporary, script_path)
    return command, script_paths


def _configured_output_token_budgets(*, repo, cfg, adapters, progress=None):
    """Load model budgets only when the Human explicitly configured a limit."""

    fallback_tokens = cfg.governance.provider_output_token_limit
    if fallback_tokens is None:
        if progress is not None:
            progress.info("单次模型输出上限：ENSEMBLE 未设置；由模型供应商决定")
        return {}
    snapshot = load_or_freeze_meeting_budgets(
        repo=repo,
        adapters=adapters,
        context_fraction=cfg.governance.provider_output_context_fraction,
        fallback_tokens=fallback_tokens,
    )
    if progress is not None:
        for budget in snapshot.models:
            progress.info(
                f"输出预算 {budget.provider_id}:{budget.model_id} = "
                f"{budget.requested_output_tokens} tokens（{budget.basis}）"
            )
    return add_replacement_output_budgets(
        repo=repo,
        adapters=adapters,
        budgets=budget_map(snapshot),
        context_fraction=cfg.governance.provider_output_context_fraction,
        fallback_tokens=fallback_tokens,
    )


def _configured_input_context_budgets(*, repo, cfg, adapters, progress=None):
    configured_model_limits = {
        (provider_id, model_id): limit
        for provider_id, provider in cfg.providers.items()
        for model_id, limit in provider.model_input_token_limits.items()
    }
    snapshot = load_or_freeze_input_context_budgets(
        repo=repo,
        adapters=adapters,
        safety_fraction=cfg.governance.provider_input_context_fraction,
        fallback_tokens=cfg.governance.provider_input_token_limit_fallback,
        configured_model_limits=configured_model_limits,
    )
    if progress is not None:
        for budget in snapshot.models:
            progress.info(
                f"输入安全预算 {budget.provider_id}:{budget.model_id} = "
                f"{budget.maximum_input_tokens} tokens（{budget.basis}）"
            )
    return add_replacement_input_context_budgets(
        repo=repo,
        adapters=adapters,
        budgets=input_context_budget_map(snapshot),
        safety_fraction=cfg.governance.provider_input_context_fraction,
        fallback_tokens=cfg.governance.provider_input_token_limit_fallback,
        configured_model_limits=configured_model_limits,
    )


def cmd_doctor(args) -> int:
    cfg = _load_config(_config_path(args.config))
    problems = []
    for pid, p in cfg.providers.items():
        if p.enabled and p.kind != "codex_subscription" and (
            p.kind != "claude_code" or p.api_key_env
        ) and not p.api_key():
            problems.append(
                f"{pid}: missing {p.api_key_env} and no readable configured api_key_file"
            )
    notification_problems = build_email_notifier(cfg.notifications.email).configuration_problems()
    print(f"Configuration: {cfg.source_path}")
    print(f"Meeting output directory for ensemble start: {Path.cwd()}")
    if cfg.governance.provider_output_token_limit is None:
        print("Provider output budget: no ENSEMBLE per-call limit; provider-managed")
    else:
        print(
            "Provider output budget target: "
            f"{cfg.governance.provider_output_context_fraction:.0%} of advertised input context; "
            f"fallback {cfg.governance.provider_output_token_limit} tokens"
        )
    print(
        "Provider input safety budget: "
        f"{cfg.governance.provider_input_context_fraction:.0%} of advertised input context; "
        f"fallback {cfg.governance.provider_input_token_limit_fallback} estimated tokens"
    )
    print(
        "Research Desk defaults: "
        f"primary retriever={cfg.research.retriever}; "
        f"OpenAlex authentication={'API key' if cfg.research.openalex_api_key() else 'anonymous'}; "
        f"Tavily supplement={'enabled' if cfg.research.tavily.enabled else 'disabled'}; freshness="
        f"volatile:{cfg.research.volatile_freshness_days} days/"
        f"versioned:{cfg.research.versioned_freshness_days} days/"
        f"stable:{cfg.research.stable_freshness_days} days; "
        f"results/query={cfg.research.max_results_per_query}; "
        f"source-document limit={cfg.research.max_source_document_bytes} bytes/file"
    )
    if cfg.research.tavily.enabled and not cfg.research.tavily.api_key():
        problems.append(
            "research.tavily: missing "
            f"{cfg.research.tavily.api_key_env} and no readable configured api_key_file"
        )
    if not cfg.research.openalex_api_key():
        problems.append(
            "research.openalex: anonymous access; set "
            f"{cfg.research.openalex_api_key_env} or configure openalex_api_key_file"
        )
    for provider_id, provider in cfg.providers.items():
        levels = ", ".join(provider.reasoning_effort_map) or "default only"
        print(f"Reasoning control {provider_id}: {levels}")
        default_concurrency = provider.max_concurrent_requests or 1
        source = "configured" if provider.max_concurrent_requests else "safe fallback"
        overrides = ", ".join(
            f"{model}={limit}"
            for model, limit in sorted(
                provider.model_max_concurrent_requests.items()
            )
        )
        print(
            f"Concurrency control {provider_id}: default={default_concurrency} "
            f"({source})"
            + (f"; model overrides: {overrides}" if overrides else "")
        )
    if problems:
        print("Configuration parsed. Problems found:")
        for x in problems:
            print(f"  - {x}")
        return 2
    if notification_problems:
        print("Optional notification warnings:")
        for problem in notification_problems:
            print(f"  - {problem}")
    print("Provider credentials are configured; notification transport is optional.")
    return 0


def cmd_discover(args) -> int:
    cfg = _load_config(_config_path(args.config))
    provider_ids = list(cfg.providers)
    out = [model.model_dump() for model in discover_models(cfg, provider_ids)]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_verify_events(args) -> int:
    log = HashChainEventLog(args.path)
    log.verify()
    print("event chain OK")
    return 0


def cmd_migrate_v06(args) -> int:
    report = migrate_v06_meeting(
        args.meeting,
        args.output,
        config_path=_config_path(args.config),
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print("v0.6 会议迁移预检通过；源会议未修改，目标目录未创建。")
    else:
        print(f"v0.6 会议副本已升级至 v{__version__}：{report['destination_path']}")
        print(f"恢复命令：ensemble open {shlex.quote(report['destination_path'])}")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0



def _provider_model(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("expected provider:model")
    provider, model = value.split(":", 1)
    if not provider or not model:
        raise argparse.ArgumentTypeError("expected provider:model")
    return provider, model


def _build_research_retriever(cfg, repo: MeetingRepository | None = None):
    max_results_per_query = cfg.research.max_results_per_query
    quota_policy = "wait"
    if repo is not None:
        manifest = json.loads(repo.docs.read_text("identity_private/meeting_manifest.json"))
        frozen_limit = manifest.get("openalex_max_results_per_query")
        if frozen_limit is not None:
            max_results_per_query = frozen_limit
        from project_ensemble.runtime.run_controls import effective_openalex_quota_policy
        # Meetings initialized before this control existed receive the current
        # daily-quota diagnosis and Human choice. Frozen evidence is never rewritten.
        quota_policy = effective_openalex_quota_policy(
            repo, manifest.get("openalex_quota_policy") or "wait",
        )
    primary_retriever = OpenAlexRetriever(
        base_url=cfg.research.openalex_base_url,
        contact_email=cfg.research.openalex_contact_email,
        api_key=cfg.research.openalex_api_key(),
        timeout_seconds=cfg.research.request_timeout_seconds,
        max_results_per_query=max_results_per_query,
        max_concurrent_requests=cfg.research.openalex_max_concurrent_requests,
        min_request_interval_seconds=cfg.research.openalex_min_request_interval_seconds,
    )
    retrievers = [primary_retriever]
    if cfg.research.tavily.enabled:
        tavily_key = cfg.research.tavily.api_key()
        if not tavily_key:
            raise PermanentProviderError(
                "Tavily is enabled but its API key is unavailable; set "
                f"{cfg.research.tavily.api_key_env} or configure research.tavily.api_key_file"
            )
        retrievers.append(
            TavilyRetriever(
                api_key=tavily_key,
                base_url=cfg.research.tavily.base_url,
                timeout_seconds=cfg.research.request_timeout_seconds,
                search_depth=cfg.research.tavily.search_depth,
                max_results_per_query=cfg.research.tavily.max_results_per_query,
                chunks_per_source=cfg.research.tavily.chunks_per_source,
                max_concurrent_requests=(
                    cfg.research.tavily.max_concurrent_requests
                ),
            )
        )
    if repo is not None and (repo.root / "public/human_references/manifest.json").is_file():
        from project_ensemble.research.human_references import HumanReferenceRetriever

        retrievers.append(HumanReferenceRetriever(repo))
    if len(retrievers) == 1:
        return primary_retriever
    if quota_policy == "legacy_parallel" or not isinstance(retrievers[1], TavilyRetriever):
        return CompositeRetriever(
            retrievers,
            preserve_openalex_quota=(quota_policy == "wait"),
        )
    policy_retriever = PolicyResearchRetriever(primary_retriever, retrievers[1], quota_policy=quota_policy)
    return (policy_retriever if len(retrievers) == 2
            else CompositeRetriever([policy_retriever, *retrievers[2:]]))


def _research_freshness_windows(cfg):
    return {
        FreshnessClass.VOLATILE: cfg.research.volatile_freshness_days,
        FreshnessClass.VERSIONED: cfg.research.versioned_freshness_days,
        FreshnessClass.STABLE: cfg.research.stable_freshness_days,
    }


def _research_document_fetcher(cfg):
    return HttpDocumentFetcher(
        timeout_seconds=cfg.research.request_timeout_seconds,
        max_bytes=cfg.research.max_source_document_bytes,
        max_concurrent_requests=cfg.research.max_concurrent_document_downloads,
    )


def _meeting_concurrency_limits(repo: MeetingRepository, cfg) -> dict[tuple[str, str], int]:
    from project_ensemble.runtime.run_controls import effective_model_concurrency

    manifest_path = repo.root / "identity_private/meeting_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        frozen = manifest.get("model_concurrency_limits") or {}
        if frozen:
            return {
                tuple(key.split(":", 1)): int(limit)
                for key, limit in effective_model_concurrency(repo, frozen).items()
            }
    limits: dict[tuple[str, str], int] = {}
    for provider_id, provider in cfg.providers.items():
        if provider.max_concurrent_requests is not None:
            limits[(provider_id, "*")] = provider.max_concurrent_requests
        limits.update(
            {
                (provider_id, model_id): limit
                for model_id, limit in provider.model_max_concurrent_requests.items()
            }
        )
    return {
        tuple(key.split(":", 1)): int(limit)
        for key, limit in effective_model_concurrency(
            repo, {f"{provider}:{model}": limit for (provider, model), limit in limits.items()}
        ).items()
    }


def _interactive_start_arguments(args, *, cfg_path: str | None = None):
    """Populate the ordinary start fields for the bare-command home screen."""

    # ``open`` already has ``args.config = None`` from argparse.  The source
    # meeting's resolved configuration must replace that placeholder when a
    # continuation creates a new meeting.
    if cfg_path is not None:
        args.config = cfg_path

    defaults = {
        "config": getattr(args, "config", None),
        "governance_docs": getattr(args, "governance_docs", None),
        "non_interactive": False,
        "meeting_type": None,
        "deliverable_type": None,
        "parent_meeting": None,
        "human_reference": None,
        "provider": None,
        "model": None,
        "chair": None,
        "representative_reasoning_effort": None,
        "chair_reasoning_effort": None,
        "enable_research": False,
        "research_model": None,
        "research_reasoning_effort": None,
        "research_max_concurrent_claim_groups": None,
        "openalex_quota_policy": None,
        "task": None,
        "title": None,
        "email": None,
        "inherit": None,
        "rendering_science_model": None,
        "rendering_citation_model": None,
        "rendering_language": None,
        "rendering_academic_skeleton": False,
        "rendering_full_abstract": False,
        "rendering_section_abstracts": False,
        "rendering_segmentation": None,
        "rendering_liveliness": None,
        "rendering_output_format": None,
        "rendering_science_order": None,
        "rendering_science_consultation_authority": None,
        "rendering_target_body_characters": None,
        "literature_language": None,
        "deliberation_language": None,
        "literature_full_abstract": False,
        "literature_section_abstracts": False,
        "literature_segmentation": None,
        "literature_liveliness": None,
        "literature_signposting": None,
        "literature_target_body_characters": None,
        "report_palette": None,
        "maximum_parallelism": None,
        "model_concurrency_limit": None,
        "literature_writing_policy": None,
        "writer_model": None,
        "writer_reasoning_effort": None,
        "fast_planner_model": None,
        "technician_model": None,
        "technician_reasoning_effort": None,
    }
    for name, value in defaults.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    return args


def _resume_kind(repo: MeetingRepository) -> str:
    manifest = json.loads(
        (repo.root / "public/meeting_manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("deliverable_type") == DeliverableType.LITERATURE_REVIEW.value:
        return "run-report"
    if manifest.get("deliverable_type") == DeliverableType.SCHOLARLY_RENDERING.value:
        return "run-render"
    if manifest.get("meeting_type") == MeetingType.RESEARCH.value:
        return "run-research"
    if manifest.get("meeting_type") == MeetingType.AUDIT.value:
        return "run-audit"
    return "run-general"


def _audit_not_implemented(repo: MeetingRepository) -> None:
    raise FeatureNotImplementedError(
        "AUDIT_STATE_MACHINE_NOT_IMPLEMENTED_IN_V0_7_0: "
        f"meeting {repo.meeting_id} is preserved and unchanged. The public GitHub "
        "release exposes this fail-fast interface intentionally; it must not route an "
        "audit meeting through the deliberation runner."
    )


def _assert_meeting_runtime_compatible(
    repo: MeetingRepository, *, governance_docs: str | Path
) -> None:
    """Refuse in-place semantic migration of a frozen meeting."""

    if (repo.root / "public/archive_manifest.json").is_file():
        raise PolicyNotConfiguredError(
            "ARCHIVED_MEETING_NOT_RESUMABLE: 原会议的流程记录已按 Human 授权归档删除；"
            "可以以保留的证据和完整文稿召开后续新会，但不能恢复原流程。"
        )

    manifest = json.loads(
        repo.docs.read_text("identity_private/meeting_manifest.json")
    )
    frozen_software = manifest.get("software_version")
    # 0.7.3 continues the 0.7.1 meeting runtime/governance format. Keep this
    # allowlist explicit; the frozen governance digest below remains the
    # semantic compatibility check.
    if not _meeting_software_version_supported(frozen_software):
        raise PolicyNotConfiguredError(
            "MEETING_SOFTWARE_VERSION_MISMATCH: "
            f"meeting requires {frozen_software or 'an unversioned legacy runtime'}, "
            f"but this command is {__version__}. Resume it with the compatible installation "
            "or copy a v0.6 meeting with 'ensemble migrate-v06 --help'; "
            f"v{__version__} will not migrate it in place."
        )
    current_digest = directory_digest(Path(governance_docs))
    if current_digest != manifest.get("governance_digest"):
        raise PolicyNotConfiguredError(
            "MEETING_GOVERNANCE_DIGEST_MISMATCH: the configured governance package does "
            "not match the immutable package pinned at initialization. Restore the exact "
            "package or create a successor meeting."
        )


def _meeting_software_version_supported(frozen_version: str | None) -> bool:
    """Return whether this runtime can execute a frozen meeting format."""

    # 0.7.3 continues the 0.7.1 meeting runtime/governance format. Keep this
    # allowlist explicit: the 0.7.0 line belongs to ensemble-old.
    return frozen_version in {__version__, "0.7.1"}


def _meeting_governance_docs(repo: MeetingRepository, configured: str | Path) -> str | Path:
    """Locate the exact governance package frozen when this meeting began."""

    reference = repo.root / "human_private/session_configuration.json"
    recorded = None
    if reference.is_file():
        recorded = json.loads(reference.read_text(encoding="utf-8")).get(
            "governance_docs_path"
        )
    frozen = json.loads(
        repo.docs.read_text("identity_private/meeting_manifest.json")
    ).get("governance_digest")
    for candidate in (recorded, configured, *bundled_historical_governance_docs()):
        if candidate is None:
            continue
        path = Path(candidate).expanduser()
        if path.is_dir() and directory_digest(path) == frozen:
            return path
    return recorded or configured


def _resume_selected_meeting(args, repo: MeetingRepository, cfg) -> int:
    command = _resume_kind(repo)
    args._resume_cmd = command
    args._resume_meeting = str(repo.root)
    private_manifest = repo.root / "identity_private/meeting_manifest.json"
    if private_manifest.is_file():
        frozen_version = json.loads(private_manifest.read_text(encoding="utf-8")).get("software_version")
        if frozen_version and not _meeting_software_version_supported(frozen_version):
            if not str(frozen_version).startswith("0.6"):
                raise PolicyNotConfiguredError(
                    f"该会议冻结于软件版本 {frozen_version}；当前 v{__version__} 无兼容执行器。"
                )
            legacy = shutil.which("ensemble-v06")
            if legacy is None:
                raise PolicyNotConfiguredError(
                    "该会议属于 v0.6；请安装 ensemble-v06，不能用 v0.7 原地运行。"
                )
            invocation = [legacy, command, "--meeting", str(repo.root)]
            print(f"检测到 v{frozen_version} 会议；交由 ensemble-v06 继续，冻结规则不变。")
            return subprocess.run(invocation, check=False).returncode
    governance_docs = getattr(args, "governance_docs", None)
    max_output_tokens = getattr(args, "max_output_tokens", None)
    show_progress = not getattr(args, "no_progress", False)
    if show_progress:
        print(_ui(
            f"正在恢复会议 {repo.meeting_id}：读取已冻结进度并定位下一项任务……",
            f"Restoring meeting {repo.meeting_id}: loading frozen progress and locating the next task…",
        ), flush=True)
    if command == "run-audit":
        _audit_not_implemented(repo)
    elif command == "run-report":
        _run_literature_report(
            repo=repo,
            cfg=cfg,
            governance_docs=governance_docs,
            max_output_tokens=max_output_tokens,
            show_progress=show_progress,
        )
    elif command == "run-render":
        _run_scholarly_rendering(
            repo=repo,
            cfg=cfg,
            governance_docs=governance_docs,
            max_output_tokens=max_output_tokens,
            show_progress=show_progress,
        )
    elif command == "run-research":
        _run_research_only(repo=repo, cfg=cfg)
    else:
        _run_general(
            repo=repo,
            cfg=cfg,
            governance_docs=governance_docs,
            max_output_tokens=max_output_tokens,
            show_progress=show_progress,
        )
    _offer_compaction_after_completion(repo, cfg.source_path)
    return 0


def _uses_legacy_runtime(repo: MeetingRepository) -> bool:
    path = repo.root / "identity_private/meeting_manifest.json"
    if not path.is_file():
        return False
    version = json.loads(path.read_text(encoding="utf-8")).get("software_version")
    return bool(version and version != __version__)


def _available_meeting_paths(config_path: str | Path) -> list[Path]:
    local = discover_local_meetings(Path.cwd())
    # Backfill meetings made by older releases as soon as the Human opens the
    # home screen from their parent directory.  This is metadata-only: meeting
    # workspaces and immutable records are not changed.
    for path in local:
        register_meeting(path, config_path)
    paths = list(local)
    paths.extend(Path(entry.path) for entry in indexed_meetings(config_path))
    # Keep the old installation's global list discoverable without migrating
    # or mutating any v0.6 meeting.  Installed users may set an explicit path.
    legacy_config = os.environ.get("ENSEMBLE_LEGACY_CONFIG")
    if legacy_config is None:
        sibling = Path(__file__).resolve().parents[3] / "Project_ENSEMBLE_v0.6-dev/ensemble.toml"
        legacy_config = str(sibling) if sibling.is_file() else None
    if legacy_config and Path(legacy_config).is_file():
        paths.extend(Path(entry.path) for entry in indexed_meetings(legacy_config))
    return list(dict.fromkeys(
        path.resolve() for path in paths
        if (path / "public/meeting_manifest.json").is_file()
    ))


def _select_existing_meeting(wizard: TerminalWizard, config_path: str | Path) -> Path | None:
    paths = _available_meeting_paths(config_path)
    if not paths:
        while True:
            selector = terminal_input(_ui("输入会议 ID 或会议目录（b 返回上一级）: ", "Enter meeting ID or directory (b to go back): ")).strip()
            if selector.lower() == "b":
                return None
            resolved = resolve_indexed_meeting(selector, config_path)
            if resolved is not None:
                return resolved
            print(_ui("没有找到该会议；可输入完整目录，或先在会议目录中运行一次 ensemble。", "Meeting not found. Enter its full directory, or run ensemble once from the meeting directory."))
    options: list[tuple[str, str]] = []
    type_labels = {
        "deliberation": _ui("议事／文献写作", "Deliberation / literature writing"),
        "scholarly_rendering": _ui("学术重绘", "Scholarly rendering"),
        "research": _ui("命题核实", "Claim verification"),
        "audit": _ui("审计", "Audit"),
    }
    for path in paths:
        entry = inspect_meeting(path)
        archived = (path / "public/archive_manifest.json").is_file()
        meeting_type_label = (
            _ui("文献调研", "Literature review")
            if entry.deliverable_type == DeliverableType.LITERATURE_REVIEW.value
            else type_labels.get(entry.meeting_type, entry.meeting_type)
        )
        state = (
            _ui("已归档", "Archived") if archived
            else _ui("已完成", "Completed") if meeting_is_complete(path) else _ui("未完成", "Unfinished")
        )
        created = ""
        if archived:
            try:
                creation_time = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00"))
                if creation_time.tzinfo is not None:
                    creation_time = creation_time.astimezone(timezone.utc)
                    created = _ui("创建日期", "Created") + f" {creation_time.date().isoformat()} UTC · "
                else:
                    created = _ui("创建日期", "Created") + f" {creation_time.date().isoformat()} · "
            except (AttributeError, TypeError, ValueError):
                pass
        location = _ui("当前目录", "Current directory") if path == Path.cwd().resolve() else str(path.parent)
        options.append(
            (
                str(path),
                f"{entry.meeting_id} · {entry.title} · {state} · "
                f"{created}{meeting_type_label} · {location}",
            )
        )
    options.append(("back", "返回上一级菜单"))
    selected = wizard._choose_one("选择会议", options)
    if selected == "back":
        return None
    return Path(selected)


def _successor_config_path(args, repo: MeetingRepository, cfg) -> str:
    """New v0.7 meetings must not silently reuse a legacy governance config."""
    private = repo.root / "identity_private/meeting_manifest.json"
    if private.is_file():
        version = json.loads(private.read_text(encoding="utf-8")).get("software_version")
        if version and version != __version__:
            explicit = getattr(args, "config", None) or os.environ.get("ENSEMBLE_CONFIG")
            if explicit:
                return str(explicit)
            bundled = Path(__file__).resolve().parents[2] / "ensemble.toml"
            if bundled.is_file():
                return str(bundled)
            raise ValueError(
                "从旧版会议召开 v0.7 新会需要当前版本的 --config 或 ENSEMBLE_CONFIG"
            )
    return str(cfg.source_path)


class _SuccessorMenuBack(Exception):
    """Return from inheritance or setup to the completed-meeting menu."""


def _continue_completed_meeting(args, repo: MeetingRepository, cfg, wizard: TerminalWizard) -> int | None:
    while True:
        try:
            return _continue_completed_meeting_once(args, repo, cfg, wizard)
        except (_SuccessorMenuBack, StartupWizardCancelled):
            continue


def _continue_completed_meeting_once(args, repo: MeetingRepository, cfg, wizard: TerminalWizard) -> int | None:
    entry = inspect_meeting(repo.root)
    archive_path = repo.root / "public/archive_manifest.json"
    if archive_path.is_file():
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
        certification = archived.get("source_certification_status")
        print(_ui(f"会议已精简归档：{entry.meeting_id} · {entry.title}", f"Meeting archived: {entry.meeting_id} · {entry.title}"))
        if certification != "ORIGINAL_COMPLETED":
            print(_ui("保留文稿是未经原会议程序认证的完整草稿，仅供后续会议参考。", "The retained complete draft was not certified by the original meeting; successor meetings may use it as reference only."))
    else:
        print(_ui(f"会议已完成：{entry.meeting_id} · {entry.title}", f"Meeting completed: {entry.meeting_id} · {entry.title}"))
    normative_available = (
        entry.deliverable_type == DeliverableType.NORMATIVE_INSTRUMENT.value
        and any((repo.root / relative).is_file() for relative in (
            "public/final/final_report_v2.md", "public/final/final_report.md",
            "public/final/procedurally_certified_resolution.md",
        ))
    )
    rendering_available = (
        (entry.deliverable_type == DeliverableType.LITERATURE_REVIEW.value
         and (repo.root / "public/final/literature_review_report.md").is_file())
        or (entry.deliverable_type == DeliverableType.SCHOLARLY_RENDERING.value
            and (repo.root / "public/final/scholarly_rendering/scholarly_review.md").is_file())
    )
    options = []
    if normative_available:
        options.append(("literature", "依据规范性会议的最终文书和证据包，召开文献调研报告新会"))
    if rendering_available:
        options.append(("render", "新开学术化重绘：可选全文、局部或再次重绘；继承原稿与证据包"))
    options.extend([
        ("continue", "接续召开其他新会；选择需要继承的材料"),
        ("results", "仅查看当前会议成果目录"),
        ("back", "返回接续会议菜单"),
    ])
    choice = wizard._choose_one(
        "是否以该会议为来源召开新会",
        options,
    )
    if choice == "back":
        return None
    if choice == "results":
        print(_ui(f"会议目录：{repo.root}", f"Meeting directory: {repo.root}"))
        for relative in (
            "MEETING_RESULTS.md",
            "FINAL_REPORT.pdf",
            "FINAL_REPORT.md",
            "RESEARCH_RESULT.json",
        ):
            path = repo.root / relative
            if path.exists():
                print(_ui(f"成果：{path}", f"Result: {path}"))
        return 0
    if choice == "render":
        if not rendering_available:
            raise ValueError("只有已完成且已发布 Markdown 正文的文献调研报告可直接转入 Rendering")
        start_args = _interactive_start_arguments(args, cfg_path=_successor_config_path(args, repo, cfg))
        start_args._continuation_parent = str(repo.root)
        start_args._continuation_inheritance = InheritanceMode.BOTH
        start_args._continuation_meeting_type = MeetingType.SCHOLARLY_RENDERING
        start_args._from_meeting_menu = True
        return cmd_start(start_args)
    if choice == "literature":
        start_args = _interactive_start_arguments(args, cfg_path=_successor_config_path(args, repo, cfg))
        start_args._continuation_parent = str(repo.root)
        start_args._continuation_inheritance = InheritanceMode.BOTH
        start_args._continuation_meeting_type = MeetingType.DELIBERATION
        start_args._from_meeting_menu = True
        return cmd_start(start_args)
    inheritance_choice = wizard._choose_one(
            "选择新会议继承的材料",
            [
                (InheritanceMode.EVIDENCE.value, "只继承 Research Desk 证据包与文献包"),
                (
                    InheritanceMode.FINAL_DOCUMENT.value,
                    "只继承最终决议/报告，作为建议性前置文书",
                ),
                (
                    InheritanceMode.BOTH.value,
                    "同时继承证据包与建议性最终文书",
                ),
                ("back", "返回上一菜单"),
            ],
        )
    if inheritance_choice == "back":
        raise _SuccessorMenuBack()
    inheritance = InheritanceMode(inheritance_choice)
    start_args = _interactive_start_arguments(args, cfg_path=_successor_config_path(args, repo, cfg))
    start_args._continuation_parent = str(repo.root)
    start_args._continuation_inheritance = inheritance
    start_args._from_meeting_menu = True
    return cmd_start(start_args)


def _open_meeting(args, path: Path) -> int | None:
    repo = MeetingRepository(path)
    cfg = _load_config(_config_path(getattr(args, "config", None), repo))
    args._resume_cmd = _resume_kind(repo)
    args._resume_meeting = str(repo.root)
    # A direct resume already knows its path; no global index write is needed.
    entry = inspect_meeting(repo.root)
    complete = meeting_is_complete(repo.root)
    if (repo.root / "public/archive_manifest.json").is_file() and not complete:
        raise ValueError("归档会议的保留文稿缺失或归档清单无效；请先从备份恢复，不能接续")
    wizard = TerminalWizard()
    options = []
    if not complete:
        options.append(("continue", "继续原会议流程（沿用已落盘进度）"))
    if latest_draft(repo.root) is not None:
        options.append(("ask", "与主席对话：检索当前最新草稿、证据包和文献"))
    if complete and latest_draft(repo.root) is not None:
        options.append(("corrigendum", "与主席对话并勘误：另存完整新版，保留原稿，不重开审核"))
    if complete and complete_render_source(repo.root) is not None:
        options.append(("additional_formats", "补充排版格式：从已完成文稿生成 HTML、PDF 或两者；不重开会议"))
    options.append(("successor", "以当前会议为来源召开新会议"))
    if complete:
        options.append(("results", "查看已完成会议的成果文件"))
    options.append(("back", "返回会议列表"))
    label = (_ui("已归档", "Archived") if (repo.root / "public/archive_manifest.json").is_file()
             else _ui("已完成", "Completed") if complete else _ui("未完成", "Unfinished"))
    while True:
        choice = wizard._choose_one(
            _ui(f"接续会议 {entry.meeting_id} · {entry.title}（{label}）", f"Open meeting {entry.meeting_id} · {entry.title} ({label})"),
            options,
        )
        if choice == "back":
            return None
        if choice == "additional_formats":
            result = _additional_formats_interactive(repo, wizard)
            if result is not None:
                return result
            continue
        if choice == "successor" and complete:
            try:
                result = _continue_completed_meeting(args, repo, cfg, wizard)
            except StartupWizardCancelled:
                continue
            if result is None:
                continue
            return result
        break
    if choice == "continue":
        print(_ui(f"恢复未完成会议：{entry.meeting_id} · {entry.title}", f"Resuming unfinished meeting: {entry.meeting_id} · {entry.title}"))
        return _resume_selected_meeting(args, repo, cfg)
    if choice == "ask":
        return _chair_qa_interactive(repo, cfg, wizard)
    if choice == "corrigendum":
        return _chair_corrigendum_interactive(repo, cfg, wizard)
    if choice == "results":
        print(_ui(f"直接打开以下文件（都在会议一级目录 {repo.root}）：", f"Open these files directly (all in the meeting's top-level directory {repo.root}):"))
        seen_results: set[Path] = set()
        for path in sorted(repo.root.glob("*（*）.*")):
            if path.is_symlink() and path.suffix.lower() in {".pdf", ".md", ".html"}:
                print(_ui(f"成果（按标题命名）：{path}", f"Result (named after title): {path}"))
                seen_results.add(path.resolve())
        for name in (
            "SUPPLEMENTARY_REPORT.html", "SUPPLEMENTARY_REPORT.pdf",
            "LITERATURE_REVIEW.html", "LITERATURE_REVIEW.pdf", "LITERATURE_REVIEW.md",
            "SCHOLARLY_REVIEW.pdf", "SCHOLARLY_REVIEW.md",
            "FINAL_REPORT_REVISED.pdf", "FINAL_REPORT_REVISED.md",
            "MEETING_RESULTS.md", "FINAL_REPORT.pdf", "FINAL_REPORT.md",
        ):
            candidate = repo.root / name
            if candidate.exists() and candidate.resolve() not in seen_results:
                print(_ui(f"成果：{candidate}", f"Result: {candidate}"))
                seen_results.add(candidate.resolve())
        return 0
    if _has_frozen_readability_failure(repo):
        provisional_rendering_text(repo.root)
        start_args = _interactive_start_arguments(args, cfg_path=_successor_config_path(args, repo, cfg))
        start_args._continuation_parent = str(repo.root)
        start_args._continuation_inheritance = InheritanceMode.BOTH
        start_args._continuation_meeting_type = MeetingType.SCHOLARLY_RENDERING
        start_args._continuation_provisional_source = True
        start_args._from_meeting_menu = True
        if not getattr(start_args, "governance_docs", None):
            start_args.governance_docs = str(Path(__file__).resolve().parents[2] / "docs/governance")
        try:
            return cmd_start(start_args)
        except StartupWizardCancelled:
            return _open_meeting(args, path)
    print(_ui("原会议尚未完成，且没有可安全继承的冻结终稿；请先继续原会议流程。", "This meeting is unfinished and has no safely inheritable frozen final draft; resume its original workflow first."))
    return 0


def _additional_formats_interactive(repo: MeetingRepository, wizard: TerminalWizard) -> int | None:
    source = complete_render_source(repo.root)
    if source is None:
        raise ValueError("当前会议没有可补充排版的完整文稿")
    print(_ui(
        f"将使用已完成的完整文稿（{source.name}）；成果直接显示在会议一级目录，不修改原稿或重开表决。",
        f"Using the completed document ({source.name}); results will appear in the meeting's top-level directory. The source and votes remain unchanged.",
    ))
    choice = wizard._choose_one(_ui("选择要补充的格式", "Choose additional formats"), [
        ("html", "生成可交互 HTML：目录、术语和引文侧栏"),
        ("pdf", "生成 PDF：标题页、目录和分页排版"),
        ("both", "两种格式都生成；一方失败时保留另一方已完成产物"),
        ("back", "返回接续会议菜单，不做更改"),
    ])
    if choice == "back":
        return None
    formats = ("html", "pdf") if choice == "both" else (choice,)
    from project_ensemble.orchestration.report_palette import palette_options, read_meeting_palette
    default_palette = read_meeting_palette(repo.root)
    options = palette_options(wizard.language)
    palette_choice = wizard._choose_one(
        _ui("报告配色（各项列出正文→强调→中间色→摘要框→纸面的色阶）",
            "Report palette (ink → accent → midtone → callout → paper)"),
        [(default_palette, next(label for key, label in options if key == default_palette)
          + _ui("（会议原配色）", " (meeting palette)"))]
        + [(key, label) for key, label in options
           if key != default_palette]
        + [("back", _ui("返回，不生成", "Back without rendering"))],
    )
    if palette_choice == "back":
        return None
    output = render_additional_formats(repo, formats=formats, palette=palette_choice)
    for format_name, path in output.items():
        print(_ui(f"打开补充版 {format_name.upper()}：{path}", f"Open additional {format_name.upper()}: {path}"))
    return 0


def _chair_qa_interactive(repo: MeetingRepository, cfg, wizard: TerminalWizard) -> int:
    catalog = discover_models(cfg, list(cfg.providers))
    options = [(f"{item.provider_id}:{item.model_id}", "主席问答模型") for item in catalog]
    if not options:
        raise ValueError("配置中的供应商没有返回可用模型，无法开启主席问答")
    selected = wizard._choose_one("选择本次主席问答使用的模型", options)
    provider_id, model_id = _provider_model(selected)
    effort = ReasoningEffort(wizard._choose_one(
        "设置主席问答的推理强度",
        wizard._reasoning_options(cfg, ((provider_id, model_id),)),
    ))
    adapters = {provider_id: _load_adapter_for_provider(cfg, provider_id)}
    configured = cfg.providers[provider_id]
    effective_effort = effort if configured.supports_reasoning_effort(model_id, effort.value) else ReasoningEffort.DEFAULT
    service = ChairQuestionService(
        root=repo.root, adapter=adapters[provider_id], model_id=model_id,
        reasoning_effort=effective_effort, retriever=_build_research_retriever(cfg, repo),
        document_fetcher=_research_document_fetcher(cfg),
    )
    print(_ui(f"主席问答已开启 · {service.certification_state} · 文本版本 {service.revision} · {selected}",
              f"Chair Q&A started · {service.certification_state} · text revision {service.revision} · {selected}"))
    print(_ui("只读取会议公开材料；新增检索仅进入问答资料库，不改变原会议。输入 /exit 退出。", "Only public meeting material is read. New retrieval enters the Q&A library, not the original meeting. Type /exit to leave."))
    while True:
        question = terminal_input(_ui("\n你：", "\nYou: ")).strip()
        if question in {"/exit", "/quit", "退出"}:
            return 0
        if not question:
            continue
        print(_ui("主席正在查阅会议资料…", "Chair is reviewing meeting materials..."), flush=True)
        record = service.ask(question)
        print(_ui(f"\n主席（{record['certification_state']}）：", f"\nChair ({record['certification_state']}): ") + str(record['answer']))
        if record["sources"]:
            print(_ui("本次使用的资料：", "Sources used in this answer:"))
            for source in record["sources"]:
                print(
                    f"  [{source['id']}] {source['path']} · {source['locator']} "
                    f"({source['source_state']})"
                )


def _chair_corrigendum_interactive(repo: MeetingRepository, cfg, wizard: TerminalWizard) -> int:
    catalog = discover_models(cfg, list(cfg.providers))
    options = [(f"{item.provider_id}:{item.model_id}", "会后勘误主席") for item in catalog]
    if not options:
        raise ValueError("配置中的供应商没有返回可用模型，无法开启勘误对话")
    selected = wizard._choose_one("选择本次勘误主席模型", options)
    provider_id, model_id = _provider_model(selected)
    effort = ReasoningEffort(wizard._choose_one(
        "设置勘误主席的推理强度",
        wizard._reasoning_options(cfg, ((provider_id, model_id),)),
    ))
    configured = cfg.providers[provider_id]
    effective_effort = (
        effort if configured.supports_reasoning_effort(model_id, effort.value)
        else ReasoningEffort.DEFAULT
    )
    service = ChairCorrigendumService(
        root=repo.root, adapter=_load_adapter_for_provider(cfg, provider_id),
        model_id=model_id, reasoning_effort=effective_effort,
    )
    print(_ui(f"勘误对话已开启 · 当前完整文稿：{service.current()}", f"Corrigendum dialogue started · current full draft: {service.current()}"))
    print(_ui("直接输入可与主席讨论；输入 /edit 加修改要求，主席会直接另存新版 Markdown 和 PDF。", "Type to discuss with the Chair. Use /edit plus instructions to save new Markdown and PDF versions."))
    print(_ui("输入 /typeset 可不改正文，仅用当前排版器另存完整 PDF。", "Use /typeset to save a newly formatted PDF without changing the text."))
    print(_ui("原文与所有历史版本不覆盖；本功能不启动代表审核。输入 /exit 返回。", "Source and historical versions are not overwritten; this does not reopen representative review. Type /exit to return."))
    while True:
        instruction = terminal_input(_ui("\n你：", "\nYou: ")).strip()
        if instruction in {"/exit", "/quit", "退出"}:
            return 0
        if instruction == "/typeset":
            output = service.retypeset()
            print(_ui(f"新版 Markdown：{output['markdown']}", f"New Markdown: {output['markdown']}"))
            print(_ui(f"新版 PDF：{output['pdf']}", f"New PDF: {output['pdf']}"))
            continue
        if not instruction:
            continue
        edit = instruction.startswith("/edit ")
        if edit:
            instruction = instruction[6:].strip()
        print(_ui("主席正在核对当前版本与相关材料…", "Chair is checking the current version against relevant material..."), flush=True)
        result = service.converse(instruction, edit=edit)
        print(_ui("\n主席：", "\nChair: ") + str(result['answer']))
        if result["output"]:
            print(_ui(f"新版 Markdown：{result['output']['markdown']}", f"New Markdown: {result['output']['markdown']}"))
            print(_ui(f"新版 PDF：{result['output']['pdf']}", f"New PDF: {result['output']['pdf']}"))


def cmd_retypeset(args) -> int:
    """Rebuild an archived/completed PDF without starting models or votes."""
    service = ChairCorrigendumService(
        root=Path(args.meeting), adapter=None, model_id="none",
        reasoning_effort=ReasoningEffort.DEFAULT,
    )
    output = service.retypeset()
    print(f"新版 Markdown：{output['markdown']}")
    print(f"新版 PDF：{output['pdf']}")
    print("原始会议正文与 PDF 保持不变。")
    return 0


def cmd_corrigendum_edit(args) -> int:
    """Apply one explicit Human-directed Chair edit to a completed report."""
    cfg = _load_config(_config_path(args.config))
    provider_id, model_id = _provider_model(args.model)
    if provider_id not in cfg.providers:
        raise ValueError(f"未配置的勘误主席供应商：{provider_id}")
    effort = ReasoningEffort(args.reasoning_effort)
    configured = cfg.providers[provider_id]
    if not configured.supports_reasoning_effort(model_id, effort.value):
        effort = ReasoningEffort.DEFAULT
    service = ChairCorrigendumService(
        root=Path(args.meeting), adapter=_load_adapter_for_provider(cfg, provider_id),
        model_id=model_id, reasoning_effort=effort,
    )
    result = service.converse(args.instruction, edit=True)
    print(f"主席说明：{result['answer']}")
    if result["output"]:
        print(f"新版 Markdown：{result['output']['markdown']}")
        print(f"新版 PDF：{result['output']['pdf']}")
    else:
        print("主席未作修改；原稿和现有勘误版本保持不变。")
    return 0


def _has_frozen_readability_failure(repo: MeetingRepository) -> bool:
    manifest_path = repo.root / "public/meeting_manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cert_path = repo.root / "chair_private/literature_report/readability_certification.json"
    return (
        manifest.get("deliverable_type") == DeliverableType.LITERATURE_REVIEW.value
        and cert_path.is_file()
        and json.loads(cert_path.read_text(encoding="utf-8")).get("status")
        == "REVISION_REQUIRED"
        and not meeting_is_complete(repo.root)
    )


def cmd_home(args) -> int:
    wizard = TerminalWizard()
    # The first-run choice is user-scoped, independent of credentials and of
    # the output language frozen in any meeting manifest.  Test doubles and
    # older embedders may provide only the original wizard interface.
    if callable(getattr(wizard, "ensure_language_selected", None)):
        wizard.ensure_language_selected()
    wizard.show_home()
    while True:
        choice = wizard._choose_one(
            "开始使用",
            [
                ("new", "召开新会议"),
                ("existing", "接续已有会议：继续流程、向主席提问，或召开后续新会"),
                ("manage", "管理已有会议：精简归档或彻底删除"),
                ("settings", "设置：界面外观与模型供应商"),
                ("quit", "退出 ENSEMBLE"),
            ],
        )
        if choice == "quit":
            return 0
        if choice == "settings":
            try:
                seed = _config_path(args.config)
            except ValueError:
                seed = None
            configure_interactively(wizard, seed_path=seed)
            continue
        if choice == "manage":
            try:
                index_config = _config_path(args.config)
            except ValueError:
                # The user-global meeting list does not require model credentials.
                index_config = str(Path.cwd() / "ensemble.toml")
            while True:
                path = _select_existing_meeting(wizard, index_config)
                if path is None:
                    break
                _manage_existing_meeting(wizard, path, index_config)
            continue
        try:
            cfg_path = _config_path(args.config)
        except ValueError:
            if choice == "new":
                print("尚未配置模型供应商；请先进入“设置 → 模型供应商”。")
                continue
            cfg_path = str(ensure_user_config())
        cfg = _load_config(cfg_path)
        if choice == "new":
            if not cfg.providers:
                print("没有已配置的模型供应商；请先进入设置。")
                continue
            start_args = _interactive_start_arguments(args, cfg_path=str(cfg.source_path))
            start_args._from_home = True
            try:
                return cmd_start(start_args)
            except StartupWizardCancelled:
                print("已返回 ENSEMBLE 主菜单；没有创建会议。")
                continue
        path = _select_existing_meeting(wizard, cfg.source_path)
        if path is None:
            continue
        result = _open_meeting(args, path)
        if result is None:
            continue
        return result


def _manage_existing_meeting(
    wizard: TerminalWizard, path: Path, config_path: str | Path
) -> None:
    try:
        entry = inspect_meeting(path)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"无法打开该会议的管理页面：{exc}。请返回会议列表重新选择。", file=sys.stderr)
        return
    while True:
        already_archived = (path / "public/archive_manifest.json").is_file()
        action = wizard._choose_one(
            f"管理会议 {entry.meeting_id} · {entry.title}",
            [
                ("compact", "已精简归档：无需重复操作" if already_archived else
                 "精简归档：保留证据包与完整文稿，删除原会议流程"),
                ("delete", "彻底删除：删除此会议目录中的全部文件"),
                ("back", "返回会议列表，不作更改"),
            ],
        )
        if action == "back":
            return
        if action == "compact":
            if already_archived:
                print("该会议已经精简归档，无需再次操作；可返回会议列表或选择其他管理操作。")
                continue
            print(f"目标会议目录：{path.resolve()}")
            try:
                if _confirm_and_compact_meeting(path, config_path):
                    return
            except (ValueError, OSError, RuntimeError) as exc:
                print(f"本次未精简会议：{exc}。会议文件保持不变；请检查原因后重试或返回会议列表。",
                      file=sys.stderr)
            continue
        print(f"目标会议目录：{path.resolve()}")
        print("警告：这会永久删除该会议的证据、文稿、流程和审计记录，无法从 ENSEMBLE 恢复。")
        confirmation = terminal_input(
            f"确认请完整输入 DELETE {entry.meeting_id}；输入 b 返回会议菜单: "
        ).strip()
        if confirmation != f"DELETE {entry.meeting_id}":
            print("已取消；会议文件保持不变。")
            continue
        try:
            receipt, log_path, pid = launch_background_deletion(path)
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"未能提交后台删除任务：{exc}。请检查原因后重试或返回会议列表。",
                  file=sys.stderr)
            continue
        print(f"后台删除任务已启动：{entry.meeting_id}；进程 PID {pid}。可继续管理其他会议。")
        print(f"删除状态：{receipt}（QUEUED → RUNNING → DONE；失败时为 FAILED）")
        print(f"后台日志：{log_path}。状态为 DONE 后，目录与文件不可从 ENSEMBLE 恢复。")
        return


def _confirm_and_compact_meeting(path: Path, config_path: str | Path) -> bool:
    entry = inspect_meeting(path)
    plan = plan_meeting_archive(path)
    print(f"将保留的完整文稿：{plan.document_source} → {plan.document_target}")
    html_outputs = [item for item in plan.retained_files
                    if item.suffix.lower() == ".html" and (
                        item.parent == Path(".")
                        or item.is_relative_to(Path("public/final"))
                        or item.is_relative_to(Path("public/supplementary_rendering"))
                    )]
    print(f"将保留的 HTML 成稿：{len(html_outputs)} 份；一级目录 HTML 入口：{len(plan.html_aliases)} 个。")
    if not html_outputs and not plan.html_aliases:
        print("当前会议没有可保留的最终 HTML；精简不会生成新 HTML，可在完成后先补充渲染。")
    if not plan.original_completed:
        print("注意：原会议尚未完成认证；这份完整草稿只供接续使用，不会伪装成原会议的正式结论。")
    print(
        f"预计删除流程/审计及其他非保留文件 {plan.removed_file_count} 个，"
        f"合计约 {plan.removed_bytes} 字节；删除后原会议不可恢复。"
    )
    confirmation = terminal_input(
        f"确认请完整输入 ARCHIVE {entry.meeting_id}；输入 b 返回会议菜单: "
    ).strip()
    if confirmation != f"ARCHIVE {entry.meeting_id}":
        print("已取消；会议文件保持不变。")
        return False
    try:
        result = compact_meeting(path)
    except (RuntimeError, OSError) as exc:
        if (path / "public/archive_manifest.json").is_file():
            print(
                f"归档文稿已经建立，但旧流程备份清理未完成：{exc}",
                file=sys.stderr,
            )
            register_meeting(path, config_path)
            return True
        raise
    register_meeting(path, config_path)
    print(f"已精简归档 {entry.meeting_id}。完整文稿：{path / result['retained_document_path']}")
    print(f"证据包及文献包保留在：{path / 'public/research'}")
    print("原流程不可恢复；可从主菜单接续此归档会议，召开后续新会。")
    return True


def _offer_compaction_after_completion(repo: MeetingRepository, config_path: str | Path) -> None:
    """Offer only after an interactive run actually reaches a completed state."""
    if not sys.stdin.isatty() or not meeting_is_complete(repo.root):
        return
    if (repo.root / "public/archive_manifest.json").is_file():
        return
    wizard = TerminalWizard()
    try:
        choice = wizard._choose_one(
            f"会议 {repo.meeting_id} 已结束 · 是否精简会议文件",
            [
                ("keep", "暂不精简；保留完整流程、审计记录与恢复能力"),
                ("compact", "精简归档；保留证据和完整文稿，删除原会议流程"),
            ],
        )
        if choice == "compact":
            _confirm_and_compact_meeting(repo.root, config_path)
    except (EOFError, KeyboardInterrupt):
        print("已跳过精简；完整会议文件保持不变。")
    except (ValueError, OSError, RuntimeError) as exc:
        # A completed meeting must remain completed even if it cannot safely
        # be compacted (for example, a Markdown deliverable is missing).
        print(f"本次未精简会议文件：{exc}", file=sys.stderr)


def cmd_settings(args) -> int:
    wizard = TerminalWizard()
    if callable(getattr(wizard, "ensure_language_selected", None)):
        wizard.ensure_language_selected()
    try:
        seed = _config_path(args.config)
    except ValueError:
        seed = None
    configure_interactively(wizard, seed_path=seed)
    return 0


def cmd_open(args) -> int:
    selector_path = Path(args.selector).expanduser()
    direct = None
    for candidate in (selector_path, Path.cwd() / selector_path):
        if (candidate / "public/meeting_manifest.json").is_file():
            direct = candidate.resolve()
            break
    if direct is None:
        cfg = _load_config(_config_path(args.config))
        direct = resolve_indexed_meeting(args.selector, cfg.source_path)
        if direct is None:
            direct = next(
                (path for path in _available_meeting_paths(cfg.source_path)
                 if path.name == args.selector), None
            )
    if direct is None:
        raise ValueError(f"没有找到会议 {args.selector!r}；可使用会议 ID 或完整会议目录")
    return _open_meeting(args, direct) or 0


def cmd_start(args) -> int:
    cfg = _load_config(_config_path(args.config))
    if args.non_interactive:
        meeting_type = MeetingType(args.meeting_type) if args.meeting_type else None
        if args.technician_reasoning_effort and not args.technician_model:
            raise ValueError("--technician-reasoning-effort requires --technician-model")
        if args.inherit and not args.parent_meeting:
            raise ValueError("--inherit requires --parent-meeting")
        if (
            not args.enable_research
            and args.literature_writing_policy != "fast"
            and meeting_type not in {MeetingType.RESEARCH, MeetingType.SCHOLARLY_RENDERING}
            and (args.research_model or args.research_reasoning_effort)
        ):
            raise ValueError(
                "--research-model and --research-reasoning-effort require --enable-research"
            )
        requirements = [
            ("--meeting-type", args.meeting_type),
            ("--provider", args.provider),
            ("--task", args.task),
        ]
        if meeting_type == MeetingType.SCHOLARLY_RENDERING:
            requirements.extend(
                (
                    ("--chair", args.chair),
                    ("--parent-meeting", args.parent_meeting),
                    ("--rendering-science-model", args.rendering_science_model),
                    ("--rendering-citation-model", args.rendering_citation_model),
                    ("--rendering-language", args.rendering_language),
                    ("--rendering-output-format", args.rendering_output_format),
                    ("--research-model", args.research_model),
                    ("--rendering-science-order", args.rendering_science_order),
                )
            )
        elif meeting_type != MeetingType.RESEARCH:
            requirements.append(("--model", args.model))
            if args.literature_writing_policy != "fast":
                requirements.append(("--chair", args.chair))
            if args.deliverable_type == DeliverableType.LITERATURE_REVIEW.value:
                requirements.append(("--writer-model", args.writer_model))
                if args.literature_writing_policy == "fast":
                    requirements.append(("--research-model", args.research_model))
        else:
            requirements.append(("--research-model", args.research_model))
        missing = [name for name, value in requirements if not value]
        if missing:
            raise ValueError(f"non-interactive startup requires: {', '.join(missing)}")
        selection = StartupSelection(
            meeting_type=meeting_type,
            providers=tuple(args.provider),
            models=tuple(args.model or ()),
            chair_model=args.chair,
            task_description=args.task,
            escalation_email=args.email,
            representative_reasoning_effort=ReasoningEffort(
                args.representative_reasoning_effort or ReasoningEffort.DEFAULT.value
            ),
            chair_reasoning_effort=ReasoningEffort(
                args.chair_reasoning_effort or ReasoningEffort.DEFAULT.value
            ),
            research_enabled=(
                args.enable_research
                or args.literature_writing_policy == "fast"
                or meeting_type in {MeetingType.RESEARCH, MeetingType.SCHOLARLY_RENDERING}
            ),
            research_model=args.research_model,
            research_reasoning_effort=(
                ReasoningEffort(args.research_reasoning_effort or ReasoningEffort.DEFAULT.value)
                if args.enable_research
                or args.literature_writing_policy == "fast"
                or meeting_type in {MeetingType.RESEARCH, MeetingType.SCHOLARLY_RENDERING}
                else None
            ),
            research_max_concurrent_claim_groups=args.research_max_concurrent_claim_groups,
            maximum_parallelism=(
                args.maximum_parallelism if args.maximum_parallelism is not None
                else args.deliverable_type == DeliverableType.LITERATURE_REVIEW.value
            ),
            model_concurrency_limit=args.model_concurrency_limit,
            openalex_quota_policy=args.openalex_quota_policy or "wait",
            deliverable_type=DeliverableType(
                args.deliverable_type
                or (
                    DeliverableType.SCHOLARLY_RENDERING.value
                    if meeting_type == MeetingType.SCHOLARLY_RENDERING
                    else DeliverableType.NORMATIVE_INSTRUMENT.value
                )
            ),
            meeting_title=(args.title or natural_meeting_title(args.task)),
            report_palette=args.report_palette or "ocean",
            parent_meeting_path=args.parent_meeting,
            inheritance_mode=(
                InheritanceMode(args.inherit)
                if args.parent_meeting and args.inherit
                else (InheritanceMode.BOTH if args.parent_meeting else None)
            ),
            rendering_science_models=tuple(args.rendering_science_model or ()),
            rendering_citation_models=tuple(args.rendering_citation_model or ()),
            rendering_language=args.rendering_language,
            rendering_academic_skeleton=args.rendering_academic_skeleton,
            rendering_full_abstract=args.rendering_full_abstract,
            rendering_section_abstracts=args.rendering_section_abstracts,
            rendering_segmentation=args.rendering_segmentation,
            rendering_liveliness=args.rendering_liveliness,
            rendering_output_formats=tuple(args.rendering_output_format or ("html",)),
            rendering_science_order=tuple(args.rendering_science_order or ()),
            rendering_science_consultation_authority=(
                getattr(args, "rendering_science_consultation_authority", None) or "human"
            ),
            rendering_target_body_characters=args.rendering_target_body_characters,
            literature_language=args.literature_language,
            deliberation_language=getattr(args, "deliberation_language", None),
            literature_full_abstract=args.literature_full_abstract,
            literature_section_abstracts=args.literature_section_abstracts,
            literature_segmentation=args.literature_segmentation,
            literature_liveliness=args.literature_liveliness,
            literature_signposting=getattr(args, "literature_signposting", None),
            literature_target_body_characters=args.literature_target_body_characters,
            writer_model=args.writer_model,
            writer_reasoning_effort=(
                ReasoningEffort(args.writer_reasoning_effort or ReasoningEffort.DEFAULT.value)
                if args.writer_model is not None else None
            ),
            fast_planner_models=tuple(args.fast_planner_model or ()),
            technician_model=args.technician_model,
            technician_reasoning_effort=(
                ReasoningEffort(args.technician_reasoning_effort or ReasoningEffort.DEFAULT.value)
                if args.technician_model is not None else None
            ),
            literature_writing_policy=(
                (args.literature_writing_policy or "v071")
                if args.deliverable_type == DeliverableType.LITERATURE_REVIEW.value else None
            ),
            human_reference_paths=tuple(getattr(args, "human_reference", None) or ()),
        )
        catalog = discover_models(cfg, list(selection.providers))
        assert_models_were_discovered(selection, catalog)
    else:
        if any(
            (
                args.meeting_type,
                args.provider,
                args.model,
                args.chair,
                args.task,
                args.title,
                args.email,
                args.representative_reasoning_effort,
                args.chair_reasoning_effort,
                args.enable_research,
                args.research_model,
                args.research_reasoning_effort,
                args.research_max_concurrent_claim_groups,
                args.openalex_quota_policy,
                args.deliverable_type,
                args.parent_meeting,
                args.inherit,
                args.rendering_science_model,
                args.rendering_citation_model,
                args.rendering_language,
                args.rendering_academic_skeleton,
                args.rendering_full_abstract,
                args.rendering_section_abstracts,
                args.rendering_segmentation,
                args.rendering_liveliness,
                args.rendering_output_format,
                args.rendering_science_order,
                getattr(args, "rendering_science_consultation_authority", None),
                args.rendering_target_body_characters,
                args.literature_language,
                getattr(args, "deliberation_language", None),
                args.literature_full_abstract,
                args.literature_section_abstracts,
                args.literature_segmentation,
                args.literature_liveliness,
                getattr(args, "literature_signposting", None),
                args.literature_target_body_characters,
                args.literature_writing_policy,
                args.writer_model,
                args.writer_reasoning_effort,
                args.fast_planner_model,
                args.technician_model,
                args.technician_reasoning_effort,
                getattr(args, "human_reference", None),
            )
        ):
            raise ValueError("startup options may only be supplied with --non-interactive")
        wizard = TerminalWizard()
        if callable(getattr(wizard, "ensure_language_selected", None)):
            wizard.ensure_language_selected()
        try:
            selection = wizard.collect(
                cfg,
                parent_meeting_path=getattr(args, "_continuation_parent", None),
                inheritance_mode=getattr(args, "_continuation_inheritance", None),
                forced_meeting_type=getattr(args, "_continuation_meeting_type", None),
                provisional_rendering_source=getattr(
                    args, "_continuation_provisional_source", False
                ),
                prompt_for_title=True,
                prompt_for_references=True,
                prompt_for_claim_dialogue=True,
                prompt_for_literature_dialogue=True,
                prompt_for_deliberation_dialogue=True,
            )
        except StartupWizardCancelled:
            if (getattr(args, "_from_home", False)
                    or getattr(args, "_from_meeting_menu", False)):
                raise
            print("已取消会议初始化；没有创建会议。")
            return 0
        catalog = wizard.discovered_catalog
    if selection.rendering_provisional_source:
        print(
            "正在分块复制源会议文献包与证据文件；大体积资料可能需要数分钟，原会议保持只读。",
            flush=True,
        )
    repo = start_meeting(
        cfg,
        selection,
        governance_docs=args.governance_docs,
        output_directory=Path.cwd(),
        model_catalog=catalog,
    )
    register_meeting(repo.root, cfg.source_path)
    if selection.deliverable_type == DeliverableType.LITERATURE_REVIEW:
        args._resume_cmd = "run-report"
    elif selection.deliverable_type == DeliverableType.SCHOLARLY_RENDERING:
        args._resume_cmd = "run-render"
    elif selection.meeting_type == MeetingType.DELIBERATION:
        args._resume_cmd = "run-general"
    elif selection.meeting_type == MeetingType.RESEARCH:
        args._resume_cmd = "run-research"
    elif selection.meeting_type == MeetingType.AUDIT:
        args._resume_cmd = "run-audit"
    args._resume_meeting = str(repo.root)
    print(f"会议已初始化：{repo.meeting_id}")
    print(f"配置文件：{cfg.source_path}")
    print(f"工作区：{repo.root}")
    if cfg.governance.provider_output_token_limit is None:
        print("单次模型输出上限（控制参数）：ENSEMBLE 默认不限制，由模型供应商决定")
    else:
        print(
            "单次模型输出预算目标（控制参数）："
            f"公开输入上下文上限的 {cfg.governance.provider_output_context_fraction:.0%}；"
            f"无上下文元数据时使用 {cfg.governance.provider_output_token_limit} tokens"
        )
    print(
        "单次模型输入安全预算（接受标准）："
        f"供应商公布 input context 的 {cfg.governance.provider_input_context_fraction:.0%}；"
        "未公布容量的模型使用 "
        f"{cfg.governance.provider_input_token_limit_fallback} 个估算 input tokens"
    )
    print(
        "推理强度（输入控制参数）："
        + (
            f"Research Desk={selection.research_reasoning_effort.value}"
            if selection.meeting_type == MeetingType.RESEARCH
            else (
                f"学术主笔={selection.writer_reasoning_effort.value}；"
                f"智库长={selection.representative_reasoning_effort.value}；无 Chair"
                if selection.literature_writing_policy == "fast"
                else
                f"全体代表={selection.representative_reasoning_effort.value}；"
                f"Chair={selection.chair_reasoning_effort.value}"
            )
        )
    )
    private_manifest = json.loads(
        repo.docs.read_text("identity_private/meeting_manifest.json")
    )
    if selection.meeting_type == MeetingType.DELIBERATION and selection.literature_writing_policy != "fast":
        relaxed = private_manifest.get("decision_rigor") == "relaxed"
        print(
            "决策严谨度（会议冻结的程序控制参数）："
            + ("宽松；原 3/4 高门槛改为全体合格投票者过半" if relaxed else "严格；沿用原 3/4 高门槛")
        )
    if private_manifest.get("maximum_parallelism"):
        chosen_cap = selection.model_concurrency_limit
        print(
            "并行提示："
            + (
                f"本次所选模型的同时在途调用上限由人类设为 {chosen_cap} 路；"
                if chosen_cap is not None else
                "默认每个代表模型最多 4 路同时在途调用；"
            )
            + "并发越高，缓存命中率可能越低。"
        )
    concurrency_limits = private_manifest.get("model_concurrency_limits", {})
    concurrency_sources = private_manifest.get("model_concurrency_sources", {})
    if concurrency_limits:
        print(
            "模型并发上限（同时在途请求数，输入控制参数）："
            + "；".join(
                f"{model}={limit}（{concurrency_sources.get(model, 'UNKNOWN')}）"
                for model, limit in sorted(concurrency_limits.items())
            )
        )
    effective = private_manifest.get("representative_reasoning_effective", {})
    collapsed = {
        model: value
        for model, value in effective.items()
        if value != selection.representative_reasoning_effort.value
    }
    if collapsed:
        print(
            "推理强度兼容映射（实际控制参数）："
            + "；".join(f"{model}→{value}" for model, value in sorted(collapsed.items()))
        )
    if selection.research_enabled and selection.research_model is not None:
        print(
            "Research Desk：已启用；"
            f"模型={selection.research_model[0]}:{selection.research_model[1]}；"
            f"推理强度={selection.research_reasoning_effort.value}；"
            "缓存最大复用时长="
            f"时效性事实 {cfg.research.volatile_freshness_days} days / "
            f"版本化资料 {cfg.research.versioned_freshness_days} days / "
            f"稳定学术事实 {cfg.research.stable_freshness_days} days"
        )
        if selection.meeting_type == MeetingType.DELIBERATION:
            frozen_parallel = json.loads(
                repo.docs.read_text("identity_private/meeting_manifest.json")
            )["research_max_concurrent_claim_groups"]
            print(f"Research Desk 独立任务最大并行度（输入控制参数）：{frozen_parallel} 组")
        if selection.meeting_type == MeetingType.SCHOLARLY_RENDERING:
            query_limit = private_manifest.get("rendering_final_science_query_limit")
            if query_limit is not None:
                print(
                    "终局科学核校查询额度（输入控制参数）："
                    f"每位核校员、每个重绘章节最多 {query_limit} 次；修订周期共享额度"
                )
    else:
        print("Research Desk：未启用；会议保持原有离线知识行为。")
    if selection.technician_model is not None:
        print(
            "Technician：已启用；模型="
            f"{selection.technician_model[0]}:{selection.technician_model[1]}；"
            "故障所需会议片段可能发送给该供应商；不得修改 ENSEMBLE 本体"
        )
    if not args.non_interactive:
        if selection.deliverable_type == DeliverableType.LITERATURE_REVIEW:
            print(_ui("开始执行文献调研报告完整流程；如有源会议，其冻结内容保持只读。", "Starting the literature-review workflow; any source meeting's frozen content remains read-only."))
            _run_literature_report(
                repo=repo,
                cfg=cfg,
                governance_docs=args.governance_docs,
            )
        elif selection.deliverable_type == DeliverableType.SCHOLARLY_RENDERING:
            print(_ui("开始执行学术化重绘；原报告保持只读，科学事实核校先于引文核校。", "Starting scholarly rendering; the source report stays read-only and science review precedes citation review."))
            _run_scholarly_rendering(
                repo=repo,
                cfg=cfg,
                governance_docs=args.governance_docs,
            )
        elif selection.meeting_type == MeetingType.DELIBERATION:
            print(_ui("开始执行当前已实现的总则阶段；实时过程显示在终端。", "Starting the deliberation's general-principle stage; live progress appears in the terminal."))
            _run_general(repo=repo, cfg=cfg, governance_docs=args.governance_docs)
        elif selection.meeting_type == MeetingType.RESEARCH:
            print(_ui("开始执行单项命题核实；检索、证据整理和来源归档过程显示在终端。", "Starting claim verification; retrieval, evidence synthesis, and source archiving appear in the terminal."))
            _run_research_only(repo=repo, cfg=cfg)
        else:
            print(_ui("审计会议已创建；审计执行器尚未实现，当前停留在初始化阶段。", "Audit meeting created; audit execution is not implemented and remains at setup."))
        _offer_compaction_after_completion(repo, cfg.source_path)
    return 0


def _run_research_only(*, repo: MeetingRepository, cfg) -> dict:
    governance_docs = _meeting_governance_docs(repo, cfg.project.governance_docs)
    _assert_meeting_runtime_compatible(
        repo, governance_docs=governance_docs
    )
    while True:
        try:
            return _run_research_only_locked(repo=repo, cfg=cfg)
        except ModelReplacementRequested:
            if not sys.stdin.isatty():
                raise
            _interactive_model_replacement(repo=repo, cfg=cfg)


def _run_research_only_locked(*, repo: MeetingRepository, cfg) -> dict:
    result_relative = Path("public/research/research_only_result.json")
    result_path = repo.root / result_relative
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        _ensure_research_only_links(repo, result_relative)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    progress = ConsoleProgressReporter(meeting_root=repo.root)
    with repo.exclusive_run_lock():
        adapters = _build_meeting_adapters(cfg, repo, require_keys=True)
        output_token_budgets = _configured_output_token_budgets(
            repo=repo, cfg=cfg, adapters=adapters, progress=progress
        )
        input_context_budgets = _configured_input_context_budgets(
            repo=repo, cfg=cfg, adapters=adapters, progress=progress
        )
        engine = MeetingEngine(
            repo=repo,
            adapters=adapters,
            notifier=build_email_notifier(cfg.notifications.email),
            max_retries=cfg.governance.provider_retries,
            retry_base_delay_seconds=cfg.governance.provider_retry_base_delay_seconds,
            progress=progress,
            output_token_budgets=output_token_budgets,
            input_context_budgets=input_context_budgets,
            configured_concurrency_limits=_meeting_concurrency_limits(repo, cfg),
        )
        _install_live_batch_controls(progress=progress, repo=repo, cfg=cfg, engine=engine)
        progress.start_control_listener()
        task = json.loads(repo.docs.read_text("public/task.json"))["description"]
        progress.status(
            MeetingPhase.RESEARCH_ROUND,
            "命题核实 · 正在规范化主张、执行对冲检索并形成公开证据包",
        )
        try:
            packet = ResearchDesk(
                repo=repo,
                engine=engine,
                retriever=_build_research_retriever(cfg, repo),
                freshness_windows=_research_freshness_windows(cfg),
                document_fetcher=_research_document_fetcher(cfg),
                retrieval_max_retries=cfg.governance.provider_retries,
                retrieval_retry_base_delay_seconds=(
                    cfg.governance.provider_retry_base_delay_seconds
                ),
            ).research(
                ResearchRequest(
                    requester_id="HUMAN",
                    stage=ResearchStage.RESEARCH_ONLY,
                    claim=task,
                )
            )
        except ResearchQualityControlError as exc:
            result = {
                "meeting_id": repo.meeting_id,
                "status": "RESEARCH_QC_FAILED",
                "packet_id": None,
                "knowledge_status": None,
                "failure_code": exc.code,
                "failure_summary": exc.summary,
                "disposition": "NO_EVIDENCE_UPGRADE_NONBLOCKING",
            }
            repo.docs.write_once(
                "audit_private/research/research_only_qc_failure.json",
                json.dumps(result, indent=2, ensure_ascii=False),
            )
        else:
            result = {
                "meeting_id": repo.meeting_id,
                "status": "RESEARCH_COMPLETE",
                "packet_id": packet.packet_id,
                "knowledge_status": packet.knowledge_status.value,
                "evidence_packet_path": (
                    f"public/research/evidence_packets/{packet.packet_id}.json"
                ),
                "literature_bundle_path": "public/research/literature_bundle.zip",
            }
        repo.docs.write_once(
            result_relative,
            json.dumps(result, indent=2, ensure_ascii=False),
        )
        _ensure_research_only_links(repo, result_relative)
        repo.events.append(
            "RESEARCH_ONLY_MEETING_COMPLETED",
            result,
            actor="RESEARCH_DESK",
        )
        progress.stop_control_listener()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def _ensure_research_only_links(
    repo: MeetingRepository, result_relative: Path
) -> None:
    for link_name, target in (
        ("RESEARCH_RESULT.json", result_relative),
        ("LITERATURE_BUNDLE.zip", "public/research/literature_bundle.zip"),
    ):
        if not (repo.root / target).exists():
            continue
        ensure_visible_link(repo.root, link_name=link_name, target_relative=target)


def cmd_request_human(args) -> int:
    repo = MeetingRepository(args.meeting)
    cfg = _load_config(_config_path(args.config, repo))
    service = HumanEscalationService(repo, build_email_notifier(cfg.notifications.email))
    notification_enabled = cfg.notifications.email.enabled and repo.escalation_email() is not None
    event = service.request(reason_code=args.reason_code, summary=args.summary)
    outcome = (
        f"已提交 {cfg.notifications.email.transport} 邮件通知"
        if notification_enabled
        else "未配置通知，已留下跳过记录"
    )
    print(f"人工介入请求已记录；{outcome}：{event['event_hash']}")
    return 0


def cmd_resolve_consultation(args) -> int:
    repo = MeetingRepository(args.meeting)
    resolution = HumanConsultationService(repo).resolve(
        issue_id=args.issue_id,
        decision=normalize_consultation_decision(args.decision),
        rationale=args.rationale,
        scope=args.scope,
        human_wording=args.wording,
    )
    print(json.dumps(resolution.model_dump(mode="json"), indent=2, ensure_ascii=False))
    print("Human ruling 已不可变记录；重新运行原会议命令即可从咨询点恢复。", file=sys.stderr)
    return 0


def cmd_science_authority(args) -> int:
    selector = Path(args.meeting).expanduser()
    direct = next(
        (candidate.resolve() for candidate in (selector, Path.cwd() / selector)
         if (candidate / "public/meeting_manifest.json").is_file()),
        None,
    )
    if direct is None:
        cfg = _load_config(_config_path(args.config))
        direct = resolve_indexed_meeting(args.meeting, cfg.source_path)
    if direct is None:
        raise ValueError(f"没有找到会议 {args.meeting!r}；请提供会议 ID 或完整目录")
    repo = MeetingRepository(direct)
    service = ScienceConsultationAuthorityService(repo)
    if args.mode is not None:
        record_path = service.set_mode(args.mode)
        print(f"已由人类设置后续科学事实异议裁决方式：{'主席代裁' if args.mode == 'chair' else '人类裁决'}")
        print(f"授权变更记录：{repo.root / record_path}")
    mode, source = service.current()
    print(f"当前方式：{'主席代裁' if mode == 'chair' else '人类裁决'}；依据：{repo.root / source}")
    print("只适用于本次学术重绘会议尚未裁决的科学事实异议。")
    return 0


def _prompt_for_one_consultation(
    repo: MeetingRepository,
    *,
    input_fn=terminal_input,
    output=None,
    engine: MeetingEngine | None = None,
    governance_docs=None,
    max_output_tokens=None,
) -> bool:
    """Resolve exactly one open consultation in the Human terminal."""
    output = output or sys.stderr
    service = HumanConsultationService(repo)
    issues = service.open_issues()
    if not issues:
        return False
    issue = issues[0]
    if issue.stage == "FAST_SCOPE_QUESTION":
        from project_ensemble.runtime.fast_scope_consultation import prompt_fast_scope_consultation
        return prompt_fast_scope_consultation(
            repo, issue, input_fn=input_fn, output=output, engine=engine,
            max_output_tokens=max_output_tokens,
        )
    private_manifest = repo.root / "identity_private/meeting_manifest.json"
    fast_meeting = (private_manifest.is_file() and
                    json.loads(private_manifest.read_text(encoding="utf-8")).get(
                        "literature_writing_policy") == "fast")
    is_fast_issue = issue.stage.startswith("FAST_") or (
        fast_meeting and issue.stage == "LITERATURE_WRITER_CITATION_REPAIR"
    )
    is_fast_science = issue.stage == "FAST_SCIENCE_REVIEW"
    is_outline_review = issue.stage == "LITERATURE_RESEARCH_OUTLINE"
    is_rendering_scope = issue.stage == "SCHOLARLY_RENDERING_SCOPE"
    is_local_science = issue.reason_code in {
        "LITERATURE_LOCAL_SCIENCE_CHECK_REQUIRED",
        "LITERATURE_LOCAL_SCIENCE_CHECK_REOPENED",
    }
    local_science_context = (
        local_science_review_context(repo.root, issue.context) if is_local_science else {}
    )
    outline_data: dict = {}
    if is_outline_review:
        relative = issue.context.get("outline_path")
        if isinstance(relative, str) and relative:
            root = repo.root.resolve()
            candidate = (root / relative).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                try:
                    loaded = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        outline_data = loaded
                except (OSError, json.JSONDecodeError):
                    pass
    is_science_item = (
        issue.stage == "SCHOLARLY_SCIENCE_REVIEW"
        and "ACCEPT_SCIENCE_OBJECTION" in issue.options
    )
    science_context = service._related_public_records(issue) if is_science_item else {}
    science_comparison = bool(science_context.get("objection"))
    print("", file=output)
    if is_outline_review:
        render_outline_consultation(
            issue.issue_id, issue.question, issue.context, outline_data, output=output
        )
    elif is_rendering_scope:
        print(_ui("┌─ 局部重绘范围 · 待人类确认 ────────────────────────────────────", "┌─ Partial-rendering scope · awaiting Human approval ─────────────"), file=output)
        print(_ui("│ 你的要求：", "│ Your request: ") + str(issue.context.get('human_description', '')), file=output)
        print(_ui("│ 主席说明：", "│ Chair's explanation: ") + str(issue.context.get('proposal_rationale', '')), file=output)
        print(_ui("│ 拟重绘的原文块：", "│ Source blocks proposed for rendering:"), file=output)
        for block in issue.context.get("selected_blocks", []):
            print(f"│   {block['id']} · {block['heading']}", file=output)
        print(
            _ui(
                f"│ 其余 {issue.context.get('preserved_block_count', 0)}/{issue.context.get('total_block_count', 0)} 个原文块保持原样；完整提案：{issue.context.get('proposal_path', '')}",
                f"│ Unchanged source blocks: {issue.context.get('preserved_block_count', 0)}/{issue.context.get('total_block_count', 0)}; full proposal: {issue.context.get('proposal_path', '')}",
            ), file=output,
        )
    elif science_comparison:
        render_science_consultation(issue.issue_id, science_context, output=output)
    elif is_local_science:
        render_local_science_consultation(
            issue.issue_id, local_science_context, output=output,
        )
    elif is_fast_science:
        from project_ensemble.runtime.fast_science_consultation import (
            render_fast_science_consultation,
        )
        render_fast_science_consultation(repo, issue, output=output)
    else:
        print(
            _ui("┌─ 研究总纲人工审阅 ───────────────────────────────────────────", "┌─ Human review of research outline ─────────────────────────────")
            if is_outline_review
            else _ui("┌─ 科学事实异议 · 逐条咨询 ────────────────────────────────────", "┌─ Scientific objections · one at a time ───────────────────────")
            if is_science_item
            else _ui("┌─ 人工程序咨询 ───────────────────────────────────────────────", "┌─ Human procedural consultation ───────────────────────────────"),
            file=output,
        )
    stage_labels = {
        "GENERAL_PRINCIPLE_SEQUENTIAL_AMENDMENT": "总则修正案顺序处理",
        "BALLOT": "表决程序",
        "LITERATURE_RESEARCH_OUTLINE": "文献报告模块划分人工审阅",
        "SCHOLARLY_SCIENCE_REVIEW": "学术化重绘科学事实争议",
        "SCHOLARLY_CITATION_REVIEW": "学术化重绘引文核校争议",
        "SCHOLARLY_RENDERING_PUBLICATION": "学术化重绘最终出版检查",
        "FAST_TASKBOOK_QUESTION": "快速文献调研 · 主笔澄清研究任务",
        "FAST_TASKBOOK_APPROVAL": "快速文献调研 · 人类确认任务书与模块划分",
        "FAST_SCOPE_QUESTION": "快速文献调研 · 模块范围确认",
        "FAST_SCIENCE_REVIEW": "快速文献调研 · 科学异议人工决定",
        "LITERATURE_WRITER_CITATION_REPAIR": "文献调研 · 主笔引文修复待处理",
    }
    if not science_comparison and not is_outline_review and not is_rendering_scope and not is_local_science and not is_fast_science:
        print(f"│ {issue.issue_id} · {ui_text(stage_labels.get(issue.stage, issue.stage), interface_language() or 'zh')}", file=output)
        print(f"│ {issue.question}", file=output)
        if issue.affected_items:
            print(_ui("│ 受影响项目: ", "│ Affected items: ") + ', '.join(issue.affected_items), file=output)
    if issue.stage == "FAST_TASKBOOK_APPROVAL":
        relative = issue.context.get("taskbook_path")
        if isinstance(relative, str):
            candidate = (repo.root / relative).resolve()
            if candidate.is_relative_to(repo.root.resolve()) and candidate.is_file():
                taskbook = json.loads(candidate.read_text(encoding="utf-8"))
                outline = taskbook.get("outline", {})
                print(_ui("│ 研究题目：", "│ Research title: ") + str(outline.get('report_title', '')), file=output)
                print(_ui("│ 范围：", "│ Scope: ") + str(outline.get('scope_note', '')), file=output)
                for label, key in (("硬约束", "hard_constraints"),
                                   ("偏好", "preferences"),
                                   ("待确认", "open_questions"),
                                   ("暂作假设", "provisional_assumptions")):
                    values = taskbook.get(key) or []
                    if values:
                        print(f"│ {ui_text(label, interface_language() or 'zh')}: " + "; ".join(str(item) for item in values), file=output)
                for index, module in enumerate(outline.get("modules", []), 1):
                    print(f"│ {index}. {module.get('title', '')}", file=output)
                    for question in module.get("research_questions", []):
                        print(f"│    · {question}", file=output)
                print(_ui("│ 当前模块数：", "│ Current module count: ")
                      + f"{len(outline.get('modules', []))}"
                      + _ui("；可要求主笔调整为 1–12 个。", "; you may request 1–12 modules."), file=output)
                disciplines = outline.get("proposed_disciplines") or []
                print(_ui("│ 主笔建议的学科门类：", "│ Disciplines proposed by the writer: ")
                      + ("、".join(disciplines) or _ui("待人类指定", "to be specified by the Human")), file=output)
                print(_ui("│ 完整任务书：", "│ Full task brief: ") + relative, file=output)
    if issue.stage == "FAST_SCOPE_QUESTION":
        for item in issue.context.get("questions", []):
            print(_ui("│ 范围问题：", "│ Scope question: ") + str(item), file=output)
        if issue.context.get("proposed_scope_change"):
            print(_ui("│ 拟议局部变更：", "│ Proposed local change: ") + str(issue.context['proposed_scope_change']), file=output)
    if issue.stage == "LITERATURE_WRITER_CITATION_REPAIR":
        for problem in issue.context.get("problems", []):
            print(_ui("│ 未解决的引文问题：", "│ Unresolved citation issue: ") + str(problem), file=output)
        print(_ui("│ 原稿和前三次修复均已落盘；更换主笔模型后可选择再次尝试。",
                  "│ The draft and three repairs are saved; you may change the writer model before retrying."), file=output)
    threshold_labels = {
        "supermajority": "高门槛表决",
        "protective_majority": "保护性表决",
    }
    for threshold in issue.thresholds:
        print(
            _ui(
                f"│ {threshold_labels.get(threshold.name, threshold.name)}: 至少 {threshold.required_votes}/{threshold.eligible_count} 票（{threshold.formula}；规则 {threshold.comparison}）",
                f"│ {ui_text(threshold_labels.get(threshold.name, threshold.name), 'en')}: at least {threshold.required_votes} of {threshold.eligible_count} eligible votes ({threshold.formula}; rule {threshold.comparison})",
            ),
            file=output,
        )
    if is_outline_review:
        menu_width = rule_width(output, maximum=112)
        menu_heading = _ui("┌─ 请选择处理方式 ", "┌─ Choose an action ")
        print(menu_heading + "─" * max(4, menu_width - len(menu_heading)), file=output)
    else:
        print(
            _ui("┌─ 请选择处理方式 ──────────────────────────────────────────────", "┌─ Choose an action ─────────────────────────────────────────────")
            if science_comparison
            else "├──────────────────────────────────────────────────────────────",
            file=output,
        )
    for index, option in enumerate(issue.options, start=1):
        print(f"│  {index}. {_option_ui(option)}", file=output)
    if engine is not None and governance_docs is not None and not is_fast_issue:
        print(
            _ui("│  c. 与主席讨论研究范围或模块划分（每次一条）", "│  c. Discuss scope or modules with the Chair (one question at a time)")
            if is_outline_review
            else _ui("│  c. 向主席追问局部重绘边界（每次一条）", "│  c. Ask the Chair about partial-rendering boundaries (one at a time)")
            if is_rendering_scope
            else _ui("│  c. 向主席追问当前这条异议（每次一条）", "│  c. Ask the Chair about this objection (one question at a time)")
            if is_science_item
            else _ui("│  c. 用自然语言向主席询问这一程序问题（每次一条）", "│  c. Ask the Chair about this procedure (one question at a time)"),
            file=output,
        )
    if is_science_item:
        print(_ui("│  v. 查看完整原文、当前稿和本条异议", "│  v. View full source, current draft, and objection"), file=output)
        print(_ui("│  a. 授权主席裁决此后科学事实异议（会记录授权，可随时撤销）", "│  a. Authorize Chair to decide later science objections (recorded and revocable)"), file=output)
    if is_local_science:
        print(_ui("│  v. 查看完整科学疑点与相关返修前后段落", "│  v. View full scientific concerns and before/after paragraphs"), file=output)
    if is_outline_review:
        print(_ui("│  m. 单个模块细节   s. 完整范围意见   a. 文章骨架", "│  m. Module details   s. Full scope comments   a. Article structure"), file=output)
        print(_ui("│  d. 建议学科       v. 原始咨询文书", "│  d. Suggested disciplines   v. Original consultation"), file=output)
    print(
        "└" + "─" * (menu_width - 1)
        if is_outline_review else "└──────────────────────────────────────────────────────────────",
        file=output,
    )
    if len(issues) > 1:
        print(_ui(f"另有 {len(issues) - 1} 条咨询排队；按一次一条处理。", f"{len(issues) - 1} more consultations are queued; resolve one at a time."), file=output)
    while True:
        try:
            raw = input_fn(
                _ui("请选择编号（回车保持暂停）: ", "Choose a number (Enter to stay paused): ") if is_fast_issue
                else _ui("请选择编号，或输入 c 询问主席（回车保持暂停）: ", "Choose a number or c to ask the Chair (Enter to stay paused): ")
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print(_ui("\n保持 PAUSED；未记录 Human ruling。", "\nMeeting remains paused; no Human ruling was recorded."), file=output)
            return False
        if not raw:
            print(_ui("保持 PAUSED；未记录 Human ruling。", "Meeting remains paused; no Human ruling was recorded."), file=output)
            return False
        if is_outline_review and raw.lower() in {"m", "s", "a", "d", "v"}:
            detail_kind = raw.lower()
            module_index = None
            if detail_kind == "m":
                modules = outline_data.get("modules") or []
                if not modules:
                    print(_ui("冻结总纲文件暂不可用；可按 v 查看原始咨询文书。", "Frozen outline unavailable; press v to view the original consultation."), file=output)
                    continue
                try:
                    chosen = input_fn(
                        _ui(f"查看第几个模块？输入 1-{len(modules)}；回车返回审阅菜单: ", f"Which module? Enter 1-{len(modules)}; Enter returns to review menu: ")
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    print(_ui("\n保持 PAUSED；未记录 Human ruling。", "\nMeeting remains paused; no Human ruling was recorded."), file=output)
                    return False
                if not chosen:
                    continue
                if not chosen.isascii() or not chosen.isdigit() or not 1 <= int(chosen) <= len(modules):
                    print(_ui("模块编号无效；没有改变总纲或记录决定。", "Invalid module number; outline and decision remain unchanged."), file=output)
                    continue
                module_index = int(chosen) - 1
            render_outline_detail(
                detail_kind, issue.question, issue.context, outline_data,
                output=output, module_index=module_index,
            )
            continue
        if raw.lower() in {"v", "view"} and is_science_item:
            context = science_context
            print(_ui("\n原文：\n", "\nSource text:\n") + str(context.get("source_markdown", _ui("未找到原文", "Source not found"))), file=output)
            print(_ui("\n当前重绘稿：\n", "\nCurrent rendering draft:\n") + str(context.get("current_redraw", _ui("未找到当前稿", "Current draft not found"))), file=output)
            print(_ui("\n本条异议：\n", "\nThis objection:\n") + json.dumps(context.get("objection", {}), ensure_ascii=False, indent=2), file=output)
            print(_ui("\n主席完整建议：\n", "\nFull Chair advice:\n") + json.dumps(context.get("chair_advice", {}), ensure_ascii=False, indent=2), file=output)
            print("", file=output)
            continue
        if raw.lower() in {"v", "view"} and is_local_science:
            render_local_science_consultation(
                issue.issue_id, local_science_context, output=output, full=True,
            )
            continue
        if raw.lower() in {"a", "authorize"} and is_science_item:
            if engine is None:
                print(_ui("本入口无法调用主席；请用 science-authority 命令设置后恢复会议。", "Chair cannot be invoked here; set authority with science-authority and resume."), file=output)
                continue
            record = ScienceConsultationAuthorityService(repo).set_mode("chair")
            print(_ui(f"已授权主席处理本条及此后科学事实异议；记录：{record}", f"Chair authorized to decide this and later scientific objections; record: {record}"), file=output)
            try:
                resolution = service.ask_chair_to_decide_science_objection(
                    issue=issue, engine=engine, max_output_tokens=max_output_tokens,
                )
            except Exception as exc:
                engine.progress.chair_ruling_notice(
                    f"{issue.issue_id} 未完成，异议保持未决：{exc}", failed=True
                )
                return False
            if resolution is not None:
                engine.progress.chair_ruling_notice(
                    f"{issue.issue_id}: {_option_ui(resolution.decision)}"
                    f"；{resolution.rationale}"
                )
                return True
            print(_ui("授权已撤销或主席尚未完成裁决；本条仍待处理。", "Authority was revoked or the Chair has not ruled; this item remains open."), file=output)
            continue
        if (not is_fast_issue and raw.lower() in {"c", "chair", "?"}
                and engine is not None and governance_docs is not None):
            try:
                question = input_fn(
                    _ui("向主席提出一个研究范围或模块划分问题（一条）: ", "Ask the Chair one question about scope or modules: ")
                    if is_outline_review
                    else _ui("就当前异议向主席提问（一条）: ", "Ask one question about this objection: ")
                    if is_science_item
                    else _ui("向主席提出一个程序问题（一条）: ", "Ask the Chair one procedural question: ")
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print(_ui("\n保持当前咨询；尚未作出选择。", "\nConsultation remains open; no choice was made."), file=output)
                continue
            if not question:
                print(_ui("问题不能为空。", "Question cannot be empty."), file=output)
                continue
            turn = service.ask_chair(
                issue_id=issue.issue_id,
                question=question,
                engine=engine,
                governance_docs=governance_docs,
                max_output_tokens=max_output_tokens,
            )
            print("", file=output)
            print(_ui(f"主席（第 {turn.turn_number} 次解释）:", f"Chair (explanation {turn.turn_number}):"), file=output)
            for line in turn.chair_answer.splitlines():
                print(f"  {line}", file=output)
            print("", file=output)
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(issue.options):
            decision = issue.options[int(raw) - 1]
            break
        suffix = _ui("，或输入 c 向主席提问", ", or c to ask the Chair") if engine is not None else ""
        if is_science_item:
            suffix += _ui("，输入 v 查看完整对照", ", or v to view the full comparison")
        print(_ui(f"请输入 1-{len(issue.options)} 中的一个编号{suffix}。", f"Enter a number from 1 to {len(issue.options)}{suffix}."), file=output)
    if decision == "PAUSE_FOR_MANUAL_REVIEW":
        print(_ui("保持暂停；本项未保存为最终裁决，下次恢复可重新选择。",
                  "Meeting remains paused; this was not saved as a final ruling, so you may choose again on resume."), file=output)
        return False
    human_wording = None
    if decision == "USE_HUMAN_WORDING":
        while True:
            try:
                human_wording = input_fn(
                    _ui("请回答主笔的问题（一条）: ", "Answer the writer's question (one item): ") if is_fast_issue
                    else _ui("请输入由 Human 决定的最终正文表述（必填）: ", "Enter final wording decided by the Human (required): ")
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print(_ui("\n保持 PAUSED；未记录 Human ruling。", "\nMeeting remains paused; no Human ruling was recorded."), file=output)
                return False
            if human_wording:
                break
            print(_ui("最终正文表述不能为空。", "Final wording cannot be empty."), file=output)
    pending_audience_profile = None
    profile_relative = None
    if (decision in {"APPROVE_OUTLINE", "APPROVE_TASKBOOK"}
            and "proposed_disciplines" in issue.context):
        cycle = int(issue.context["cycle"])
        suffix = f"FAST-{cycle:03d}" if decision == "APPROVE_TASKBOOK" else f"C{cycle:03d}"
        profile_relative = Path(f"public/literature_report/audience_profile-{suffix}.json")
        if not (repo.root / profile_relative).exists():
            proposed = issue.context.get("proposed_disciplines") or []
            print((_ui("\n主笔拟定的学科：", "\nDisciplines proposed by the writer: ")
                   if decision == "APPROVE_TASKBOOK" else
                   _ui("\n主席拟定的学科：", "\nDisciplines proposed by the Chair: "))
                  + ("、".join(proposed) or _ui("暂无", "none")), file=output)
            print(_ui("可修改学科清单；专业度为读者在该学科的预期熟悉程度，不影响证据标准。", "You may edit the discipline list. Proficiency means expected reader familiarity; it does not change evidence standards."), file=output)
            print(_ui("专业度锚点：1 无先验知识；2 读过科普；3 相关专业本科生；4 相关专业新近毕业生；5 相关专业长期研究者。", "Proficiency anchors: 1 no prior knowledge; 2 popular-science reader; 3 relevant undergraduate; 4 recent graduate; 5 active researcher."), file=output)
            try:
                raw = input_fn(_ui("学科清单（逗号分隔；回车沿用主席建议）: ", "Disciplines (comma-separated; Enter keeps Chair's suggestions): ")).strip()
                disciplines = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()] if raw else proposed
                disciplines = list(dict.fromkeys(disciplines))
                if not disciplines:
                    print(_ui("至少指定一个学科；保持 PAUSED。", "Specify at least one discipline; meeting stays paused."), file=output)
                    return False
                proficiency = {}
                for discipline in disciplines:
                    while True:
                        level = input_fn(_ui(f"读者对「{discipline}」的专业度 1–5: ", f"Reader proficiency in {discipline}, 1–5: ")).strip()
                        if level in {"1", "2", "3", "4", "5"}:
                            proficiency[discipline] = int(level)
                            break
                        print(_ui("请输入 1–5。", "Enter 1–5."), file=output)
                glossary = True
                while True:
                    choice = input_fn(_ui(
                        "在摘要后、正文前加入独立术语表（不计正文长度预算）？[Y/n]: ",
                        "Add a separate glossary after the abstract and before the body "
                        "(excluded from the body-length target)? [Y/n]: ",
                    )).strip().lower()
                    if choice in {"", "n", "no", "y", "yes"}:
                        glossary = choice not in {"n", "no"}
                        break
                pending_audience_profile = {
                    "meeting_id": repo.meeting_id,
                    "outline_issue_id": issue.issue_id,
                    "proficiency_scale": "1_LEAST_FAMILIAR_TO_5_MOST_FAMILIAR",
                    "disciplines": proficiency,
                    "article_skeleton": issue.context.get("article_skeleton", []),
                    "glossary_appendix": glossary,
                    "glossary_placement": "front" if glossary else None,
                }
            except (EOFError, KeyboardInterrupt):
                print(_ui("\n保持 PAUSED；未记录读者画像或 Human ruling。", "\nMeeting remains paused; no audience profile or Human ruling was recorded."), file=output)
                return False
    if decision in {"ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION"}:
        rationale = consultation_option_label(decision)
    elif decision == "USE_WRITER_DEFAULT":
        rationale = "人类已授权主笔对此项采用合理默认值；无须人类再次确认。"
    else:
        rationale = None
    substantive_instruction_required = decision in {
        "REJECT_AND_REPLAN", "REVISE_RENDERING_SCOPE", "REVISE_SKELETON_ONLY",
        "DIRECT_CHAIR_SCIENCE_REVISION", "REVISE_TASKBOOK",
    }
    while rationale is None:
        try:
            rationale_prompt = _ui((
                "请具体说明不批准的异议或你决定的新范围（供下一轮完整重做）: "
                if decision == "REJECT_AND_REPLAN"
                else "请说明要增加、移除或细化的重绘范围: "
                if decision == "REVISE_RENDERING_SCOPE"
                else "请说明文章骨架哪里不合适、希望主席怎样局部修改: "
                if decision == "REVISE_SKELETON_ONLY"
                else "请用一句自然语言说明希望主席怎样修改当前稿: "
                if decision == "DIRECT_CHAIR_SCIENCE_REVISION"
                else "请输入期望模块总数（1–12），或说明希望主笔如何修改任务书: "
                if decision == "REVISE_TASKBOOK"
                else "给主笔／主席的附言（可选；回车跳过）: "
            ), (
                "State concrete objections or your chosen new scope (for complete replanning): "
                if decision == "REJECT_AND_REPLAN"
                else "Describe additions, removals, or refinements to the rendering scope: "
                if decision == "REVISE_RENDERING_SCOPE"
                else "Explain what is wrong with the article structure and what the Chair should edit: "
                if decision == "REVISE_SKELETON_ONLY"
                else "In one sentence, tell the Chair how to revise the current draft: "
                if decision == "DIRECT_CHAIR_SCIENCE_REVISION"
                else "Enter the desired total module count (1–12), or describe taskbook revisions: "
                if decision == "REVISE_TASKBOOK"
                else "Optional note to the writer/Chair (Enter to skip): "
            ))
            entered = input_fn(rationale_prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print(_ui("\n保持 PAUSED；未记录 Human ruling。", "\nMeeting remains paused; no Human ruling was recorded."), file=output)
            return False
        if entered:
            if decision == "REVISE_TASKBOOK" and entered.isdecimal():
                target_count = int(entered)
                if not 1 <= target_count <= 12:
                    print(_ui("模块总数须在 1–12 之间。", "The total module count must be 1–12."), file=output)
                    continue
                entered = (
                    f"请将任务书的一级研究模块调整为恰好 {target_count} 个；"
                    "按需要独立回答的关键问题重新划清边界和依赖，保留原研究范围与已确认约束，"
                    "不要仅增加小节，也不要为了凑数设置空洞模块。"
                )
            rationale = entered
            break
        if not substantive_instruction_required:
            rationale = f"Human selected {decision}; no additional note."
            break
        print(_ui("需要具体修改方向，不能为空。", "A concrete revision instruction is required."), file=output)
    if pending_audience_profile is not None and profile_relative is not None:
        repo.docs.write_once(profile_relative, json.dumps(
            pending_audience_profile, indent=2, ensure_ascii=False
        ))
    service.resolve(
        issue_id=issue.issue_id,
        decision=decision,
        rationale=rationale,
        human_wording=human_wording,
        scope="THIS_CONSULTATION_ONLY",
    )
    remaining = service.open_issues()
    if is_science_item and remaining:
        print(
            _ui(
                f"已记录 {issue.issue_id}：{consultation_option_label(decision)}；本轮仍有 {len(remaining)} 条异议，全部处理后主席才会修稿。",
                f"Recorded {issue.issue_id}: {_option_ui(decision)}. {len(remaining)} objections remain in this round; the Chair revises only after all are resolved.",
            ),
            file=output,
        )
    else:
        print(
            _ui(f"已记录 {issue.issue_id}：{consultation_option_label(decision)}；会议继续。", f"Recorded {issue.issue_id}: {_option_ui(decision)}. Meeting continues."),
            file=output,
        )
    return True


def _run_general(*, repo, cfg, governance_docs=None, max_output_tokens=None, show_progress=True):
    governance_docs = _meeting_governance_docs(
        repo, governance_docs or cfg.project.governance_docs
    )
    _assert_meeting_runtime_compatible(
        repo, governance_docs=governance_docs
    )
    while True:
        try:
            with repo.exclusive_run_lock():
                return _run_general_locked(
                    repo=repo,
                    cfg=cfg,
                    governance_docs=governance_docs,
                    max_output_tokens=max_output_tokens,
                    show_progress=show_progress,
                )
        except ModelReplacementRequested:
            if not (show_progress and sys.stdin.isatty()):
                raise
            _interactive_model_replacement(repo=repo, cfg=cfg)


def _run_general_locked(*, repo, cfg, governance_docs=None, max_output_tokens=None, show_progress=True):
    public_manifest = json.loads(repo.docs.read_text("public/meeting_manifest.json"))
    if public_manifest.get("deliverable_type") == DeliverableType.LITERATURE_REVIEW.value:
        raise ValueError(
            "literature-review meetings must be resumed with ensemble run-report"
        )
    notifier = build_email_notifier(cfg.notifications.email)
    progress = ConsoleProgressReporter(meeting_root=repo.root) if show_progress else NullProgressReporter()
    adapters = _build_meeting_adapters(cfg, repo, require_keys=True)
    output_token_budgets = {}
    if max_output_tokens is None:
        output_token_budgets = _configured_output_token_budgets(
            repo=repo, cfg=cfg, adapters=adapters, progress=progress
        )
    input_context_budgets = _configured_input_context_budgets(
        repo=repo, cfg=cfg, adapters=adapters, progress=progress
    )
    engine = MeetingEngine(
        repo=repo,
        adapters=adapters,
        notifier=notifier,
        max_retries=cfg.governance.provider_retries,
        retry_base_delay_seconds=cfg.governance.provider_retry_base_delay_seconds,
        progress=progress,
        output_token_budgets=output_token_budgets,
        input_context_budgets=input_context_budgets,
        configured_concurrency_limits=_meeting_concurrency_limits(repo, cfg),
    )
    _install_live_batch_controls(progress=progress, repo=repo, cfg=cfg, engine=engine)
    progress.start_control_listener()
    private_manifest = json.loads(
        repo.docs.read_text("identity_private/meeting_manifest.json")
    )
    research_round_runner = None
    if private_manifest.get("research_enabled", False):
        research_round_runner = ResearchRoundRunner(
            repo=repo,
            engine=engine,
            governance_docs=governance_docs or cfg.project.governance_docs,
            retriever=_build_research_retriever(cfg, repo),
            document_fetcher=_research_document_fetcher(cfg),
            freshness_windows=_research_freshness_windows(cfg),
            max_output_tokens=max_output_tokens,
            max_concurrent_claim_groups=(
                private_manifest.get("research_max_concurrent_claim_groups")
                or cfg.research.max_concurrent_claim_groups
            ),
            retrieval_max_retries=cfg.governance.provider_retries,
            retrieval_retry_base_delay_seconds=(
                cfg.governance.provider_retry_base_delay_seconds
            ),
        )
    runner = GeneralPrincipleRunner(
        repo=repo,
        engine=engine,
        governance_docs=governance_docs or cfg.project.governance_docs,
        max_output_tokens=max_output_tokens,
        continue_into_detailed=True,
        continue_into_clause_review=True,
        research_round_runner=research_round_runner,
    )
    while True:
        result = runner.run()
        if (
            result.paused_reason == "HUMAN_CONSULTATION_REQUIRED"
            and show_progress
            and sys.stdin.isatty()
        ):
            progress.stop_control_listener()
            with progress.consultation_display():
                consultation_resolved = _prompt_for_one_consultation(
                    repo,
                    engine=engine,
                    governance_docs=governance_docs or cfg.project.governance_docs,
                    max_output_tokens=max_output_tokens,
                )
            if consultation_resolved:
                progress.start_control_listener()
                continue
        break
    progress.stop_control_listener()
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return result


def cmd_run_general(args) -> int:
    repo = MeetingRepository(args.meeting)
    if _has_frozen_readability_failure(repo):
        return _open_meeting(args, repo.root)
    cfg = _load_config(_config_path(args.config, repo))
    register_meeting(repo.root, cfg.source_path)
    if _uses_legacy_runtime(repo):
        return _resume_selected_meeting(args, repo, cfg)
    actual = _resume_kind(repo)
    if actual != "run-general":
        print(
            f"兼容映射：旧入口 run-general → {actual}；会议类型以冻结 manifest 为准。",
            file=sys.stderr,
        )
        return _resume_selected_meeting(args, repo, cfg)
    _run_general(
        repo=repo,
        cfg=cfg,
        governance_docs=args.governance_docs,
        max_output_tokens=args.max_output_tokens,
        show_progress=not args.no_progress,
    )
    _offer_compaction_after_completion(repo, cfg.source_path)
    return 0


def _run_literature_report(
    *,
    repo,
    cfg,
    governance_docs=None,
    max_output_tokens=None,
    show_progress=True,
):
    from project_ensemble.orchestration.literature_fast import FastResearchDeskFailure

    governance_docs = _meeting_governance_docs(
        repo, governance_docs or cfg.project.governance_docs
    )
    _assert_meeting_runtime_compatible(
        repo, governance_docs=governance_docs
    )
    while True:
        try:
            return _run_literature_report_locked(
                repo=repo,
                cfg=cfg,
                governance_docs=governance_docs,
                max_output_tokens=max_output_tokens,
                show_progress=show_progress,
            )
        except (ProviderContentRejectedError, FastResearchDeskFailure) as exc:
            manifest_path = repo.root / "identity_private/meeting_manifest.json"
            is_fast = manifest_path.is_file() and json.loads(
                manifest_path.read_text(encoding="utf-8")
            ).get("literature_writing_policy") == "fast"
            if not (is_fast and show_progress and sys.stdin.isatty()):
                raise
            if not _interactive_research_desk_fallback(repo=repo, cfg=cfg, error=exc):
                print("已暂停会议；核查成功项和原始问题均保持落盘，可稍后恢复。", file=sys.stderr)
                return None
        except ModelReplacementRequested:
            if not (show_progress and sys.stdin.isatty()):
                raise
            _interactive_model_replacement(repo=repo, cfg=cfg)


def _run_literature_report_locked(
    *,
    repo,
    cfg,
    governance_docs=None,
    max_output_tokens=None,
    show_progress=True,
):
    with repo.exclusive_run_lock():
        progress = ConsoleProgressReporter(meeting_root=repo.root) if show_progress else NullProgressReporter()
        adapters = _build_meeting_adapters(cfg, repo, require_keys=True)
        output_token_budgets = {}
        if max_output_tokens is None:
            output_token_budgets = _configured_output_token_budgets(
                repo=repo, cfg=cfg, adapters=adapters, progress=progress
            )
        engine = MeetingEngine(
            repo=repo,
            adapters=adapters,
            notifier=build_email_notifier(cfg.notifications.email),
            max_retries=cfg.governance.provider_retries,
            retry_base_delay_seconds=cfg.governance.provider_retry_base_delay_seconds,
            progress=progress,
            output_token_budgets=output_token_budgets,
            input_context_budgets=_configured_input_context_budgets(
                repo=repo, cfg=cfg, adapters=adapters, progress=progress
            ),
            configured_concurrency_limits=_meeting_concurrency_limits(repo, cfg),
        )
        _install_live_batch_controls(progress=progress, repo=repo, cfg=cfg, engine=engine)
        progress.start_control_listener()
        private_manifest = json.loads(
            repo.docs.read_text("identity_private/meeting_manifest.json")
        )
        exploration_enabled = bool(private_manifest.get("planning_exploration_enabled", False))
        research_desk = ResearchDesk(
            repo=repo,
            engine=engine,
            retriever=_build_research_retriever(cfg, repo),
            freshness_windows=_research_freshness_windows(cfg),
            document_fetcher=_research_document_fetcher(cfg),
            max_output_tokens=max_output_tokens,
            retrieval_max_retries=cfg.governance.provider_retries,
            retrieval_retry_base_delay_seconds=(
                cfg.governance.provider_retry_base_delay_seconds
            ),
        )
        progress.live_research_desk = research_desk
        if private_manifest.get("literature_writing_policy") == "fast":
            from project_ensemble.orchestration.literature_fast import (
                FastLiteratureRunner, FastResearchDeskFailure,
                FastResearchDeskPause, FastResearchDeskRetry,
            )
            from project_ensemble.orchestration.literature_writing_v071 import LiteratureWritingPaused
            execution = FastLiteratureRunner(
                repo=repo, engine=engine,
                governance_docs=governance_docs or cfg.project.governance_docs,
                research_desk=research_desk, max_output_tokens=max_output_tokens,
            )
            progress.live_fast_runner = execution
            def refresh_runtime_budgets() -> None:
                engine.input_context_budgets = _configured_input_context_budgets(
                    repo=repo, cfg=cfg, adapters=adapters,
                )
                if max_output_tokens is None:
                    engine.output_token_budgets = _configured_output_token_budgets(
                        repo=repo, cfg=cfg, adapters=adapters,
                    )

            def refresh_research_retriever() -> None:
                # A Ctrl+R quota-policy change is committed after every in-flight
                # request finishes; no worker may keep using the old backend route.
                from project_ensemble.research.source_reading import SourceReader

                retriever = _build_research_retriever(cfg, repo)
                previous_reader = research_desk.source_reader
                research_desk.retriever = retriever
                research_desk.source_reader = SourceReader(
                    repo=repo, retriever=retriever,
                    document_fetcher=previous_reader.fetcher,
                    max_sources=previous_reader.max_sources,
                )

            def refresh_concurrency_limits() -> None:
                engine.configured_concurrency_limits = _meeting_concurrency_limits(repo, cfg)
                research_desk.update_model_concurrency_limit(
                    engine.participant_concurrency_limit("RESEARCH_DESK")
                )

            execution.batch_replacement_applied = refresh_runtime_budgets
            execution.batch_retriever_refresh = refresh_research_retriever
            execution.batch_concurrency_refresh = refresh_concurrency_limits
            if show_progress and sys.stdin.isatty():
                execution.batch_control_callback = lambda: _interactive_model_replacement(
                    repo=repo, cfg=cfg, deferred=True,
                )
                execution.batch_daily_quota_callback = lambda error: _interactive_openalex_daily_fallback(
                    error=error, tavily_available=bool(cfg.research.tavily.enabled),
                )
                def offer_fast_research_fallback(error) -> bool:
                    retry = _interactive_research_desk_fallback(
                        repo=repo, cfg=cfg, error=error, run_lock_held=True,
                    )
                    if retry:
                        refresh_runtime_budgets()
                    return retry

                execution.batch_failure_callback = offer_fast_research_fallback
            while True:
                try:
                    result = execution.run()
                    break
                except FastResearchDeskRetry:
                    progress.info("其余独立核查已经完成；现在只重新提交未落盘的问题")
                    continue
                except FastResearchDeskPause:
                    progress.shutdown_control_listener()
                    print("已暂停；本批次其他已完成的证据包均已落盘，可稍后恢复。", file=sys.stderr)
                    return None
                except LiteratureWritingPaused as exc:
                    progress.stop_control_listener()
                    progress.status(MeetingPhase.PAUSED, exc.reason_code)
                    if not (show_progress and sys.stdin.isatty()):
                        print(json.dumps({"meeting_id": repo.meeting_id, "next_phase": "PAUSED",
                                          "paused_reason": exc.reason_code}, ensure_ascii=False))
                        return None
                    with progress.consultation_display():
                        consultation_resolved = _prompt_for_one_consultation(
                            repo, engine=engine,
                            governance_docs=governance_docs or cfg.project.governance_docs,
                            max_output_tokens=max_output_tokens,
                        )
                    if not consultation_resolved:
                        return None
                    progress.start_control_listener()
                except ProviderContentRejectedError:
                    progress.stop_control_listener()
                    raise
                except FastResearchDeskFailure:
                    progress.shutdown_control_listener()
                    raise
            progress.stop_control_listener()
            print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
            return result
        planning_runner = LiteratureReportPlanningRunner(
            repo=repo,
            engine=engine,
            governance_docs=governance_docs or cfg.project.governance_docs,
            max_output_tokens=max_output_tokens,
            exploration_service=(
                ResearchExplorationService(research_desk, max_output_tokens=max_output_tokens)
                if exploration_enabled else None
            ),
        )
        while True:
            planning = planning_runner.run()
            if (
                planning.next_phase == MeetingPhase.PAUSED
                and planning.paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
                and show_progress
                and sys.stdin.isatty()
            ):
                progress.stop_control_listener()
                with progress.consultation_display():
                    consultation_resolved = _prompt_for_one_consultation(
                        repo,
                        engine=engine,
                        governance_docs=governance_docs or cfg.project.governance_docs,
                        max_output_tokens=max_output_tokens,
                    )
                if consultation_resolved:
                    progress.start_control_listener()
                    continue
            if planning.next_phase == MeetingPhase.PAUSED:
                progress.stop_control_listener()
                print(json.dumps(planning.model_dump(mode="json"), indent=2, ensure_ascii=False))
                return planning
            break
        execution = LiteratureReportExecutionRunner(
            repo=repo,
            engine=engine,
            governance_docs=governance_docs or cfg.project.governance_docs,
            research_desk=research_desk,
            max_output_tokens=max_output_tokens,
        )
        from project_ensemble.orchestration.literature_writing_v071 import LiteratureWritingPaused
        while True:
            try:
                result = execution.run()
                break
            except LiteratureWritingPaused as exc:
                progress.stop_control_listener()
                progress.status(MeetingPhase.PAUSED, exc.reason_code)
                if not (show_progress and sys.stdin.isatty()):
                    print(json.dumps({"meeting_id": repo.meeting_id, "next_phase": "PAUSED",
                                      "paused_reason": exc.reason_code}, ensure_ascii=False))
                    return None
                with progress.consultation_display():
                    consultation_resolved = _prompt_for_one_consultation(
                        repo, engine=engine, governance_docs=governance_docs or cfg.project.governance_docs,
                        max_output_tokens=max_output_tokens,
                    )
                if not consultation_resolved:
                    return None
                progress.start_control_listener()
        progress.stop_control_listener()
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return result


def cmd_run_report(args) -> int:
    repo = MeetingRepository(args.meeting)
    if _has_frozen_readability_failure(repo):
        return _open_meeting(args, repo.root)
    cfg = _load_config(_config_path(args.config, repo))
    register_meeting(repo.root, cfg.source_path)
    if _uses_legacy_runtime(repo):
        return _resume_selected_meeting(args, repo, cfg)
    actual = _resume_kind(repo)
    if actual != "run-report":
        print(
            f"兼容映射：旧入口 run-report → {actual}；会议类型以冻结 manifest 为准。",
            file=sys.stderr,
        )
        return _resume_selected_meeting(args, repo, cfg)
    _run_literature_report(
        repo=repo,
        cfg=cfg,
        governance_docs=args.governance_docs,
        max_output_tokens=args.max_output_tokens,
        show_progress=not args.no_progress,
    )
    _offer_compaction_after_completion(repo, cfg.source_path)
    return 0


def _run_scholarly_rendering(
    *,
    repo,
    cfg,
    governance_docs=None,
    max_output_tokens=None,
    show_progress=True,
):
    governance_docs = _meeting_governance_docs(
        repo, governance_docs or cfg.project.governance_docs
    )
    _assert_meeting_runtime_compatible(
        repo, governance_docs=governance_docs
    )
    while True:
        try:
            return _run_scholarly_rendering_locked(
                repo=repo,
                cfg=cfg,
                governance_docs=governance_docs,
                max_output_tokens=max_output_tokens,
                show_progress=show_progress,
            )
        except ModelReplacementRequested:
            if not (show_progress and sys.stdin.isatty()):
                raise
            _interactive_model_replacement(repo=repo, cfg=cfg)


def _run_scholarly_rendering_locked(
    *,
    repo,
    cfg,
    governance_docs=None,
    max_output_tokens=None,
    show_progress=True,
):
    with repo.exclusive_run_lock():
        progress = ConsoleProgressReporter(meeting_root=repo.root) if show_progress else NullProgressReporter()
        adapters = _build_meeting_adapters(cfg, repo, require_keys=True)
        output_token_budgets = {}
        if max_output_tokens is None:
            output_token_budgets = _configured_output_token_budgets(
                repo=repo, cfg=cfg, adapters=adapters, progress=progress
            )
        engine = MeetingEngine(
            repo=repo,
            adapters=adapters,
            notifier=build_email_notifier(cfg.notifications.email),
            max_retries=cfg.governance.provider_retries,
            retry_base_delay_seconds=cfg.governance.provider_retry_base_delay_seconds,
            progress=progress,
            output_token_budgets=output_token_budgets,
            input_context_budgets=_configured_input_context_budgets(
                repo=repo, cfg=cfg, adapters=adapters, progress=progress
            ),
            configured_concurrency_limits=_meeting_concurrency_limits(repo, cfg),
        )
        _install_live_batch_controls(progress=progress, repo=repo, cfg=cfg, engine=engine)
        progress.start_control_listener()
        research_desk = ResearchDesk(
            repo=repo,
            engine=engine,
            retriever=_build_research_retriever(cfg, repo),
            freshness_windows=_research_freshness_windows(cfg),
            document_fetcher=_research_document_fetcher(cfg),
            max_output_tokens=max_output_tokens,
            retrieval_max_retries=cfg.governance.provider_retries,
            retrieval_retry_base_delay_seconds=(
                cfg.governance.provider_retry_base_delay_seconds
            ),
        )
        progress.live_research_desk = research_desk
        runner = ScholarlyRenderingRunner(
            repo=repo,
            engine=engine,
            research_desk=research_desk,
            max_output_tokens=max_output_tokens,
        )
        while True:
            result = runner.run()
            if result.status == "PAUSED":
                consultation_service = HumanConsultationService(repo)
                resolved_any = False
                if show_progress and sys.stdin.isatty():
                    progress.stop_control_listener()
                while consultation_service.open_issues():
                    issue = consultation_service.open_issues()[0]
                    delegated = (
                        issue.stage == "SCHOLARLY_SCIENCE_REVIEW"
                        and issue.reason_code == "SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED"
                        and ScienceConsultationAuthorityService(repo).current()[0] == "chair"
                    )
                    if delegated:
                        try:
                            resolution = consultation_service.ask_chair_to_decide_science_objection(
                                issue=issue, engine=engine, max_output_tokens=max_output_tokens,
                            )
                        except Exception as exc:
                            if show_progress:
                                progress.chair_ruling_notice(
                                    f"{issue.issue_id} 未完成，异议保持未决：{exc}", failed=True
                                )
                            else:
                                print(f"主席代裁未完成，异议保持未决：{exc}", file=sys.stderr)
                            repo.events.append(
                                "DELEGATED_CHAIR_CONSULTATION_FAILED",
                                {"meeting_id": repo.meeting_id, "issue_id": issue.issue_id,
                                 "error_type": type(exc).__name__, "error": str(exc)},
                                actor="orchestrator",
                            )
                            break
                        if resolution is not None:
                            if show_progress:
                                progress.chair_ruling_notice(
                                    f"{issue.issue_id}：{consultation_option_label(resolution.decision)}"
                                    f"；{resolution.rationale}"
                                )
                            else:
                                print(
                                    f"主席代裁 {issue.issue_id}：{consultation_option_label(resolution.decision)}"
                                    f"；{resolution.rationale}", file=sys.stderr,
                                )
                            resolved_any = True
                            continue
                    if not (show_progress and sys.stdin.isatty()):
                        break
                    with progress.consultation_display():
                        consultation_resolved = _prompt_for_one_consultation(
                            repo,
                            engine=engine,
                            governance_docs=governance_docs or cfg.project.governance_docs,
                            max_output_tokens=max_output_tokens,
                        )
                    if not consultation_resolved:
                        break
                    resolved_any = True
                if resolved_any and not consultation_service.open_issues():
                    progress.start_control_listener()
                    continue
            break
        progress.stop_control_listener()
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return result


def cmd_run_render(args) -> int:
    repo = MeetingRepository(args.meeting)
    cfg = _load_config(_config_path(args.config, repo))
    register_meeting(repo.root, cfg.source_path)
    if _uses_legacy_runtime(repo):
        return _resume_selected_meeting(args, repo, cfg)
    actual = _resume_kind(repo)
    if actual != "run-render":
        print(
            f"兼容映射：旧入口 run-render → {actual}；会议类型以冻结 manifest 为准。",
            file=sys.stderr,
        )
        return _resume_selected_meeting(args, repo, cfg)
    _run_scholarly_rendering(
        repo=repo,
        cfg=cfg,
        governance_docs=args.governance_docs,
        max_output_tokens=args.max_output_tokens,
        show_progress=not args.no_progress,
    )
    _offer_compaction_after_completion(repo, cfg.source_path)
    return 0


def cmd_reassemble_render(args) -> int:
    """Reissue only the presentation structure of a completed rendering."""
    selector = Path(args.meeting).expanduser()
    direct = next(
        (candidate.resolve() for candidate in (selector, Path.cwd() / selector)
         if (candidate / "public/meeting_manifest.json").is_file()),
        None,
    )
    if direct is None:
        cfg = _load_config(_config_path(args.config))
        direct = resolve_indexed_meeting(args.meeting, cfg.source_path)
    if direct is None:
        raise ValueError(f"没有找到会议 {args.meeting!r}；请提供会议 ID 或完整目录")
    repo = MeetingRepository(direct)
    with repo.exclusive_run_lock():
        edition = reissue_structured_scholarly_publication(repo)
    print(json.dumps(edition, indent=2, ensure_ascii=False))
    return 0


def cmd_run_research(args) -> int:
    repo = MeetingRepository(args.meeting)
    cfg = _load_config(_config_path(args.config, repo))
    register_meeting(repo.root, cfg.source_path)
    if _uses_legacy_runtime(repo):
        return _resume_selected_meeting(args, repo, cfg)
    actual = _resume_kind(repo)
    if actual != "run-research":
        print(
            f"兼容映射：旧入口 run-research → {actual}；会议类型以冻结 manifest 为准。",
            file=sys.stderr,
        )
        return _resume_selected_meeting(args, repo, cfg)
    _run_research_only(repo=repo, cfg=cfg)
    _offer_compaction_after_completion(repo, cfg.source_path)
    return 0


def cmd_research_claim(args) -> int:
    repo = MeetingRepository(args.meeting)
    cfg = _load_config(_config_path(args.config, repo))
    progress = ConsoleProgressReporter(meeting_root=repo.root)
    with repo.exclusive_run_lock():
        incomplete_rounds = ResearchRoundRunner.incomplete_round_ids(repo)
        if incomplete_rounds:
            raise ValueError(
                "cannot publish a manual research claim while a formal Research Round is "
                f"incomplete ({', '.join(incomplete_rounds)}); resume the meeting with "
                "ensemble run-general first"
            )
        adapters = _build_meeting_adapters(cfg, repo, require_keys=True)
        output_token_budgets = _configured_output_token_budgets(
            repo=repo, cfg=cfg, adapters=adapters, progress=progress
        )
        input_context_budgets = _configured_input_context_budgets(
            repo=repo, cfg=cfg, adapters=adapters, progress=progress
        )
        engine = MeetingEngine(
            repo=repo,
            adapters=adapters,
            notifier=build_email_notifier(cfg.notifications.email),
            max_retries=cfg.governance.provider_retries,
            retry_base_delay_seconds=cfg.governance.provider_retry_base_delay_seconds,
            progress=progress,
            output_token_budgets=output_token_budgets,
            input_context_budgets=input_context_budgets,
            configured_concurrency_limits=_meeting_concurrency_limits(repo, cfg),
        )
        retriever = _build_research_retriever(cfg, repo)
        try:
            packet = ResearchDesk(
                repo=repo,
                engine=engine,
                retriever=retriever,
                freshness_windows=_research_freshness_windows(cfg),
                document_fetcher=_research_document_fetcher(cfg),
                retrieval_max_retries=cfg.governance.provider_retries,
                retrieval_retry_base_delay_seconds=(
                    cfg.governance.provider_retry_base_delay_seconds
                ),
            ).research(
                ResearchRequest(
                    requester_id=args.requester,
                    stage=ResearchStage(args.stage),
                    claim=args.claim,
                    force_refresh=args.force_refresh,
                    refresh_reason=args.refresh_reason,
                )
            )
        except ResearchQualityControlError as exc:
            result = {
                "meeting_id": repo.meeting_id,
                "status": "QC_FAILED",
                "packet_id": None,
                "knowledge_status": None,
                "failure_code": exc.code,
                "failure_summary": exc.summary,
                "disposition": "NO_EVIDENCE_UPGRADE_NONBLOCKING",
            }
            repo.events.append(
                "MANUAL_RESEARCH_CLAIM_QC_FAILED_NONBLOCKING",
                result,
                actor="RESEARCH_DESK",
            )
        else:
            result = packet.model_dump(mode="json")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_invalidate_research_packet(args) -> int:
    repo = MeetingRepository(args.meeting)
    with repo.exclusive_run_lock():
        record = ResearchPacketCache(repo).invalidate(
            packet_id=args.packet,
            authority=CacheInvalidationAuthority.HUMAN,
            reason=CacheInvalidationReason(args.reason),
            rationale=args.rationale,
            evidence_url=args.evidence_url,
        )
    print(record.model_dump_json(indent=2))
    return 0


def cmd_replace_model(args) -> int:
    """Record a future-only model handoff for one meeting participant."""

    repo = MeetingRepository(args.meeting)
    cfg = _load_config(_config_path(args.config, repo))
    provider_id, model_id = args.model
    provider = cfg.providers.get(provider_id)
    if provider is None:
        raise ValueError(f"provider {provider_id!r} is not configured in the meeting configuration")
    if provider.selectable_models is not None and model_id not in provider.selectable_models:
        raise ValueError(
            f"model {provider_id}:{model_id} is not in that provider's configured selectable_models"
        )
    with repo.exclusive_run_lock():
        service = ModelReplacementService(repo)
        if args.participant is not None:
            records = [
                service.replace(
                    participant_id=args.participant,
                    provider_id=provider_id,
                    model_id=model_id,
                    reason=args.reason,
                )
            ]
        else:
            from_provider_id, from_model_id = args.from_model
            records = service.replace_all_using(
                from_provider_id=from_provider_id,
                from_model_id=from_model_id,
                provider_id=provider_id,
                model_id=model_id,
                reason=args.reason,
            )
    print(json.dumps([record.model_dump(mode="json") for record in records], indent=2, ensure_ascii=False))
    print(
        "已记录模型替换；该替换只对后续调用生效，既有响应、票据和审计记录保持不变。",
        file=sys.stderr,
    )
    return 0


def _interactive_openalex_daily_fallback(*, error, tavily_available: bool) -> bool:
    """Ask once after a verified daily quota event, without stopping workers."""
    output = sys.stderr
    print(_ui("\n┌─ OpenAlex 当日额度不足 ──────────────────────────────────────",
              "\n┌─ OpenAlex daily credits insufficient ───────────────────────"), file=output)
    print(_ui(f"│ 已通过 OpenAlex 官方余额接口核实：{str(error)[:350]}",
              f"│ Confirmed using OpenAlex's official rate-limit endpoint: {str(error)[:350]}"), file=output)
    print(_ui("│ 其他无需新检索的任务继续在后台执行；已落盘结果不会重做。",
              "│ Other work that needs no new search continues; saved results remain intact."), file=output)
    if tavily_available:
        print(_ui("│ 1. 授权 Tavily 接手未完成检索；保留降级标记，并尝试 OpenAlex 补检",
                  "│ 1. Let Tavily handle pending searches; mark gaps and retry OpenAlex"), file=output)
    else:
        print(_ui("│ 备用搜索 API 未配置；可先在设置中启用 Tavily。",
                  "│ No backup search API configured; enable Tavily in Settings first."), file=output)
    print(_ui("│ 2. 不使用备用搜索；完成非检索工作后暂停会议",
              "│ 2. Do not use a backup; pause after non-search work finishes"), file=output)
    print("└──────────────────────────────────────────────────────────", file=output)
    if not tavily_available:
        return False
    while True:
        answer = terminal_input(_ui("选择 1–2（回车选择 2）: ",
                                    "Choose 1–2 (Enter chooses 2): ")).strip()
        if answer == "1":
            return True
        if answer in {"", "2", "b", "q"}:
            return False
        print(_ui("请输入 1 或 2。", "Enter 1 or 2."), file=output)


def _install_live_batch_controls(*, progress, repo, cfg, engine) -> None:
    """Let Representative lanes apply Ctrl+R at the next unstarted call."""
    if not sys.stdin.isatty() or isinstance(progress, NullProgressReporter):
        return

    def runtime_key(record):
        runtime = record.get("runtime") or {}
        fallback = (str(runtime.get("provider_id") or ""),
                    str(runtime.get("model_id") or ""))
        participant = record.get("representative_id")
        if not participant:
            return fallback
        try:
            return current_runtime_for(repo, str(participant))
        except ValueError:
            return fallback

    def apply_changes() -> None:
        changes = _interactive_model_replacement(
            repo=repo, cfg=cfg, deferred=True,
        ) or []
        if any(item.get("kind") == "force_stop" for item in changes):
            progress.force_stop_active_calls()
            return
        for item in changes:
            if item.get("kind") == "runtime_control":
                from project_ensemble.runtime.run_controls import record_run_control
                record_run_control(
                    repo, kind=item["control_kind"], target=item["target"],
                    value=item["value"], reason=item["reason"],
                )
                if item["control_kind"] == "research_parallelism":
                    fast_runner = getattr(progress, "live_fast_runner", None)
                    if fast_runner is not None:
                        fast_runner.research_max_concurrent_claim_groups = int(item["value"])
            else:
                target = (item["provider_id"], item["model_id"])
                if item["provider_id"] not in engine.adapters:
                    engine.adapters[item["provider_id"]] = _load_adapter_for_provider(
                        cfg, item["provider_id"]
                    )
                if current_runtime_for(repo, item["participant_id"]) != target:
                    ModelReplacementService(repo).replace(**item)
        if changes:
            engine.configured_concurrency_limits = _meeting_concurrency_limits(repo, cfg)
            engine.input_context_budgets = _configured_input_context_budgets(
                repo=repo, cfg=cfg, adapters=engine.adapters,
            )
            if getattr(engine, "max_output_tokens", None) is None:
                engine.output_token_budgets = _configured_output_token_budgets(
                    repo=repo, cfg=cfg, adapters=engine.adapters,
                )
            desk = getattr(progress, "live_research_desk", None)
            if desk is not None:
                desk.update_model_concurrency_limit(
                    engine.participant_concurrency_limit("RESEARCH_DESK")
                )
                if any(item.get("control_kind") == "openalex_quota_policy"
                       for item in changes):
                    from project_ensemble.research.source_reading import SourceReader
                    retriever = _build_research_retriever(cfg, repo)
                    previous_reader = desk.source_reader
                    desk.retriever = retriever
                    desk.source_reader = SourceReader(
                        repo=repo, retriever=retriever,
                        document_fetcher=previous_reader.fetcher,
                        max_sources=previous_reader.max_sources,
                    )
            progress.info(
                "新设置已用于尚未启动的代表调用；在途调用不取消，缩小的并行上限随槽位释放生效"
            )

    progress.live_runtime_key = runtime_key
    progress.live_batch_control_callback = apply_changes
    progress.immediate_control_callback = apply_changes


def _interactive_research_desk_fallback(
    *, repo: MeetingRepository, cfg, error, run_lock_held: bool = False
) -> bool:
    """Retry one missing question or select a future-only Research Desk model."""

    from project_ensemble.orchestration.literature_fast import (
        DEFAULT_FAST_SEARCH_REPAIR_DIRECTION, FastResearchDeskFailure,
        request_fast_research_rollback,
    )
    from project_ensemble.runtime.research_fallbacks import ResearchFallbacks

    output = sys.stderr
    failed_question = isinstance(error, FastResearchDeskFailure)
    cause = error.cause if failed_question else error
    search_failure = failed_question and (
        isinstance(cause, OpenAlexQueryRejected)
        or str(cause).startswith("OpenAlex retrieval failed:")
        or str(cause).startswith("OpenAlex search HTTP 400")
    )
    openalex_500 = search_failure and "OpenAlex retrieval failed: HTTP 500" in str(cause)
    openalex_400 = search_failure and (
        isinstance(cause, OpenAlexQueryRejected) or "HTTP 400" in str(cause)
    )
    def manual_rollback() -> bool:
        if not failed_question:
            return False
        if openalex_400:
            print(_ui(
                "建议修复方向：保留原问题和四类证据方向；缩短检索式，优先使用核心主题词、短语及明确的 OR 词形变体；"
                "避免复杂嵌套布尔结构，默认不用 * 或 ? 通配符，仅在必要且后端支持时使用。",
                "Suggested repair: preserve the claim and all four evidence directions; shorten queries to core topical terms,"
                " phrases, and explicit OR variants; avoid nested Boolean logic and do not use * or ? wildcards by default."
                " Use them only when necessary and supported by the backend.",
            ), file=output)
            repair_choice = terminal_input(_ui(
                "回车采用这条预设；输入 c 自定义排障方向；b 返回: ",
                "Enter accepts this preset; c lets you customize it; b returns: ",
            )).strip().lower()
            if repair_choice in {"", "1"}:
                direction = DEFAULT_FAST_SEARCH_REPAIR_DIRECTION
            elif repair_choice == "c":
                direction = terminal_input(_ui(
                    "输入补充或替代方向（回车取消）: ",
                    "Enter an alternative direction (Enter cancels): ",
                )).strip()
            else:
                return False
        else:
            direction = terminal_input(_ui(
                "给 Research Desk 一句排障方向，用于重拟本题四类检索式（回车取消）: ",
                "Give Research Desk one direction for replanning this item's four searches (Enter cancels): ",
            )).strip()
        if not direction:
            return False
        from contextlib import nullcontext
        with (nullcontext() if run_lock_held else repo.exclusive_run_lock()):
            path = request_fast_research_rollback(repo, error.request_id, direction)
        print(_ui(f"已记录手动回退：{path}；原查询与已完成证据不删除。",
                  f"Manual rollback recorded: {path}; prior queries and completed evidence remain."), file=output)
        return True
    current = current_runtime_for(repo, "RESEARCH_DESK")
    print(_ui("\n┌─ Research Desk 单项核查需要人类处理 ─────────────────────────", "\n┌─ Research Desk check needs Human action ───────────────────────"), file=output)
    if failed_question:
        print(
            _ui(f"│ 未完成：{error.module_id} · 第 {error.round_number} 轮 · 问题 {error.index}", f"│ Unfinished: {error.module_id} · round {error.round_number} · question {error.index}"),
            file=output,
        )
    if search_failure:
        print(_ui("│ 故障位置：OpenAlex 检索后端；目前没有证据表明模型调用失败。",
                  "│ Failure location: OpenAlex search; no model-call failure is indicated."), file=output)
    else:
        print(_ui(f"│ 当前模型：{current[0]}:{current[1]}", f"│ Current model: {current[0]}:{current[1]}"), file=output)
    if isinstance(cause, ProviderContentRejectedError):
        print(_ui(f"│ 供应商返回：HTTP {cause.status_code} · Content Exists Risk", f"│ Provider response: HTTP {cause.status_code} · Content Exists Risk"), file=output)
        if cause.provider_request_id:
            print(_ui(f"│ 供应商请求 ID：{cause.provider_request_id}", f"│ Provider request ID: {cause.provider_request_id}"), file=output)
    else:
        print(_ui(f"│ 失败类型：{type(cause).__name__}；{str(cause)[:200]}", f"│ Failure type: {type(cause).__name__}; {str(cause)[:200]}"), file=output)
    print(_ui("│ 已完成的核查不会重做；未完成项没有证据结论。", "│ Completed checks are preserved; unfinished items have no evidence conclusion."), file=output)
    if search_failure:
        if openalex_500:
            print(_ui("│ HTTP 500 是服务端响应；可能与检索式有关，也可能是服务故障，不能直接归因。",
                      "│ HTTP 500 is a server response; a query trigger is possible but unconfirmed."), file=output)
            print(_ui("│ 重复失败后，已配置的 Technician 会只修订本题四类检索式；原问题和原查询保留。",
                      "│ After repeated failures, the configured Technician revises only this item's four searches."), file=output)
        else:
            print(_ui("│ HTTP 400 会先按保留主题词的简化查询重试；不会因此更换 Research Desk 模型。",
                      "│ HTTP 400 first retries with simpler topical terms; the Research Desk model stays unchanged."), file=output)
            if openalex_400:
                print(_ui("│ 若需手动回退，系统提供预设修复方向：精简查询、保留主题词、默认避免通配符。",
                          "│ Manual rollback offers a preset: shorten queries, retain topic terms, and avoid wildcards by default."), file=output)
        if openalex_500:
            print(_ui("│ 学术命题优先 OpenAlex；不会因本次 HTTP 500 自动改用 Tavily。",
                      "│ Academic claims prefer OpenAlex; this HTTP 500 does not trigger Tavily."), file=output)
        print(_ui("│ 1. 只重新提交未落盘的检索；优先沿用已落盘的修订检索式",
                  "│ 1. Retry unfinished item using any saved revised search plan"), file=output)
        print(_ui("│ 2. 保持暂停，稍后处理", "│ 2. Stay paused and decide later"), file=output)
        print(_ui("│ r. 手动回退到本题检索式拟定，并给出排障方向",
                  "│ r. Roll back this item's search plan with a Human direction"), file=output)
        print("└──────────────────────────────────────────────────────────", file=output)
        while True:
            choice = terminal_input(_ui("选择编号（1-2；回车保持暂停）: ",
                                        "Choose 1–2 (Enter to stay paused): ")).strip()
            if choice.lower() == "r":
                if manual_rollback():
                    return True
                continue
            if choice in {"", "2", "b", "q"}:
                return False
            if choice == "1":
                return True
            print(_ui("请输入 1 或 2。", "Enter 1 or 2."), file=output)
    if run_lock_held:
        print(_ui("│ 其他独立核查仍在后台继续；你的选择将在当前批次结束后用于未落盘项。", "│ Other independent checks continue; your choice applies to unsaved items after this batch."), file=output)
    print(_ui("│ 主模型保持不变；备用模型仅按你选择的故障范围启用，并留下审计记录。", "│ The primary model stays unchanged; the backup is used only for the selected failure scope and is audited."), file=output)
    if failed_question:
        print(_ui("│ 1. 用当前模型重试未落盘项", "│ 1. Retry the unsaved item with the current model"), file=output)
        print(_ui("│ 2. 仅此问题改用备用模型", "│ 2. Use a backup model for this question only"), file=output)
        print(_ui("│ 3. 本会议后续同模型故障时也使用该备用模型", "│ 3. Also use this backup for later failures of the same model"), file=output)
        print(_ui("│ 4. 保持暂停，稍后处理", "│ 4. Stay paused and decide later"), file=output)
        print(_ui("│ r. 手动回退到本题检索式拟定，并给出排障方向",
                  "│ r. Roll back this item's search plan with a Human direction"), file=output)
    else:
        print(_ui("│ 1. 仅下一次主模型故障改用备用模型", "│ 1. Use a backup for the next primary-model failure only"), file=output)
        print(_ui("│ 2. 本会议此后主模型故障时均用该备用模型", "│ 2. Use the backup whenever the primary model later fails"), file=output)
        print(_ui("│ 3. 保持暂停，稍后处理", "│ 3. Stay paused and decide later"), file=output)
    print("└──────────────────────────────────────────────────────────", file=output)
    while True:
        choice = terminal_input(
            _ui("选择编号（1-4；回车保持暂停）: ", "Choose 1–4 (Enter to stay paused): ") if failed_question
            else _ui("选择编号（1-3；回车保持暂停）: ", "Choose 1–3 (Enter to stay paused): ")
        ).strip()
        if failed_question and choice.lower() == "r":
            if manual_rollback():
                return True
            continue
        if choice in ({"", "4", "b", "q"} if failed_question else {"", "3", "b", "q"}):
            return False
        if failed_question and choice == "1":
            print(_ui("将只重试未落盘的问题；其余证据包保持不变。", "Only unsaved questions will be retried; other evidence packets stay unchanged."), file=output)
            return True
        if choice in ({"2", "3"} if failed_question else {"1", "2"}):
            break
        print(_ui("请输入有效编号。", "Enter a valid number."), file=output)

    provider_ids = list(cfg.providers)
    try:
        catalogs = []
        for provider_id in provider_ids:
            try:
                catalogs.extend(discover_models(cfg, [provider_id]))
            except Exception as exc:
                print(_ui(f"供应商 {provider_id} 暂不可用：{exc}", f"Provider {provider_id} is unavailable: {exc}"), file=output)
        catalog = catalogs
    except Exception as exc:
        print(_ui(f"模型列表查询失败：{exc}。会议保持暂停，可稍后用 replace-model 指定。", f"Model discovery failed: {exc}. Meeting stays paused; use replace-model later."), file=output)
        return False
    available = [
        model for model in dict.fromkeys(
            (item.provider_id, item.model_id) for item in catalog
        ) if model != current
    ]
    if not available:
        print(_ui("没有可选的备用模型；会议保持暂停。", "No backup model is available; meeting stays paused."), file=output)
        return False
    while True:
        print(_ui("\n可用的备用模型：", "\nAvailable backup models:"), file=output)
        for index, (provider_id, model_id) in enumerate(available, start=1):
            print(f"  {index}. {provider_id}:{model_id}", file=output)
        selected = _replacement_menu_choice(_ui("选择备用模型", "Choose a backup model"), len(available), output=output)
        if selected is None:
            return False
        provider_id, model_id = available[selected]
        confirmation = terminal_input(
            _ui(f"确认主模型保持不变，按所选故障范围启用 {provider_id}:{model_id}？[y/N] ", f"Keep the primary model and use {provider_id}:{model_id} only within the selected failure scope? [y/N] ")
        ).strip().lower()
        if confirmation not in {"y", "yes", "是"}:
            continue
        reason = (
            "Human selected a Research Desk backup after provider content rejection"
            if isinstance(cause, ProviderContentRejectedError)
            else f"Human selected a Research Desk backup after {type(cause).__name__}"
        ) + (
            f"; provider request ID {cause.provider_request_id}"
            if isinstance(cause, ProviderContentRejectedError) and cause.provider_request_id else ""
        )
        from contextlib import nullcontext
        with (nullcontext() if run_lock_held else repo.exclusive_run_lock()):
            ResearchFallbacks(repo).choose(
                request_id=(error.request_id if failed_question else
                            f"PROVIDER-FAILURE-{getattr(cause, 'provider_request_id', None) or 'UNKNOWN'}"),
                source=current,
                target=(provider_id, model_id),
                scope=(("REQUEST_ONLY" if choice == "2" else "ON_FUTURE_FAILURES")
                       if failed_question else
                       ("NEXT_FAILURE_ONLY" if choice == "1" else "ON_FUTURE_FAILURES")),
                reason=reason,
            )
        print(_ui("已记录备用模型；未完成问题将在安全边界重新核查。", "Backup recorded; unfinished questions will be rechecked at the next safe boundary."), file=output)
        return True


def _replacement_menu_choice(prompt: str, count: int, *, output) -> int | None:
    """Choose a one-based row, return to the prior menu, or safely stop."""

    while True:
        raw = terminal_input(_ui(f"{prompt}（1-{count}；b 返回；q 安全退出）: ", f"{prompt} (1–{count}; b back; q safe exit): ")).strip().lower()
        if raw in {"b", "返回", "back", ""}:
            return None
        if raw in {"q", "退出", "quit"}:
            raise KeyboardInterrupt
        if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw) - 1
        print(_ui(f"请输入 1-{count}、b 或 q。", f"Enter 1–{count}, b, or q."), file=output)


def _interactive_run_control(*, repo: MeetingRepository, cfg, mode: int,
                             deferred: bool, output) -> list[dict] | None:
    """Collect one future-call control change without editing the frozen manifest."""
    from project_ensemble.runtime.run_controls import (
        effective_openalex_quota_policy, effective_reasoning_effort,
        effective_research_parallelism, record_run_control,
    )

    manifest = json.loads((repo.root / "identity_private/meeting_manifest.json").read_text(encoding="utf-8"))
    kind: str
    target: str | None
    value: int | str
    if mode == 3:
        if not manifest.get("research_model"):
            print(_ui("本会议未启用 Research Desk。", "Research Desk is disabled in this meeting."), file=output)
            return None
        current = effective_research_parallelism(
            repo, manifest.get("research_max_concurrent_claim_groups")
        ) or 1
        print(_ui(f"Research Desk 独立问题并行度当前为 {current} 组（调度上限，不等于模型同时在途上限）。",
                  f"Research Desk independent-task parallelism is {current} groups (a scheduler cap, not the model-call cap)."), file=output)
        raw = terminal_input(_ui("新的独立问题并行度（1–32；b 返回；q 安全退出）: ",
                                 "New independent-task parallelism (1–32; b back; q exit): ")).strip().lower()
        if raw in {"b", "back", "返回", ""}:
            return None
        if raw in {"q", "quit", "退出"}:
            raise KeyboardInterrupt
        if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 32:
            print(_ui("请输入 1–32 的整数；设置未改变。", "Enter an integer from 1 to 32; nothing changed."), file=output)
            return None
        kind, target, value = "research_parallelism", None, int(raw)
    elif mode == 4:
        models = sorted({current_runtime_for(repo, participant)
                         for participant in meeting_participant_ids(repo)})
        if not models:
            print(_ui("没有可设置的当前模型。", "No active model is available."), file=output)
            return None
        limits = _meeting_concurrency_limits(repo, cfg)
        print(_ui("\n当前模型同时在途调用上限：", "\nCurrent simultaneous model-call limits:"), file=output)
        for index, (provider, model) in enumerate(models, 1):
            limit = limits.get((provider, model), limits.get((provider, "*"), 1))
            print(f"  {index}. {provider}:{model} · {limit}", file=output)
        selected = _replacement_menu_choice(_ui("选择模型", "Choose model"), len(models), output=output)
        if selected is None:
            return None
        provider, model = models[selected]
        raw = terminal_input(_ui("新的模型同时在途调用上限（1–16；b 返回；q 安全退出）: ",
                                 "New simultaneous-call cap (1–16; b back; q exit): ")).strip().lower()
        if raw in {"b", "back", "返回", ""}:
            return None
        if raw in {"q", "quit", "退出"}:
            raise KeyboardInterrupt
        if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 16:
            print(_ui("请输入 1–16 的整数；设置未改变。", "Enter an integer from 1 to 16; nothing changed."), file=output)
            return None
        kind, target, value = "model_concurrency", f"{provider}:{model}", int(raw)
        print(_ui("提醒：增大同时调用量可能触发供应商限流；超出的请求仍须遵守供应商实际限制。",
                  "Warning: more simultaneous calls may trigger provider rate limits; provider limits still apply."), file=output)
    elif mode == 6:
        if not manifest.get("research_model"):
            print(_ui("本会议未启用 Research Desk。", "Research Desk is disabled in this meeting."), file=output)
            return None
        frozen = manifest.get("openalex_quota_policy") or "wait"
        current = effective_openalex_quota_policy(repo, frozen)
        labels = {
            "legacy_parallel": _ui("旧会并行检索", "legacy parallel search"),
            "wait": _ui("确认当日耗尽时由人类裁定", "ask Human on confirmed daily exhaustion"),
            "tavily": _ui("限流时用 Tavily 补读，并尝试 OpenAlex 补检",
                          "read with Tavily during rate limits, then retry OpenAlex"),
        }
        print(_ui(f"当前 OpenAlex 额度策略：{labels.get(current, current)}。",
                  f"Current OpenAlex quota policy: {labels.get(current, current)}."), file=output)
        print(_ui("  1. 确认当日额度耗尽时询问是否用备用搜索；未知 429 短时退避重试（默认）",
                  "  1. Ask whether to use backup search on daily exhaustion; retry unknown 429s (default)"), file=output)
        tavily_enabled = bool(cfg.research.tavily.enabled)
        if tavily_enabled:
            print(_ui("  2. 限流时先用 Tavily 补读原文，再尝试 OpenAlex 补检；未补成则标记覆盖不足（默认）",
                      "  2. Read originals with Tavily, then retry OpenAlex; flag gaps if still unavailable (default)"), file=output)
        else:
            print(_ui("  Tavily 未启用；先在设置中配置，才可选择方案 2。",
                      "  Tavily is disabled; configure it in Settings before choosing option 2."), file=output)
        selected = _replacement_menu_choice(_ui("选择额度策略", "Choose quota policy"),
                                             2 if tavily_enabled else 1, output=output)
        if selected is None:
            return None
        kind, target, value = "openalex_quota_policy", None, "tavily" if selected == 1 else "wait"
        if value == current:
            print(_ui("额度策略未改变。", "Quota policy is unchanged."), file=output)
            return None
    else:
        participants = meeting_participant_ids(repo)
        print(_ui("\n选择要调整推理强度的参与者：", "\nChoose a participant's reasoning effort:"), file=output)
        for index, participant in enumerate(participants, 1):
            if participant == "CHAIR":
                frozen = manifest.get("chair_reasoning_effort", "default")
            elif participant == "RESEARCH_DESK":
                frozen = manifest.get("research_reasoning_effort") or "default"
            elif participant == "WRITER":
                frozen = manifest.get("writer_reasoning_effort") or "default"
            elif participant == "TECHNICIAN":
                frozen = manifest.get("technician_reasoning_effort") or "default"
            else:
                registry = repo.root / "identity_private/representative_registry.json"
                members = json.loads(registry.read_text(encoding="utf-8")) if registry.is_file() else []
                record = next((item for item in members
                               if item.get("representative_id") == participant), {})
                frozen = record.get("runtime", {}).get("reasoning_setting") or "default"
            current = effective_reasoning_effort(repo, participant, ReasoningEffort(frozen)).value
            print(f"  {index}. {participant} · {current}", file=output)
        selected = _replacement_menu_choice(_ui("选择参与者", "Choose participant"), len(participants), output=output)
        if selected is None:
            return None
        target = participants[selected]
        efforts = ["default", "low", "medium", "high"]
        for index, effort in enumerate(efforts, 1):
            print(f"  {index}. {effort}", file=output)
        selected_effort = _replacement_menu_choice(_ui("选择推理强度", "Choose reasoning effort"), len(efforts), output=output)
        if selected_effort is None:
            return None
        kind, value = "reasoning_effort", efforts[selected_effort]
        print(_ui("供应商不支持所选档位时，仍按该供应商的兼容映射发送合法值。",
                  "Unsupported effort levels still use the provider's compatible mapping."), file=output)
    reason = terminal_input(_ui("调整原因（必填；b 返回；q 安全退出）: ",
                                "Reason for change (required; b back; q exit): ")).strip()
    if reason.lower() in {"q", "quit", "退出"}:
        raise KeyboardInterrupt
    if reason.lower() in {"b", "back", "返回", ""}:
        print(_ui("未保存设置。", "Setting was not saved."), file=output)
        return None
    change = {"kind": "runtime_control", "control_kind": kind,
              "target": target, "value": value, "reason": reason}
    if deferred:
        print(_ui("设置已提交；菜单关闭后对尚未启动的子任务生效，在途请求不取消。",
                  "Change submitted; it applies to unstarted tasks after this menu; active calls continue."), file=output)
        return [change]
    with repo.exclusive_run_lock():
        record_run_control(repo, kind=kind, target=target, value=value, reason=reason)
    print(_ui("设置已记录；冻结初始配置未改写，后续调用使用新值。",
              "Change recorded; frozen initialization stays intact and future calls use the new value."), file=output)
    return []


def _interactive_model_replacement(
    *, repo: MeetingRepository, cfg, deferred: bool = False,
    allow_force_stop: bool = True,
) -> list[dict] | None:
    """Choose a runtime now; a batch scheduler commits it at its safe boundary."""

    output = sys.stderr
    while True:
        print(_ui("\n┌─ 模型与运行参数 ─────────────────────────────────────────", "\n┌─ Models and runtime controls ─────────────────────────────"), file=output)
        print(_ui("│ 1. 替换某个代表（也可选择 CHAIR / RESEARCH_DESK）", "│ 1. Replace one participant (including CHAIR / RESEARCH_DESK)"), file=output)
        print(_ui("│ 2. 替换某个模型的全部当前使用者", "│ 2. Replace all current users of one model"), file=output)
        print(_ui("│ 3. 调整 Research Desk 独立问题并行度", "│ 3. Change Research Desk independent-task parallelism"), file=output)
        print(_ui("│ 4. 调整某个模型的同时在途调用上限", "│ 4. Change one model's simultaneous-call cap"), file=output)
        print(_ui("│ 5. 调整某个参与者的推理强度", "│ 5. Change one participant's reasoning effort"), file=output)
        print(_ui("│ 6. 调整 OpenAlex 额度耗尽时是否改用 Tavily", "│ 6. Choose whether Tavily replaces OpenAlex after quota exhaustion"), file=output)
        if deferred and allow_force_stop:
            print(_ui("│ 7. 强制中止当前在途模型调用；保留已落盘进度，随后选择替代模型",
                      "│ 7. Force-stop active calls; keep saved work, then choose a replacement"), file=output)
        print(_ui("│ b. 取消更换，继续会议   q. 安全退出会议", "│ b. Cancel and continue   q. Safely exit meeting"), file=output)
        print("└──────────────────────────────────────────────────────────", file=output)
        mode_index = _replacement_menu_choice(
            _ui("选择操作", "Choose action"),
            7 if deferred and allow_force_stop else 6,
            output=output,
        )
        if mode_index is None:
            return [] if deferred else None
        if deferred and mode_index == 6:
            return [{"kind": "force_stop"}]
        mode = str(mode_index + 1)
        if mode_index >= 2:
            changed = _interactive_run_control(
                repo=repo, cfg=cfg, mode=mode_index + 1,
                deferred=deferred, output=output,
            )
            if changed is None:
                continue
            return changed
        while True:
            participants = meeting_participant_ids(repo)
            participant_id: str | None = None
            if mode == "1":
                print(_ui("\n可替换的参与者：", "\nReplaceable participants:"), file=output)
                for index, candidate in enumerate(participants, start=1):
                    provider_id, model_id = current_runtime_for(repo, candidate)
                    print(f"  {index}. {candidate} · {provider_id}:{model_id}", file=output)
                source_index = _replacement_menu_choice(_ui("选择参与者", "Choose participant"), len(participants), output=output)
                if source_index is None:
                    break
                participant_id = participants[source_index]
                source_model = current_runtime_for(repo, participant_id)
            else:
                counts: dict[tuple[str, str], int] = {}
                for candidate in participants:
                    runtime = current_runtime_for(repo, candidate)
                    counts[runtime] = counts.get(runtime, 0) + 1
                models = sorted(counts)
                print(_ui("\n当前使用中的模型：", "\nModels currently in use:"), file=output)
                for index, model in enumerate(models, start=1):
                    print(_ui(f"  {index}. {model[0]}:{model[1]} · {counts[model]} 名参与者", f"  {index}. {model[0]}:{model[1]} · {counts[model]} participants"), file=output)
                source_index = _replacement_menu_choice(_ui("选择当前模型", "Choose current model"), len(models), output=output)
                if source_index is None:
                    break
                source_model = models[source_index]
            source_label = f"{source_model[0]}:{source_model[1]}"
            provider_ids = list(cfg.providers)
            if not provider_ids:
                print(_ui("没有已配置的供应商；请先在设置中添加供应商。", "No providers are configured; add one in Settings first."), file=output)
                continue
            print(_ui("\n可用于本次切换的已配置供应商：", "\nConfigured providers available for this replacement:"), file=output)
            for index, candidate_provider in enumerate(provider_ids, start=1):
                provider_cfg = cfg.providers[candidate_provider]
                name = provider_cfg.display_name or candidate_provider
                print(f"  {index}. {name} [{candidate_provider}]", file=output)
            provider_index = _replacement_menu_choice(
                _ui("选择目标供应商", "Choose target provider"), len(provider_ids), output=output
            )
            if provider_index is None:
                continue
            target_provider = provider_ids[provider_index]
            if target_provider != source_model[0]:
                confirmed = terminal_input(_ui(
                    f"将把后续任务内容发送给外部供应商 {target_provider}。确认使用该供应商？[y/N] ",
                    f"Future task content will be sent to external provider {target_provider}. Confirm? [y/N] ",
                )).strip().lower()
                if confirmed not in {"y", "yes", "是"}:
                    continue
            print(_ui(f"\n正在从 {target_provider} 实时发现模型……", f"\nDiscovering models from {target_provider}…"), file=output, flush=True)
            try:
                catalog = discover_models(cfg, [target_provider])
            except Exception as exc:
                print(_ui(f"模型列表查询失败：{exc}；可返回重试或选择其他供应商。", f"Model discovery failed: {exc}; retry or choose another provider."), file=output)
                continue
            available = list(dict.fromkeys(
                (descriptor.provider_id, descriptor.model_id) for descriptor in catalog
            ))
            available = [model for model in available if model != source_model]
            if not available:
                print(_ui("该供应商没有可用的替代模型；请选择其他供应商。", "This provider has no available replacement model; choose another."), file=output)
                continue
            while True:
                print(_ui(f"\n替换对象当前为 {source_label}；可用目标模型：", f"\nCurrent model: {source_label}; available targets:"), file=output)
                for index, model in enumerate(available, start=1):
                    marker = _ui("（当前）", " (current)") if model == source_model else ""
                    print(f"  {index}. {model[0]}:{model[1]}{marker}", file=output)
                target_index = _replacement_menu_choice(
                    _ui("选择目标模型", "Choose target model"), len(available), output=output
                )
                if target_index is None:
                    break
                target_provider, target_model = available[target_index]
                if (target_provider, target_model) == source_model:
                    print(_ui("目标模型与当前模型相同，请选择另一个模型。", "Target is the current model; choose another."), file=output)
                    continue
                reason = terminal_input(_ui("替换原因（必填；b 返回；q 安全退出）: ", "Replacement reason (required; b back; q safe exit): ")).strip()
                if reason.lower() in {"q", "退出", "quit"}:
                    raise KeyboardInterrupt
                if reason.lower() in {"b", "返回", "back", ""}:
                    if not reason:
                        print(_ui("替换原因不能为空；输入 b 返回上一级。", "Reason cannot be empty; enter b to go back."), file=output)
                        continue
                    continue
                if deferred:
                    targets = (
                        [participant_id]
                        if mode == "1" else [
                            candidate for candidate in meeting_participant_ids(repo)
                            if current_runtime_for(repo, candidate) == source_model
                        ]
                    )
                    print(
                        _ui(f"已提交 {len(targets)} 个模型替换；在途请求保持原模型，菜单关闭后未启动的子任务使用新模型。",
                            f"Submitted {len(targets)} replacements. Active calls keep their models; unstarted tasks use the new model after this menu."),
                        file=output,
                    )
                    return [
                        {"participant_id": candidate, "provider_id": target_provider,
                         "model_id": target_model, "reason": reason}
                        for candidate in targets
                    ]
                with repo.exclusive_run_lock():
                    service = ModelReplacementService(repo)
                    if mode == "1":
                        records = [service.replace(
                            participant_id=participant_id,
                            provider_id=target_provider,
                            model_id=target_model,
                            reason=reason,
                        )]
                    else:
                        records = service.replace_all_using(
                            from_provider_id=source_model[0],
                            from_model_id=source_model[1],
                            provider_id=target_provider,
                            model_id=target_model,
                            reason=reason,
                        )
                print(
                    _ui(f"已替换 {len(records)} 个运行时；历史记录不变，后续调用使用新模型。", f"Replaced {len(records)} runtimes; history is unchanged and later calls use the new model."),
                    file=output,
                )
                while True:
                    again = terminal_input(_ui("继续更换其他模型？[y/N；q 安全退出] ", "Replace another model? [y/N; q safe exit] ")).strip().lower()
                    if again in {"y", "yes", "是"}:
                        break
                    if again in {"", "n", "no", "否"}:
                        return
                    if again in {"q", "退出", "quit"}:
                        raise KeyboardInterrupt
                    print(_ui("请输入 y、n 或 q。", "Enter y, n, or q."), file=output)
                break
            if target_index is None:
                continue
            # A completed replacement returns to the first menu, so runtime
            # identities are re-read rather than reused from the old choice.
            break


def _main_impl() -> int:
    enable_utf8_terminal_erase()
    parser = argparse.ArgumentParser(prog="ensemble")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p = sub.add_parser("home", help="open the interactive home screen")
    p.add_argument("--config", help="configuration file; defaults to ENSEMBLE_CONFIG")
    p.add_argument("--governance-docs", help="override the governance_docs path from configuration")
    p.set_defaults(func=cmd_home)

    p = sub.add_parser("settings", help="configure appearance, model providers, search, and evidence freshness")
    p.add_argument("--config", help="optional source configuration to copy on first setup")
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("open", help="open a meeting menu by ID/path: continue, ask Chair, or create successor")
    p.add_argument("selector", help="meeting ID or meeting workspace path")
    p.add_argument("--config", help="optional; recovered from a direct meeting path when omitted")
    p.add_argument("--governance-docs", help="override the governance_docs path from configuration")
    p.add_argument("--max-output-tokens", type=int, default=None)
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser(
        "resume", help="open a meeting by ID/path and choose continuation, Chair Q&A, or successor"
    )
    p.add_argument("selector", help="meeting ID or meeting workspace path")
    p.add_argument("--config", help="optional; recovered from the meeting when omitted")
    p.add_argument("--governance-docs", help="override the governance_docs path from configuration")
    p.add_argument("--max-output-tokens", type=int, default=None)
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("doctor", help="check config and credential-variable presence")
    p.add_argument("--config", help="configuration file; defaults to ENSEMBLE_CONFIG")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("retypeset", help="rebuild a completed report PDF as a new edition")
    p.add_argument("meeting", help="completed meeting directory")
    p.set_defaults(func=cmd_retypeset)

    p = sub.add_parser("corrigendum-edit", help="apply one Chair edit after a completed meeting")
    p.add_argument("meeting", help="completed meeting directory")
    p.add_argument("--config", required=True)
    p.add_argument("--model", required=True, help="provider:model")
    p.add_argument("--reasoning-effort", choices=[x.value for x in ReasoningEffort], default="high")
    p.add_argument("--instruction", required=True, help="Human's exact editing instruction")
    p.set_defaults(func=cmd_corrigendum_edit)

    p = sub.add_parser("discover-models", help="query configured providers for live model lists")
    p.add_argument("--config", help="configuration file; defaults to ENSEMBLE_CONFIG")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("verify-events", help="verify an append-only event hash chain")
    p.add_argument("--path", required=True)
    p.set_defaults(func=cmd_verify_events)

    p = sub.add_parser(
        "migrate-v06",
        help=f"copy and validate a v0.6 meeting for v{__version__} resume",
    )
    p.add_argument("--meeting", required=True, help="existing v0.6 meeting directory")
    p.add_argument("--output", required=True, help="new directory for the migrated copy")
    p.add_argument("--config", required=True, help=f"v{__version__} configuration file")
    p.add_argument("--dry-run", action="store_true", help="read-only compatibility check")
    p.set_defaults(func=cmd_migrate_v06)

    p = sub.add_parser("start", help="open the required terminal setup UI and initialize a meeting")
    p.add_argument("--config", help="configuration file; defaults to ENSEMBLE_CONFIG")
    p.add_argument("--governance-docs", help="override the governance_docs path from configuration")
    p.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    # Audit has no runner yet. Keep its frozen-data type and legacy resume route,
    # but do not offer creation of a meeting that cannot actually proceed.
    p.add_argument("--meeting-type", choices=[x.value for x in MeetingType if x != MeetingType.AUDIT])
    p.add_argument("--deliverable-type", choices=[x.value for x in DeliverableType])
    p.add_argument("--parent-meeting", help="source meeting directory for a derived deliverable")
    p.add_argument("--human-reference", action="append", metavar="PATH",
                   help="local PDF/TXT/Markdown/HTML/CSV reference to import at meeting initialization; repeatable")
    p.add_argument(
        "--inherit",
        choices=[x.value for x in InheritanceMode],
        help="parent assets to inherit; defaults to both for compatibility",
    )
    p.add_argument("--rendering-science-model", action="append", type=_provider_model)
    p.add_argument("--rendering-citation-model", action="append", type=_provider_model)
    p.add_argument("--rendering-language", choices=["zh", "en", "fr"])
    p.add_argument("--rendering-academic-skeleton", action="store_true")
    p.add_argument("--rendering-full-abstract", action="store_true")
    p.add_argument("--rendering-section-abstracts", action="store_true")
    p.add_argument("--rendering-segmentation", type=int, choices=range(1, 6))
    p.add_argument("--rendering-liveliness", type=int, choices=range(1, 6))
    p.add_argument(
        "--rendering-target-body-characters", type=int,
        help="advisory scholarly-rendering body length in characters; not enforced or guaranteed; references and standalone appendices excluded",
    )
    p.add_argument("--literature-language", choices=["zh", "en", "fr"])
    p.add_argument("--report-palette", choices=["ocean", "forest", "plum", "slate"],
                   help="reader-facing HTML/PDF colour palette; presentation only")
    p.add_argument("--deliberation-language", choices=["task", "zh", "en", "fr"],
                   help="language for reader-facing deliberation documents; independent of UI language")
    p.add_argument("--literature-full-abstract", action="store_true")
    p.add_argument("--literature-section-abstracts", action="store_true")
    p.add_argument("--literature-segmentation", type=int, choices=range(1, 6))
    p.add_argument("--literature-liveliness", type=int, choices=range(1, 6))
    p.add_argument("--literature-signposting", type=int, choices=range(1, 6))
    p.add_argument(
        "--literature-target-body-characters", type=int,
        help="advisory literature-review body length in non-whitespace Unicode characters; not enforced or guaranteed; references and standalone appendices excluded",
    )
    p.add_argument(
        "--rendering-output-format", action="append", choices=["html", "md", "latex", "pdf"]
    )
    p.add_argument(
        "--rendering-science-order",
        action="append",
        help="provider:model in the Human-selected sequential science-review order",
    )
    p.add_argument(
        "--rendering-science-consultation-authority",
        choices=["human", "chair"],
        help="scholarly rendering: who decides unresolved science objections (default: human)",
    )
    p.add_argument("--provider", action="append")
    p.add_argument("--model", action="append", type=_provider_model)
    p.add_argument("--chair", type=_provider_model)
    p.add_argument("--writer-model", type=_provider_model,
                   help="v0.7.1 literature review: one Writer model for all chapters")
    p.add_argument("--literature-writing-policy", choices=["v071", "fast"],
                   help="literature review workflow; fast has one Writer, no Chair, and multiple Librarian reviewers")
    p.add_argument("--writer-reasoning-effort", choices=[x.value for x in ReasoningEffort])
    p.add_argument("--fast-planner-model", action="append", type=_provider_model,
                   help="optional fast-literature module-split proposals; provide two or three models, or omit to let the Writer plan independently")
    p.add_argument("--technician-model", type=_provider_model,
                   help="optional Technician; sends minimal meeting error context to the selected model provider")
    p.add_argument("--technician-reasoning-effort", choices=[x.value for x in ReasoningEffort])
    p.add_argument("--representative-reasoning-effort", choices=[x.value for x in ReasoningEffort])
    p.add_argument("--chair-reasoning-effort", choices=[x.value for x in ReasoningEffort])
    p.add_argument("--enable-research", action="store_true")
    p.add_argument("--research-model", type=_provider_model)
    p.add_argument("--research-reasoning-effort", choices=[x.value for x in ReasoningEffort])
    p.add_argument("--research-max-concurrent-claim-groups", type=int,
                   help="maximum independent Research Desk claim groups in flight; positive integer")
    p.add_argument("--maximum-parallelism", action=argparse.BooleanOptionalAction, default=None,
                   help="up to four concurrent independent representative calls per base model; default on for literature reviews")
    p.add_argument("--model-concurrency-limit", type=int,
                   help="initial simultaneous-call cap per selected base model (1–16)")
    p.add_argument("--openalex-quota-policy", choices=("wait", "tavily"),
                   help="on confirmed daily OpenAlex exhaustion: ask Human (wait) or pre-authorize Tavily")
    p.add_argument("--task")
    p.add_argument("--title", help="human-readable meeting title; defaults to a task-derived title")
    p.add_argument("--email")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("request-human", help="persist a human decision point and send its escalation email")
    p.add_argument("--config", help="optional; recovered from the meeting when omitted")
    p.add_argument("--meeting", required=True)
    p.add_argument("--reason-code", required=True)
    p.add_argument("--summary", required=True)
    p.set_defaults(func=cmd_request_human)

    p = sub.add_parser(
        "resolve-consultation",
        help="record an immutable Human ruling for an open Chair consultation",
    )
    p.add_argument("--meeting", required=True)
    p.add_argument("--issue-id", required=True)
    p.add_argument("--decision", required=True, help="咨询中列出的中文选项；也兼容内部英文代码")
    p.add_argument("--rationale", required=True)
    p.add_argument("--wording", help="仅 USE_HUMAN_WORDING 使用的最终正文表述")
    p.add_argument("--scope", default="THIS_CONSULTATION_ONLY")
    p.set_defaults(func=cmd_resolve_consultation)

    p = sub.add_parser(
        "science-authority",
        help="show or switch the Human authorization for scholarly science objections",
    )
    p.add_argument("--meeting", required=True)
    p.add_argument("--config", help="会议 ID 不在当前目录时，可用配置文件定位全局会议索引")
    p.add_argument("--mode", choices=["human", "chair"], help="omit to inspect the current setting")
    p.set_defaults(func=cmd_science_authority)

    p = sub.add_parser(
        "run-general",
        help="run the resumable D0→D3 path, status transition, and Primary Drafter C0",
    )
    p.add_argument("--config", help="optional; recovered from the meeting when omitted")
    p.add_argument("--meeting", required=True)
    p.add_argument("--governance-docs", help="override the governance_docs path from configuration")
    p.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="set a per-call output-token limit for this run (default: no ENSEMBLE limit)",
    )
    p.add_argument("--no-progress", action="store_true", help="suppress the live terminal task table")
    p.set_defaults(func=cmd_run_general)

    p = sub.add_parser(
        "run-report",
        help="resume a literature-review meeting through evidence research and final publication",
    )
    p.add_argument("--config", help="optional; recovered from the meeting when omitted")
    p.add_argument("--meeting", required=True)
    p.add_argument("--governance-docs", help="override the governance_docs path from configuration")
    p.add_argument("--max-output-tokens", type=int, default=None)
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=cmd_run_report)

    p = sub.add_parser(
        "run-render",
        help="resume a scholarly-rendering meeting through science and citation bookkeeping",
    )
    p.add_argument("--config", help="optional; recovered from the meeting when omitted")
    p.add_argument("--meeting", required=True)
    p.add_argument("--governance-docs", help="override the governance_docs path from configuration")
    p.add_argument("--max-output-tokens", type=int, default=None)
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=cmd_run_render)

    p = sub.add_parser(
        "reassemble-render",
        help="add title, contents, and chapter headings to a completed scholarly report without rerunning models",
    )
    p.add_argument("--meeting", required=True, help="completed rendering meeting ID or directory")
    p.add_argument("--config", help="required only when resolving an indexed meeting outside the current directory")
    p.set_defaults(func=cmd_reassemble_render)

    p = sub.add_parser(
        "research-claim",
        help="submit one concrete factual claim to the meeting's shared Research Desk",
    )
    p.add_argument("--config", help="optional; recovered from the meeting when omitted")
    p.add_argument("--meeting", required=True)
    p.add_argument("--requester", required=True, help="registered Representative or Audit Member ID")
    p.add_argument("--stage", required=True, choices=[x.value for x in ResearchStage])
    p.add_argument("--claim", required=True)
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--refresh-reason")
    p.set_defaults(func=cmd_research_claim)

    p = sub.add_parser(
        "run-research",
        help="resume a research-only meeting and publish its evidence packet",
    )
    p.add_argument("--config", help="optional; recovered from the meeting when omitted")
    p.add_argument("--meeting", required=True)
    p.set_defaults(func=cmd_run_research)

    p = sub.add_parser(
        "invalidate-research-packet",
        help="record a Human cache invalidation for one meeting-local evidence packet",
    )
    p.add_argument("--meeting", required=True)
    p.add_argument("--packet", required=True)
    p.add_argument("--reason", required=True, choices=[x.value for x in CacheInvalidationReason])
    p.add_argument("--rationale", required=True)
    p.add_argument("--evidence-url")
    p.set_defaults(func=cmd_invalidate_research_packet)

    p = sub.add_parser(
        "replace-model",
        help="在暂停/安全中断后，为一个参与者记录只影响后续调用的模型替换",
    )
    p.add_argument("--config", help="可选；默认从会议配置记录恢复")
    p.add_argument("--meeting", required=True)
    target_group = p.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--participant",
        help="只替换一个参与者，例如 R-9DEBEF、CHAIR 或 RESEARCH_DESK",
    )
    target_group.add_argument(
        "--from-model",
        type=_provider_model,
        help="替换当前仍在使用该 provider:model 的全部参与者",
    )
    p.add_argument(
        "--model",
        required=True,
        type=_provider_model,
        help="目标运行时，格式 provider:model",
    )
    p.add_argument("--reason", required=True, help="替换原因；会进入治理层审计记录")
    p.set_defaults(func=cmd_replace_model)

    argv = sys.argv[1:]
    if not argv:
        argv = ["home"]
    known_commands = {
        "home",
        "settings",
        "open",
        "resume",
        "start",
        "doctor",
        "retypeset",
        "corrigendum-edit",
        "discover-models",
        "verify-events",
        "migrate-v06",
        "request-human",
        "resolve-consultation",
        "science-authority",
        "run-general",
        "run-report",
        "run-render",
        "reassemble-render",
        "run-research",
        "research-claim",
        "invalidate-research-packet",
        "replace-model",
    }
    if argv[0] not in known_commands and argv[0] not in {"-h", "--help"}:
        if argv[0].startswith("-"):
            argv = ["home", *argv]
        else:
            argv = ["open", *argv]
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        shutdown_active_control_listeners()
        resume_command = _resume_command(args)
        if resume_command is None:
            message = _ui("操作已取消；未启动会议。", "Operation cancelled; no meeting was started.")
        else:
            message = _ui("操作已取消；已经落盘的会议进度保持不变，可稍后安全恢复。", "Operation cancelled; saved meeting progress is unchanged and can be resumed safely later.")
        print(message, file=sys.stderr)
        if resume_command is not None:
            print(_ui("恢复当前会议：", "Resume this meeting:"), file=sys.stderr)
            print(f"  {resume_command}", file=sys.stderr)
            try:
                script_info = _write_resume_script(args)
            except OSError as exc:
                print(_ui(f"恢复脚本未能写入当前目录：{exc}", f"Could not write the resume script in the current directory: {exc}"), file=sys.stderr)
            else:
                if script_info is not None:
                    _, script_paths = script_info
                    print(
                        _ui("已覆盖生成恢复脚本：" + "；".join(str(path) for path in script_paths),
                            "Resume script refreshed: " + "; ".join(str(path) for path in script_paths)),
                        file=sys.stderr,
                    )
        return 130
    except Exception as exc:
        shutdown_active_control_listeners()
        try:
            return _recover_unhandled_failure(args, exc)
        except Exception as recovery_error:
            print(_ui(f"恢复界面也遇到故障：{recovery_error}。原始故障：{exc}。",
                      f"Recovery UI also failed: {recovery_error}. Original failure: {exc}."), file=sys.stderr)
            command = _resume_command(args)
            if command is not None:
                print(_ui(f"会议进度未主动删除；请重试：{command}",
                          f"Saved meeting progress was not deleted; retry: {command}"), file=sys.stderr)
            return 2


def main() -> int:
    """Restore the caller's terminal even after an unexpected exit path.

    Live progress and the fallback line editor temporarily disable terminal
    echo. Their local cleanup remains important while ENSEMBLE is running;
    this process boundary is the final safeguard before returning to a shell.
    """

    saved_terminal = None
    try:
        import termios

        if sys.stdin.isatty():
            fd = sys.stdin.fileno()
            saved_terminal = (termios, fd, termios.tcgetattr(fd))
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    try:
        return _main_impl()
    finally:
        try:
            shutdown_active_control_listeners()
        except (OSError, ValueError):
            pass
        finally:
            if saved_terminal is not None:
                tty_module, fd, original = saved_terminal
                try:
                    tty_module.tcsetattr(fd, tty_module.TCSANOW, original)
                except (OSError, ValueError, tty_module.error):
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
