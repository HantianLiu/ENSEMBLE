from __future__ import annotations

import sys
import os
import json
import re
from contextlib import contextmanager
import select
import termios
import threading
import time
import unicodedata
import weakref
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence, TextIO

from project_ensemble.domain import MeetingPhase
from project_ensemble.interface_language import ui_text
from project_ensemble.user_settings import interface_language
from project_ensemble.errors import ModelReplacementRequested
from project_ensemble.runtime.labels import persona_display_label
from project_ensemble.runtime.telemetry import TokenTelemetry
from project_ensemble.runtime.terminal_style import (
    BLUE,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    LIGHT_GRAY,
    MAGENTA,
    RED,
    YELLOW,
    rule_width,
    styled,
    supports_color,
)


_ACTIVE_CONTROL_REPORTERS: weakref.WeakSet[ConsoleProgressReporter] = weakref.WeakSet()
_ACTIVE_CONTROL_REPORTERS_LOCK = threading.Lock()


def shutdown_active_control_listeners() -> None:
    """Restore stdin for every live progress reporter after process interruption."""

    with _ACTIVE_CONTROL_REPORTERS_LOCK:
        reporters = list(_ACTIVE_CONTROL_REPORTERS)
    for reporter in reporters:
        reporter.shutdown_control_listener()


@dataclass(frozen=True)
class TaskProgressItem:
    """Public identity needed to reserve one row in the live task table."""

    task_id: str
    participant_id: str
    provider_id: str | None = None
    model_id: str | None = None
    persona: str | None = None
    label: str | None = None
    initial_state: str = "pending"
    initial_detail: str | None = None
    initial_backend_warning: str | None = None
    initial_desk_state: str | None = None
    initial_desk_detail: str | None = None
    completion_requires_commit: bool = False
    exploration_layout: bool = False
    exploration_auto_desk_updates: bool = True
    exploration_actor_title: str = "代表 · 规划方案"
    exploration_desk_title: str = "Research Desk · 探索检索"


@dataclass
class _LiveTask:
    task_id: str
    participant_id: str
    identity: str
    stage: str = "等待调度"
    state: str = "pending"
    detail: str = "等待调用"
    backend_warning: str | None = None
    reasoning_chars: int = 0
    content_chars: int = 0
    stream_state: str | None = None
    completion_requires_commit: bool = False
    desk_state: str = "pending"
    desk_detail: str = "尚未提问"
    label_locked: bool = False
    desk_auto_updates: bool = True


@dataclass(frozen=True)
class _RenderingStep:
    section_id: str
    section_index: int
    section_total: int
    title: str
    stage: str
    detail: str
    kind: str = "rendering"


class ProgressReporter(Protocol):
    def fast_workflow_stage(self, step: int) -> None: ...

    def artifact_committed(self, participant_id: str, detail: str) -> None: ...

    """Human-facing runtime progress without access to private model metadata."""

    def status(self, phase: MeetingPhase, message: str) -> None: ...

    def set_workload(self, message: str | None) -> None: ...

    def rendering_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None: ...

    def literature_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None: ...

    def fast_planning_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, detail: str,
    ) -> None: ...

    def fast_research_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None: ...

    def task_batch_started(
        self, tasks: Sequence[TaskProgressItem], *, title: str | None = None
    ) -> None: ...

    def task_started(self, task_id: str, detail: str = "正在运行") -> None: ...

    def task_waiting(self, task_id: str, detail: str) -> None: ...

    def task_backend_warning(self, task_id: str, reason: str) -> None: ...

    def task_finished(
        self, task_id: str, *, failed: bool = False, detail: str | None = None
    ) -> None: ...

    def research_step(self, participant_id: str, detail: str, *, waiting: bool = False) -> None: ...

    def research_backend_unavailable(self, participant_id: str, backend_id: str, reason: str) -> None: ...

    def exploration_actor(self, participant_id: str) -> None: ...

    def exploration_desk(self, participant_id: str, state: str, detail: str) -> None: ...

    def call_started(self, participant_id: str, stage: str) -> None: ...

    def call_started_with_runtime(
        self,
        participant_id: str,
        stage: str,
        provider_id: str,
        model_id: str,
        persona: str | None,
    ) -> None: ...

    def call_completed(self, participant_id: str, stage: str) -> None: ...

    def stream_progress(
        self,
        participant_id: str,
        stage: str,
        state: str,
        reasoning_chars: int,
        content_chars: int,
    ) -> None: ...

    def token_usage(self, telemetry: TokenTelemetry) -> None: ...

    def retrying(
        self,
        participant_id: str,
        retry_number: int,
        maximum_retries: int,
        delay_seconds: float,
        error_type: str,
    ) -> None: ...

    def speech(self, participant_id: str, label: str, content: str) -> None: ...

    def info(self, message: str) -> None: ...

    def chair_ruling_notice(self, message: str, *, failed: bool = False) -> None: ...

    def paused(self, reason_code: str, participant_id: str) -> None: ...

    def start_control_listener(self) -> None: ...

    def stop_control_listener(self) -> None: ...

    def shutdown_control_listener(self) -> None: ...

    def raise_if_control_requested(self) -> None: ...

    def defer_model_replacement(self): ...

    def control_request_pending(self) -> bool: ...

    def interactive_menu(self): ...


class NullProgressReporter:
    def fast_workflow_stage(self, step: int) -> None:
        pass

    def artifact_committed(self, participant_id: str, detail: str) -> None:
        pass

    def status(self, phase: MeetingPhase, message: str) -> None:
        pass

    def set_workload(self, message: str | None) -> None:
        pass

    def rendering_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None:
        pass

    def literature_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None:
        pass

    def fast_planning_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, detail: str,
    ) -> None:
        pass

    def fast_research_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None:
        pass

    def task_batch_started(
        self, tasks: Sequence[TaskProgressItem], *, title: str | None = None
    ) -> None:
        pass

    def task_started(self, task_id: str, detail: str = "正在运行") -> None:
        pass

    def task_waiting(self, task_id: str, detail: str) -> None:
        pass

    def task_backend_warning(self, task_id: str, reason: str) -> None:
        pass

    def task_finished(
        self, task_id: str, *, failed: bool = False, detail: str | None = None
    ) -> None:
        pass

    def research_step(self, participant_id: str, detail: str, *, waiting: bool = False) -> None:
        pass

    def research_backend_unavailable(self, participant_id: str, backend_id: str, reason: str) -> None:
        pass

    def exploration_actor(self, participant_id: str) -> None:
        pass

    def exploration_desk(self, participant_id: str, state: str, detail: str) -> None:
        pass

    def call_started(self, participant_id: str, stage: str) -> None:
        pass

    def call_started_with_runtime(
        self,
        participant_id: str,
        stage: str,
        provider_id: str,
        model_id: str,
        persona: str | None,
    ) -> None:
        pass

    def call_completed(self, participant_id: str, stage: str) -> None:
        pass

    def stream_progress(
        self,
        participant_id: str,
        stage: str,
        state: str,
        reasoning_chars: int,
        content_chars: int,
    ) -> None:
        pass

    def token_usage(self, telemetry: TokenTelemetry) -> None:
        pass

    def retrying(
        self,
        participant_id: str,
        retry_number: int,
        maximum_retries: int,
        delay_seconds: float,
        error_type: str,
    ) -> None:
        pass

    def speech(self, participant_id: str, label: str, content: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def chair_ruling_notice(self, message: str, *, failed: bool = False) -> None:
        pass

    def paused(self, reason_code: str, participant_id: str) -> None:
        pass

    def start_control_listener(self) -> None:
        pass

    def stop_control_listener(self) -> None:
        pass

    def shutdown_control_listener(self) -> None:
        pass

    def raise_if_control_requested(self) -> None:
        pass

    @contextmanager
    def defer_model_replacement(self):
        yield

    def control_request_pending(self) -> bool:
        return False

    @contextmanager
    def interactive_menu(self):
        yield


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _fit(text: str, width: int) -> str:
    """Truncate and pad text by terminal columns, including CJK full-width glyphs."""

    if width <= 0:
        return ""
    used = 0
    kept: list[str] = []
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > width:
            break
        kept.append(char)
        used += char_width
    truncated = len(kept) < len(text)
    if truncated and width >= 2:
        while kept and used + 1 > width:
            removed = kept.pop()
            used -= 2 if unicodedata.east_asian_width(removed) in {"W", "F"} else 1
        kept.append("…")
        used += 1
    return "".join(kept) + " " * max(0, width - used)


def _openalex_warning_text(reason: str, language: str) -> str:
    """Translate known OpenAlex limit fields without guessing why 429 occurred."""
    status = re.search(r"HTTP\s+(\d{3})", reason)
    remaining = re.search(r"daily_remaining=([^;\s]+)", reason)
    limit = re.search(r"daily_limit=([^;\s]+)", reason)
    reset = re.search(r"reset_seconds=([^;\s]+)", reason)
    kind = re.search(r"rate_limit_kind=([^;\s]+)", reason)
    if status:
        if kind and kind.group(1) == "DAILY":
            balance = remaining.group(1) if remaining else "unknown"
            seconds = reset.group(1) if reset else "unknown"
            return (
                f"OpenAlex daily credits insufficient ({balance} remaining); reset in {seconds} s"
                if language == "en" else
                f"OpenAlex 当日检索额度不足（剩余 {balance} credits）；约 {seconds} 秒后重置"
            )
        if kind and kind.group(1) == "NOT_DAILY":
            balance = (
                f"{remaining.group(1)}/{limit.group(1)} credits"
                if remaining and limit and remaining.group(1) != "unreported"
                and limit.group(1) != "unreported" else
                f"{remaining.group(1)} credits" if remaining and remaining.group(1) != "unreported"
                else "unreported"
            )
            return (
                f"OpenAlex HTTP 429; daily balance {balance}; not daily exhaustion; short backoff"
                if language == "en" else
                f"OpenAlex HTTP 429；当日余额 {balance}；并非日额度耗尽，短时退避重试"
            )
        parts = [f"OpenAlex HTTP {status.group(1)}"]
        if remaining and remaining.group(1) != "unreported":
            parts.append(
                f"daily credits remaining {remaining.group(1)}"
                if language == "en" else f"当日剩余额度 {remaining.group(1)} credits"
            )
        if reset and reset.group(1) != "unreported":
            parts.append(
                f"daily reset in {reset.group(1)} s"
                if language == "en" else f"当日额度重置倒计时 {reset.group(1)} 秒"
            )
        if ((remaining and remaining.group(1) != "unreported")
                or (reset and reset.group(1) != "unreported")):
            return ("; " if language == "en" else "；").join(parts)
        if status.group(1) == "429":
            return ("OpenAlex HTTP 429; daily quota or short-term rate limit unknown; "
                    "see this question's status" if language == "en" else
                    "OpenAlex HTTP 429；服务端未报告剩余额度或重置时间，无法判断限流类型；处理情况见本题状态")
    return (f"OpenAlex unavailable: {reason}" if language == "en"
            else f"OpenAlex 不可用：{reason}")


class ConsoleProgressReporter:
    """Stable task table on a TTY; compact event records for redirected output."""

    def __init__(
        self,
        output: TextIO | None = None,
        *,
        color: bool | None = None,
        live: bool | None = None,
        meeting_root: str | Path | None = None,
    ):
        self.output = output or sys.stderr
        self.language = interface_language() or "zh"
        self.color = supports_color(self.output, color)
        self.live = self._supports_live_updates() if live is None else live
        # Keep the rightmost terminal column unused: a full-width border can
        # trigger autowrap and make cursor-up redraw erase too few rows.
        self.width = max(39, rule_width(self.output) - int(self.live))
        self.meeting_root = Path(meeting_root) if meeting_root is not None else None
        self._source_record_paths: set[Path] = set()
        self._source_ids: dict[str, str] = {}
        self._last_source_scan = 0.0
        self._output_lock = threading.RLock()
        self._tasks: dict[str, _LiveTask] = {}
        self._rendered_table_lines = 0
        self._last_table_render = 0.0
        self._live_note: str | None = None
        self._batch_title: str | None = None
        self._status_context: str | None = None
        self._status_phase: MeetingPhase | None = None
        self._exploration_layout = False
        self._exploration_actor_title = "代表 · 规划方案"
        self._exploration_desk_title = "Research Desk · 探索检索"
        self._workload: str | None = None
        self._rendering_step: _RenderingStep | None = None
        self._fast_workflow_stage: int | None = None
        self._thread_task = threading.local()
        self._control_thread: threading.Thread | None = None
        self._control_stop = threading.Event()
        self._control_requested = threading.Event()
        self._defer_control_to_main_thread = False
        self._menu_active = False
        self._control_original_termios = None
        self._controls_active = False
        self._control_users = 0
        self._control_generation = 0
        self._control_owners = threading.local()

    def _supports_live_updates(self) -> bool:
        try:
            return bool(self.output.isatty())
        except (AttributeError, OSError):
            return False

    def _timestamp(self) -> str:
        return datetime.now().astimezone().strftime("%H:%M:%S")

    def _clear_live_table_locked(self) -> None:
        if self._menu_active or not self.live or not self._rendered_table_lines:
            return
        for _ in range(self._rendered_table_lines):
            self.output.write("\x1b[1A\x1b[2K\r")
        self._rendered_table_lines = 0

    def _task_style(self, state: str) -> tuple[str, ...]:
        if state == "pending":
            return (DIM,)
        if state == "completed":
            return (GREEN,)
        if state == "failed":
            return (RED,)
        return (BOLD,)

    def _terminal_height(self) -> int:
        try:
            return os.get_terminal_size(self.output.fileno()).lines
        except (AttributeError, OSError, ValueError):
            return 24

    def _research_source_counts(self) -> tuple[int, int]:
        """Count unique cited/archived sources, not provider tokens or full-text reads."""

        if self.meeting_root is None:
            return (0, 0)
        now = time.monotonic()
        if now - self._last_source_scan < 2.0:
            return (
                sum(kind == "literature" for kind in self._source_ids.values()),
                sum(kind == "web" for kind in self._source_ids.values()),
            )
        self._last_source_scan = now
        records = self.meeting_root / "public/research/literature_bundle/records"
        if records.is_dir():
            for path in records.glob("*.json"):
                if path in self._source_record_paths:
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                self._source_record_paths.add(path)
                source_id = record.get("source_id")
                if not isinstance(source_id, str) or not source_id:
                    continue
                source_type = str(record.get("source_type") or "").lower()
                scholarly = source_type != "web_page" and (
                    source_id.startswith("https://openalex.org/")
                    or bool(record.get("doi"))
                    or bool(record.get("publication_year") and record.get("authors"))
                )
                self._source_ids[source_id] = "literature" if scholarly else "web"
        return (
            sum(kind == "literature" for kind in self._source_ids.values()),
            sum(kind == "web" for kind in self._source_ids.values()),
        )

    def _workflow_tree(self) -> tuple[tuple[str, ...], int] | None:
        """A stable top-level progress tree shared by every meeting type."""
        if self._fast_workflow_stage is not None:
            labels = (
                ("Confirm task brief", "Plan modules", "Research evidence",
                 "Draft and review modules", "Assemble and publish")
                if self.language == "en" else
                ("确认任务书", "制定模块执行单", "研究与证据核查",
                 "逐模块写作与科学审阅", "组装与出版")
            )
            return labels, self._fast_workflow_stage
        phase = self._status_phase
        if phase is None:
            return None
        groups = (
            (
                (MeetingPhase.RESEARCH_PLANNING, MeetingPhase.RESEARCH_OUTLINE_REVIEW),
                (MeetingPhase.LITERATURE_MODULE_RESEARCH, MeetingPhase.LITERATURE_MODULE_DRAFTING,
                 MeetingPhase.LITERATURE_MODULE_REVIEW, MeetingPhase.LITERATURE_MODULE_CONFIRMATION),
                (MeetingPhase.LITERATURE_REPORT_SYNTHESIS, MeetingPhase.LITERATURE_REPORT_REVIEW),
                (MeetingPhase.LITERATURE_REPORT_PUBLICATION,),
            ),
            (
                (MeetingPhase.SCHOLARLY_RENDERING_PLAN,),
                (MeetingPhase.SCHOLARLY_RENDERING_DRAFT,),
                (MeetingPhase.SCHOLARLY_SCIENCE_REVIEW,),
                (MeetingPhase.SCHOLARLY_CITATION_REVIEW,),
                (MeetingPhase.SCHOLARLY_RENDERING_PUBLICATION,),
            ),
            (
                (MeetingPhase.INITIAL_DRAFT, MeetingPhase.GENERAL_POSITION),
                (MeetingPhase.AMENDMENT_SUBMISSION, MeetingPhase.COSPONSORSHIP),
                (MeetingPhase.BALLOT, MeetingPhase.GENERAL_RATIFICATION,
                 MeetingPhase.STATUS_TRANSITION),
                (MeetingPhase.DETAILED_DRAFTING, MeetingPhase.CLAUSE_REVIEW,
                 MeetingPhase.CHAIR_REVIEW, MeetingPhase.THINK_TANK_REVIEW),
                (MeetingPhase.RESOLUTION_FROZEN, MeetingPhase.HUMAN_REVIEW),
            ),
        )
        names_zh = (
            ("规划与总纲审阅", "逐模块研究、写作与核校", "全篇综合与审阅", "出版"),
            ("分块规划", "重绘撰写", "科学事实审阅", "引文审阅", "出版"),
            ("提出初稿与立场", "修正案与联署", "表决", "条文细化与审阅", "冻结与交付"),
        )
        names_en = (
            ("Planning and outline", "Module research, drafting and review",
             "Whole-report synthesis", "Publication"),
            ("Section planning", "Rendering draft", "Science review",
             "Citation review", "Publication"),
            ("Initial draft and positions", "Amendments and sponsorship",
             "Ballots", "Clause drafting and review", "Freeze and handoff"),
        )
        if phase == MeetingPhase.RESEARCH_ROUND:
            return (("Verify claim",) if self.language == "en" else ("核实命题",)), 1
        for kind, sequence in enumerate(groups):
            for position, phases in enumerate(sequence, 1):
                if phase in phases:
                    return (names_en if self.language == "en" else names_zh)[kind], position
        return None

    def _table_lines_locked(self) -> list[str]:
        tasks = list(self._tasks.values())
        rendering = self._rendering_step
        workflow = self._workflow_tree()
        if not tasks and rendering is None and workflow is None:
            return []
        completed = sum(task.state == "completed" for task in tasks)
        failed = sum(task.state == "failed" for task in tasks)
        if self._exploration_layout:
            desk_completed = sum(task.desk_state == "completed" for task in tasks)
            progress = (
                f"Representatives submitted {completed}/{len(tasks)} · Desk completed {desk_completed}/{len(tasks)}"
                if self.language == "en" else
                f"代表已提交 {completed}/{len(tasks)} · Desk 已完成 {desk_completed}/{len(tasks)}"
            )
        else:
            progress = f"Completed {completed}/{len(tasks)}" if self.language == "en" else f"完成 {completed}/{len(tasks)}"
            if (rendering is not None and rendering.kind == "literature"
                    and rendering.stage == "draft"
                    and self._status_context and "主笔撰写" in self._status_context):
                progress = (
                    "Draft saved" if completed == len(tasks) and tasks
                    else "Draft not yet saved; Research Desk may receive further calls"
                ) if self.language == "en" else (
                    "初稿已落盘" if completed == len(tasks) and tasks
                    else "初稿尚未落盘；Research Desk 可能继续被调用"
                )
            elif rendering is not None and rendering.kind in {"literature", "fast_research", "fast_planning"}:
                progress = (
                    f"Subtasks committed {completed}/{len(tasks)}"
                    if self.language == "en" else f"本批子任务已完成 {completed}/{len(tasks)}"
                )
            if tasks and all(task.task_id.startswith("fast-desk-") for task in tasks):
                running = sum(task.state == "running" for task in tasks)
                model_waiting = sum(
                    task.state == "pending" and task.detail.startswith("等待模型名额")
                    for task in tasks
                )
                openalex_waiting = sum(
                    task.state == "pending" and task.detail.startswith("OpenAlex 当日额度耗尽")
                    for task in tasks
                )
                rate_waiting = sum(
                    task.state == "pending" and task.detail.startswith("检索后端限速")
                    for task in tasks
                )
                not_started = len(tasks) - completed - failed - running - model_waiting - openalex_waiting - rate_waiting
                progress = (
                    f"Packets saved {completed}/{len(tasks)} · processing {running} · model slots {model_waiting} · OpenAlex reset {openalex_waiting} · rate backoff {rate_waiting} · queued {not_started}"
                    if self.language == "en" else
                    f"证据包已落盘 {completed}/{len(tasks)} · 处理 {running} · 等模型 {model_waiting} · 等 OpenAlex {openalex_waiting} · 限速等待 {rate_waiting} · 未启动 {not_started}"
                )
        if failed:
            progress += f" · Failed {failed}" if self.language == "en" else f" · 失败 {failed}"
        batch_title = ui_text(self._batch_title or self._status_context or ("Current task" if self.language == "en" else "当前任务"), self.language)
        # Cursor-up redraws can only erase lines that are still on screen. A
        # publication batch may contain hundreds of individual patch votes;
        # rendering every vote would scroll the top of the table away and
        # leave a fresh copy in scrollback after every progress update.
        max_lines = max(1, self._terminal_height() - 2)
        if max_lines < (14 if rendering and rendering.kind != "fast_planning" else 8):
            running = sum(task.state == "running" for task in tasks)
            workload = f" · {self._workload}" if self._workload else ""
            if rendering:
                batch_title = (
                    f"{('Rendering section' if rendering.kind == 'rendering' else 'Research module') if self.language == 'en' else ('重绘章节' if rendering.kind == 'rendering' else '研究模块')}"
                    f" {rendering.section_index}/{rendering.section_total}"
                    f" · {rendering.stage} · {ui_text(rendering.detail, self.language)}"
                )
            return [
                styled(
                    _fit(
                        f"{('Progress' if self.language == 'en' else '进度')} · {batch_title} · {progress} · {('Running' if self.language == 'en' else '运行')} {running}"
                        + workload
                        + (" · Ctrl+C interrupt / Ctrl+R models & controls" if self.language == "en" else " · Ctrl+C 中断 / Ctrl+R 模型与运行参数"),
                        self.width,
                    ),
                    CYAN,
                    enabled=self.color,
                )
            ]
        fixed_lines = (
            4 + int(self.live) + int(bool(self._live_note))
            + int(bool(self._workload)) + (6 if rendering else 0)
            + (len(workflow[0]) + 1 if workflow else 0)
            + (min(rendering.section_total, 6) if workflow and rendering and rendering.kind != "fast_research" else 0)
        )
        row_limit = max(1, max_lines - fixed_lines)
        visible_tasks = tasks
        hidden_tasks: list[_LiveTask] = []
        if sum(1 + bool(task.backend_warning) for task in tasks) > row_limit:
            if row_limit > 1:
                row_limit -= 1  # Reserve one row for the hidden-task summary.
            priority = (
                [task for task in tasks if task.state == "running"]
                + [task for task in tasks if task.state == "failed"]
                + list(reversed([task for task in tasks if task.state == "completed"]))
                + [task for task in tasks if task.state == "pending"]
            )
            visible_ids: set[str] = set()
            occupied = 0
            for task in priority:
                cost = 1 + bool(task.backend_warning)
                if occupied + cost <= row_limit:
                    visible_ids.add(task.task_id)
                    occupied += cost
            if not visible_ids and priority:
                visible_ids.add(priority[0].task_id)
            visible_tasks = [task for task in tasks if task.task_id in visible_ids]
            hidden_tasks = [task for task in tasks if task.task_id not in visible_ids]
        title_prefix = "┌─ "
        title_suffix = f" · {progress} " if tasks else " "
        if rendering:
            if rendering.kind == "fast_research" and rendering.stage == "desk":
                batch_title = (
                    f"Research round {rendering.section_index}/{rendering.section_total} · {rendering.title}"
                    if self.language == "en" else
                    f"第 {rendering.section_index}/{rendering.section_total} 轮资料核查 · {rendering.title}"
                )
            else:
                batch_title = (
                    f"{'Rendering section' if rendering.kind == 'rendering' else 'Research module'} "
                    f"{rendering.section_index}/{rendering.section_total} · {rendering.section_id} · {rendering.title}"
                    if self.language == "en" else
                    f"第 {rendering.section_index}/{rendering.section_total} 个"
                    f"{'重绘章节' if rendering.kind == 'rendering' else '研究模块'}"
                    f" · {rendering.section_id} · {rendering.title}"
                )
        title_width = max(
            8,
            self.width - _display_width(title_prefix) - _display_width(title_suffix),
        )
        visible_batch_title = _fit(batch_title, title_width).rstrip()
        title = title_prefix + visible_batch_title + title_suffix
        lines = [
            styled(
                title + "─" * max(0, self.width - _display_width(title)),
                CYAN,
                enabled=self.color,
            )
        ]
        pending_workflow_steps: list[tuple[int, str]] = []
        pending_module_steps: list[int] = []
        if workflow:
            labels, current = workflow
            for position, label in enumerate(labels, 1):
                if position > current:
                    pending_workflow_steps.append((position, label))
                    continue
                state = "completed" if position < current else (
                    "running" if position == current else "pending"
                )
                symbol = {"completed": "✓", "running": "●", "pending": "○"}[state]
                lines.append(styled(
                    f"│ {_fit(f'  {symbol} {position}/{len(labels)} · {label}', self.width - 3)}",
                    *self._task_style(state), enabled=self.color,
                ))
            if rendering:
                show_modules = rendering.kind != "fast_research"
                if show_modules:
                    total = rendering.section_total
                    current_module = rendering.section_index
                    previous = range(1, current_module)
                    if total > 6 and current_module > 4:
                        lines.append(styled(
                            f"│ {_fit(f'    ├─ ✓ 已完成前 {current_module - 3} 个模块', self.width - 3)}",
                            *self._task_style("completed"), enabled=self.color,
                        ))
                        previous = range(current_module - 2, current_module)
                    for module_index in previous:
                        module_id = f"{'SR' if rendering.kind == 'rendering' else 'RM'}-{module_index:02d}"
                        lines.append(styled(
                            f"│ {_fit(f'    ├─ ✓ {module_id} · 模块 {module_index}/{total}', self.width - 3)}",
                            *self._task_style("completed"), enabled=self.color,
                        ))
                module_label = (
                    f"    ├─ ● 模块 {rendering.section_index}/{rendering.section_total} · {rendering.section_id} · {rendering.title}"
                    if rendering.kind != "rendering" else
                    f"    ├─ ● 重绘章节 {rendering.section_index}/{rendering.section_total} · {rendering.section_id} · {rendering.title}"
                )
                if rendering.kind == "fast_research":
                    module_label = (
                        f"    └─ ● 研究轮次 {rendering.section_index}/{rendering.section_total}"
                        f" · {rendering.title}"
                    )
                lines.append(styled(
                    f"│ {_fit(module_label, self.width - 3)}", BOLD, enabled=self.color,
                ))
                if show_modules:
                    pending_module_steps = list(range(
                        rendering.section_index + 1,
                        min(rendering.section_total, rendering.section_index + 2
                            if rendering.section_total > 6 else rendering.section_total) + 1,
                    ))
            if rendering is None:
                for position, label in pending_workflow_steps:
                    lines.append(styled(
                        f"│ {_fit(f'  ○ {position}/{len(labels)} · {label}', self.width - 3)}",
                        *self._task_style("pending"), enabled=self.color,
                    ))
            if not tasks and rendering is None:
                lines.append(styled("└" + "─" * (self.width - 1), CYAN, enabled=self.color))
                return lines
        if rendering:
            if rendering.kind == "rendering":
                stage_order = ("draft", "science", "citation")
                stage_labels = {
                    "draft": "重绘撰写", "science": "科学性审阅", "citation": "引文审阅",
                }
            elif rendering.kind == "fast_planning":
                stage_order = ("plan",)
                stage_labels = {"plan": "制定模块执行单"}
            elif rendering.kind == "fast_research":
                stage_order = ("queries", "desk")
                stage_labels = {"queries": "主笔确定问题", "desk": "Research Desk 核查证据"}
            else:
                stage_order = ("outline", "draft", "science", "citation")
                stage_labels = {
                    "outline": "写作提纲", "draft": "学术主笔", "science": "科学性审阅",
                    "citation": "引文与组装",
                }
            if self.language == "en" and rendering.kind == "fast_planning":
                stage_labels = {"plan": "Prepare module execution plan"}
            elif self.language == "en" and rendering.kind == "fast_research":
                stage_labels = {"queries": "Writer frames questions", "desk": "Research Desk checks evidence"}
            elif self.language == "en":
                stage_labels = {
                    "outline": "Writing outline", "draft": "Academic drafting" if rendering.kind != "rendering" else "Rendering draft",
                    "science": "Science review", "citation": "Citation review and assembly",
                }
            current_order = stage_order.index(rendering.stage)
            for stage in stage_order:
                position = stage_order.index(stage)
                state = "completed" if position < current_order else (
                    "running" if position == current_order else "pending"
                )
                symbol = {"completed": "✓", "running": "●", "pending": "○"}[state]
                label = f"{'    │   ├─ ' if workflow else '  '}{symbol} {stage_labels[stage]}"
                lines.append(styled(
                    f"│ {_fit(label, self.width - 3)}", *self._task_style(state),
                    enabled=self.color,
                ))
                if stage == rendering.stage:
                    lines.append(styled(
                        f"│ {_fit(('    │   │   └─ ' if workflow else '      └─ ') + ui_text(rendering.detail, self.language), self.width - 3)}",
                        BOLD, enabled=self.color,
                    ))
            if workflow:
                for module_index in pending_module_steps:
                    module_id = f"{'SR' if rendering.kind == 'rendering' else 'RM'}-{module_index:02d}"
                    lines.append(styled(
                        f"│ {_fit(f'    ├─ ○ {module_id} · 模块 {module_index}/{rendering.section_total}', self.width - 3)}",
                        *self._task_style("pending"), enabled=self.color,
                    ))
                if (rendering.kind != "fast_research" and rendering.section_total > 6
                        and pending_module_steps
                        and pending_module_steps[-1] < rendering.section_total):
                    remaining = rendering.section_total - pending_module_steps[-1]
                    lines.append(styled(
                        f"│ {_fit(f'    └─ ○ 后续还有 {remaining} 个模块', self.width - 3)}",
                        *self._task_style("pending"), enabled=self.color,
                    ))
                for position, label in pending_workflow_steps:
                    lines.append(styled(
                        f"│ {_fit(f'  ○ {position}/{len(labels)} · {label}', self.width - 3)}",
                        *self._task_style("pending"), enabled=self.color,
                    ))
            if not tasks:
                lines.append(styled("└" + "─" * (self.width - 1), CYAN, enabled=self.color))
                return lines
            lines.append(styled("├" + "─" * (self.width - 1), CYAN, enabled=self.color))
            batch_title = ui_text(self._batch_title or self._status_context or ("Current subtask" if self.language == "en" else "当前子任务"), self.language)
            lines.append(styled(
                f"│ {_fit(batch_title + ' · ' + progress, self.width - 3)}",
                CYAN, enabled=self.color,
            ))
        first_column = min(44, max(20, self.width // 2))
        second_column = max(12, self.width - first_column - 5)
        if self._exploration_layout:
            header = (
                f"│ {_fit(self._exploration_actor_title, first_column)} │ "
                f"{_fit(self._exploration_desk_title, second_column)}"
            )
        else:
            header = f"│ {_fit('Task' if self.language == 'en' else '任务', first_column)} │ {_fit('Status' if self.language == 'en' else '状态', second_column)}"
        lines.append(styled(header, BOLD, enabled=self.color))
        lines.append(
            styled(
                "├"
                + "─" * (first_column + 2)
                + "┼"
                + "─" * (second_column + 1),
                CYAN,
                enabled=self.color,
            )
        )
        if self._workload:
            scope = _fit(
                f"Overview: {ui_text(self._workload, self.language)}" if self.language == "en"
                else f"总览：{self._workload}", self.width - 3,
            )
            lines.append(styled(f"│ {scope}", CYAN, enabled=self.color))
        state_symbols = {
            "pending": "○",
            "running": "●",
            "completed": "✓",
            "failed": "✗",
        }
        for task in visible_tasks:
            symbol = state_symbols.get(task.state, "●")
            if self._exploration_layout:
                left = f"{symbol} {task.identity} · {ui_text(task.detail, self.language)}"
                desk_symbol = state_symbols.get(task.desk_state, "●")
                right = f"{desk_symbol} {ui_text(task.desk_detail, self.language)}"
            else:
                left, right = f"{symbol} {task.identity}", ui_text(task.detail, self.language)
            row = f"│ {_fit(left, first_column)} │ {_fit(right, second_column)}"
            row_style = (
                (YELLOW,) if task.state == "pending" and task.detail.startswith(("OpenAlex 当日额度耗尽", "检索后端限速"))
                else (LIGHT_GRAY,) if task.state == "pending" and task.participant_id == "CHAIR"
                else self._task_style(task.state)
            )
            lines.append(styled(row, *row_style, enabled=self.color))
            if task.backend_warning and (not hidden_tasks or row_limit >= 2):
                warning = _openalex_warning_text(task.backend_warning, self.language)
                lines.append(styled(
                    f"│ {_fit('    ↳ ' + warning, self.width - 3)}",
                    YELLOW, enabled=self.color,
                ))
        if hidden_tasks:
            hidden_running = sum(task.state == "running" for task in hidden_tasks)
            hidden_failed = sum(task.state == "failed" for task in hidden_tasks)
            hidden_completed = sum(task.state == "completed" for task in hidden_tasks)
            hidden_pending = sum(task.state == "pending" for task in hidden_tasks)
            summary = (
                f"Other {len(hidden_tasks)}: running {hidden_running} · failed {hidden_failed}"
                f" · completed {hidden_completed} · pending {hidden_pending}"
                if self.language == "en" else
                f"其余 {len(hidden_tasks)} 项：运行 {hidden_running} · 失败 {hidden_failed}"
                f" · 已完成 {hidden_completed} · 待调用 {hidden_pending}"
            )
            lines.append(styled(f"│ {_fit(summary, self.width - 3)}", DIM, enabled=self.color))
        if self._live_note:
            note = _fit(
                f"Info: {ui_text(self._live_note, self.language)}" if self.language == "en"
                else f"信息：{self._live_note}", self.width - 3,
            )
            lines.append(styled(f"│ {note}", DIM, enabled=self.color))
        if self.live:
            source_prefix = ""
            if self.meeting_root is not None:
                literature, web = self._research_source_counts()
                fast_desk = bool(tasks) and all(task.task_id.startswith("fast-desk-") for task in tasks)
                source_prefix = (
                    f"{'Sources archived' if fast_desk else 'Sources read'}: papers {literature} · web pages {web} │ "
                    if self.language == "en" else
                    f"{'已归档来源' if fast_desk else '已查阅来源'}：文献 {literature} · 网页 {web} │ "
                )
            hint = _fit(source_prefix + (
                "Keys: Ctrl+C stop safely · Ctrl+R models and controls" if self.language == "en"
                else "快捷键：Ctrl+C 安全中断 · Ctrl+R 模型与运行参数"
            ), self.width - 3)
            lines.append(styled(f"│ {hint}", DIM, enabled=self.color))
        lines.append(styled("└" + "─" * (self.width - 1), CYAN, enabled=self.color))
        if len(lines) > max_lines and workflow and rendering:
            # Preserve the active nested task, but trim far-away module rows
            # before falling back to a single line on a short terminal.
            for marker in ("后续还有", "├─ ○ RM-", "├─ ○ SR-",
                           "├─ ✓ RM-", "├─ ✓ SR-", "已完成前"):
                while len(lines) > max_lines:
                    candidate = next((i for i, line in enumerate(lines) if marker in line), None)
                    if candidate is None:
                        break
                    lines.pop(candidate)
        if len(lines) > max_lines:
            # Hidden-task summaries can consume the last available row. Never
            # scroll the top of an in-place table out of the visible viewport.
            running = sum(task.state == "running" for task in tasks)
            scope = f" · {self._workload}" if self._workload else ""
            short_title = (
                f"{'重绘章节' if rendering.kind == 'rendering' else '研究模块'}"
                f" {rendering.section_index}/{rendering.section_total}"
                f" · {rendering.stage} · {rendering.detail}"
                if rendering else (self._batch_title or self._status_context or "当前任务")
            )
            return [styled(
                _fit(
                    f"进度 · {short_title} · {progress} · 运行 {running}"
                    + scope + " · Ctrl+C 中断 / Ctrl+R 模型与参数",
                    self.width,
                ),
                CYAN,
                enabled=self.color,
            )]
        return lines

    def start_control_listener(self) -> None:
        """Listen for Ctrl-R while provider calls are running.

        The listener only records a request.  It never mutates meeting state or
        performs a replacement from its background thread; the main execution
        path handles the request at a durable provider-call boundary.
        """

        if not self.live:
            return
        with self._output_lock:
            if self._menu_active:
                return
            owners = getattr(self._control_owners, "generations", [])
            owners.append(self._control_generation)
            self._control_owners.generations = owners
            if self._control_thread is not None or self._control_users:
                self._control_users += 1
                return
            self._control_users = 1
        try:
            stream = sys.stdin
            if not stream.isatty():
                self._control_users = 0
                return
            fd = stream.fileno()
            original = termios.tcgetattr(fd)
            edited = termios.tcgetattr(fd)
            edited[3] &= ~termios.ICANON
            edited[3] &= ~termios.ECHO
            edited[3] |= termios.ISIG
            edited[6][termios.VMIN] = 1
            edited[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, edited)
        except (AttributeError, OSError, termios.error, ValueError):
            self._control_users = 0
            return
        self._control_original_termios = (fd, original)
        self._control_stop.clear()
        self._control_requested.clear()
        self._controls_active = True

        def listen() -> None:
            try:
                while not self._control_stop.is_set():
                    readable, _writable, _exceptional = select.select([fd], [], [], 0.1)
                    if not readable:
                        continue
                    value = os.read(fd, 1)
                    if value == b"\x12":  # Ctrl-R
                        if not self._control_requested.is_set():
                            self._control_requested.set()
                            self.info("已收到 Ctrl+R；即将打开模型与运行参数界面，已发出的请求继续完成")
            except (OSError, ValueError):
                pass

        self._control_thread = threading.Thread(
            target=listen,
            name="ensemble-terminal-controls",
            daemon=True,
        )
        self._control_thread.start()
        with _ACTIVE_CONTROL_REPORTERS_LOCK:
            _ACTIVE_CONTROL_REPORTERS.add(self)
        with self._output_lock:
            self._render_live_table_locked(force=True)

    def stop_control_listener(self) -> None:
        with self._output_lock:
            owners = getattr(self._control_owners, "generations", [])
            if not owners:
                return
            generation = owners.pop()
            if generation != self._control_generation:
                return
            if self._control_thread is None:
                return
            self._control_users = max(0, self._control_users - 1)
            if self._control_users:
                return
            thread = self._control_thread
            self._control_stop.set()
        thread.join(timeout=0.5)
        original = self._control_original_termios
        if original is not None:
            fd, attributes = original
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, attributes)
            except (OSError, termios.error, ValueError):
                pass
        self._control_thread = None
        self._control_original_termios = None
        self._controls_active = False
        with _ACTIVE_CONTROL_REPORTERS_LOCK:
            _ACTIVE_CONTROL_REPORTERS.discard(self)

    def shutdown_control_listener(self) -> None:
        """Unconditionally release stdin and restore its original TTY mode.

        ``start``/``stop`` are reference-counted because parallel provider
        calls share one listener.  At a run boundary (Ctrl-R, Ctrl-C, or an
        exception), no interactive menu may begin until every reference has
        been discarded and the background reader has stopped consuming input.
        """

        with self._output_lock:
            self._control_generation += 1
            if self._control_thread is None:
                self._control_users = 0
                self._control_requested.clear()
                with _ACTIVE_CONTROL_REPORTERS_LOCK:
                    _ACTIVE_CONTROL_REPORTERS.discard(self)
                return
            self._control_users = 1
            owners = getattr(self._control_owners, "generations", [])
            owners.append(self._control_generation)
            self._control_owners.generations = owners
        self.stop_control_listener()
        self._control_requested.clear()

    def raise_if_control_requested(self) -> None:
        if self._defer_control_to_main_thread and threading.current_thread() is not threading.main_thread():
            return
        if self._control_requested.is_set():
            self._control_requested.clear()
            # Restore canonical input before the exception reaches the CLI.
            # Otherwise the still-running background reader races the model
            # replacement menu for the Human's selection keystrokes.
            self.shutdown_control_listener()
            raise ModelReplacementRequested("Human requested interactive model replacement")

    @contextmanager
    def defer_model_replacement(self):
        """Let the batch scheduler open the Human menu at a safe call boundary."""

        self._defer_control_to_main_thread = True
        try:
            yield
        finally:
            self._defer_control_to_main_thread = False

    def control_request_pending(self) -> bool:
        return self._control_requested.is_set()

    @contextmanager
    def interactive_menu(self):
        """Give stdin to Human while background calls keep recording progress."""

        self.shutdown_control_listener()
        with self._output_lock:
            self._clear_live_table_locked()
            self._menu_active = True
        try:
            yield
        finally:
            with self._output_lock:
                self._menu_active = False
                self._render_live_table_locked(force=True)
            self.start_control_listener()

    @contextmanager
    def consultation_display(self):
        """Keep model progress from redrawing over a Human consultation."""
        with self._output_lock:
            self._clear_live_table_locked()
            self._menu_active = True
        try:
            yield
        finally:
            with self._output_lock:
                self._menu_active = False
                # The next workflow event will establish its own fresh frame.
                # Repainting the old batch here would leave stale rows above
                # the Human's finished consultation.
                self._rendered_table_lines = 0

    def _render_live_table_locked(self, *, force: bool = False) -> None:
        if self._menu_active or not self.live or not self._tasks:
            return
        now = time.monotonic()
        if not force and now - self._last_table_render < 0.25:
            return
        self._clear_live_table_locked()
        # A terminal may be resized while a provider is generating. Measure
        # again for each redraw so a stale width cannot introduce autowrap.
        self.width = max(39, rule_width(self.output) - 1)
        lines = self._table_lines_locked()
        self.output.write("\n".join(lines) + "\n")
        self.output.flush()
        self._rendered_table_lines = len(lines)
        self._last_table_render = now

    def _print_event_locked(self, kind: str, text: str, *codes: str) -> None:
        clock = styled(f"[{self._timestamp()}]", DIM, enabled=self.color)
        label = styled(f"{ui_text(kind, self.language):<7}", *codes, enabled=self.color)
        print(f"{clock} {label} {text}", file=self.output, flush=True)

    def _write(self, kind: str, text: str, *codes: str) -> None:
        with self._output_lock:
            if self._menu_active:
                return
            self._clear_live_table_locked()
            self._print_event_locked(kind, text, *codes)
            self._render_live_table_locked(force=True)

    def _rule_locked(self, color: str = CYAN) -> None:
        print(styled("─" * self.width, color, enabled=self.color), file=self.output, flush=True)

    def status(self, phase: MeetingPhase, message: str) -> None:
        with self._output_lock:
            if phase == self._status_phase and message == self._status_context:
                return
            self._clear_live_table_locked()
            self._summarize_current_batch_locked()
            self._tasks.clear()
            self._live_note = None
            self._batch_title = None
            self._exploration_layout = False
            self._exploration_actor_title = "代表 · 规划方案"
            self._exploration_desk_title = "Research Desk · 探索检索"
            self._status_phase = phase
            self._status_context = message
            preserve_tree = self._rendering_step is not None and (
                (self._rendering_step.kind == "rendering" and phase in {
                    MeetingPhase.SCHOLARLY_RENDERING_DRAFT,
                    MeetingPhase.SCHOLARLY_SCIENCE_REVIEW,
                    MeetingPhase.SCHOLARLY_CITATION_REVIEW,
                }) or
                (self._rendering_step.kind == "literature" and phase in {
                    MeetingPhase.LITERATURE_MODULE_DRAFTING,
                    MeetingPhase.LITERATURE_MODULE_REVIEW,
                }) or
                (self._rendering_step.kind == "fast_planning"
                 and phase == MeetingPhase.RESEARCH_PLANNING) or
                (self._rendering_step.kind == "fast_research"
                 and phase == MeetingPhase.LITERATURE_MODULE_RESEARCH)
            )
            if preserve_tree:
                self._render_live_table_locked(force=True)
                return
            self._rendering_step = None
            if self.live and self._workflow_tree() is not None:
                self._render_live_table_locked(force=True)
                return
            self._rule_locked()
            self._print_event_locked("状态", f"{phase.value} · {ui_text(message, self.language)}", BOLD, CYAN)

    def fast_workflow_stage(self, step: int) -> None:
        """Set the five-step fast workflow root, including on resumed meetings."""
        if not 1 <= step <= 5:
            raise ValueError("fast workflow step must be 1–5")
        with self._output_lock:
            if self._fast_workflow_stage == step:
                return
            self._clear_live_table_locked()
            self._fast_workflow_stage = step
            self._render_live_table_locked(force=True)

    def set_workload(self, message: str | None) -> None:
        """Keep a meeting-wide workload visible across changing task batches."""

        with self._output_lock:
            if message == self._workload:
                return
            self._workload = message
            if self.live:
                self._render_live_table_locked(force=True)
            elif message:
                self._print_event_locked("总览", message, CYAN)

    def rendering_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None:
        """Replace rendering status messages with one section/stage/round tree."""
        if stage not in {"draft", "science", "citation"}:
            raise ValueError(f"unknown rendering stage: {stage}")
        self._stage_step(
            section_id=section_id, section_index=section_index,
            section_total=section_total, title=title, stage=stage,
            detail=detail, kind="rendering",
        )

    def literature_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None:
        if stage not in {"outline", "draft", "science", "citation"}:
            raise ValueError(f"unknown literature stage: {stage}")
        self._stage_step(
            section_id=section_id, section_index=section_index,
            section_total=section_total, title=title, stage=stage,
            detail=detail, kind="literature",
        )

    def fast_planning_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, detail: str,
    ) -> None:
        """Show fast module planning as a stable tree, not repeated status events."""
        self._stage_step(
            section_id=section_id, section_index=section_index,
            section_total=section_total, title=title, stage="plan",
            detail=detail, kind="fast_planning",
        )

    def fast_research_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str,
    ) -> None:
        if stage not in {"queries", "desk"}:
            raise ValueError(f"unknown fast research stage: {stage}")
        self._stage_step(
            section_id=section_id, section_index=section_index,
            section_total=section_total, title=title, stage=stage,
            detail=detail, kind="fast_research",
        )

    def _stage_step(
        self, *, section_id: str, section_index: int, section_total: int,
        title: str, stage: str, detail: str, kind: str,
    ) -> None:
        stage_labels = (
            {"draft": "重绘撰写", "science": "科学性审阅", "citation": "引文审阅"}
            if kind == "rendering" else
            {"plan": "制定模块执行单"} if kind == "fast_planning" else
            {"queries": "主笔确定问题", "desk": "Research Desk 核查证据"}
            if kind == "fast_research" else
            {"outline": "写作提纲", "draft": "学术主笔", "science": "科学性审阅",
             "citation": "引文与组装"}
        )
        step = _RenderingStep(
            section_id, section_index, section_total, title, stage, detail, kind
        )
        with self._output_lock:
            if step == self._rendering_step:
                return
            self._clear_live_table_locked()
            self._rendering_step = step
            self._tasks.clear()
            self._batch_title = None
            self._live_note = None
            if self.live:
                self._render_live_table_locked(force=True)
            else:
                self._print_event_locked(
                    "进度", f"{'章节' if kind == 'rendering' else '模块'}"
                    f" {section_index}/{section_total} · {section_id}"
                    f" · {title} / {stage_labels[stage]} / {detail}", CYAN,
                )

    def task_batch_started(
        self, tasks: Sequence[TaskProgressItem], *, title: str | None = None
    ) -> None:
        if not self.live or not tasks:
            return
        with self._output_lock:
            self._clear_live_table_locked()
            self._summarize_current_batch_locked()
            self._tasks.clear()
            self._live_note = None
            self._batch_title = title
            self._exploration_layout = any(item.exploration_layout for item in tasks)
            self._exploration_actor_title = next(
                (item.exploration_actor_title for item in tasks if item.exploration_layout),
                "代表 · 规划方案",
            )
            self._exploration_desk_title = next(
                (item.exploration_desk_title for item in tasks if item.exploration_layout),
                "Research Desk · 探索检索",
            )
            for item in tasks:
                identity = item.label or item.participant_id
                display_runtime = self._current_display_runtime(item)
                if item.label is None:
                    provider_id, model_id = display_runtime
                    if provider_id and model_id:
                        identity += f" · {provider_id}:{model_id}"
                    if item.persona is not None:
                        identity += f" · {persona_display_label(item.persona)}"
                state = (
                    item.initial_state
                    if item.initial_state in {"pending", "running", "completed", "failed"}
                    else "pending"
                )
                detail = item.initial_detail or (
                    "已恢复完整结果" if state == "completed" else "等待调用"
                )
                if (
                    state == "completed" and item.provider_id and item.model_id
                    and display_runtime != (item.provider_id, item.model_id)
                ):
                    detail += f" · 初始配置 {item.provider_id}:{item.model_id}"
                self._tasks[item.task_id] = _LiveTask(
                    item.task_id,
                    item.participant_id,
                    identity,
                    state=state,
                    detail=detail,
                    backend_warning=item.initial_backend_warning,
                    completion_requires_commit=item.completion_requires_commit,
                    desk_state=(item.initial_desk_state or
                                ("completed" if state == "completed" else "pending")),
                    desk_detail=(item.initial_desk_detail or
                                 ("已恢复检索结果" if state == "completed" else "尚未提问")),
                    label_locked=item.label is not None,
                    desk_auto_updates=item.exploration_auto_desk_updates,
                )
            self._render_live_table_locked(force=True)

    def _summarize_current_batch_locked(self) -> None:
        """Leave a durable, named outcome before a status or batch replaces the table."""
        if not self.live or not self._tasks or self._rendering_step is not None:
            return
        previous_tasks = list(self._tasks.values())
        completed = sum(task.state == "completed" for task in previous_tasks)
        failed = sum(task.state == "failed" for task in previous_tasks)
        title = self._batch_title or self._status_context or "当前任务"
        if self._exploration_layout:
            desk_completed = sum(task.desk_state == "completed" for task in previous_tasks)
            summary = (
                f"{title} · 已结束：代表已提交 {completed}/{len(previous_tasks)}，"
                f"Desk 已完成 {desk_completed}/{len(previous_tasks)}"
            )
        else:
            summary = f"{title} · 已结束：完成 {completed}/{len(previous_tasks)}"
        if failed:
            summary += f"，失败 {failed}"
        self._print_event_locked(
            "批次", summary,
            GREEN if completed == len(previous_tasks) and not failed and (
                not self._exploration_layout or desk_completed == len(previous_tasks)
            ) else YELLOW,
        )

    def _current_display_runtime(self, item: TaskProgressItem) -> tuple[str | None, str | None]:
        """Show the active replacement, not the frozen registry model, in new rows."""
        if self.meeting_root is None:
            return item.provider_id, item.model_id
        try:
            from project_ensemble.runtime.model_replacements import current_runtime_for
            from project_ensemble.storage.meeting import MeetingRepository

            return current_runtime_for(MeetingRepository(self.meeting_root), item.participant_id)
        except (OSError, ValueError, KeyError):
            return item.provider_id, item.model_id

    def task_started(self, task_id: str, detail: str = "正在运行") -> None:
        if not self.live:
            return
        with self._output_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            self._thread_task.task_id = task_id
            task.state = "running"
            task.detail = detail
            self._render_live_table_locked(force=True)

    def task_waiting(self, task_id: str, detail: str) -> None:
        """Mark a parked task without binding the scheduler thread to that task."""
        if not self.live:
            return
        with self._output_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.state = "pending"
            task.detail = detail
            self._render_live_table_locked(force=True)

    def task_backend_warning(self, task_id: str, reason: str) -> None:
        """Attach a backend diagnostic to one queued Research Desk question."""
        if not self.live:
            return
        with self._output_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.backend_warning = reason.strip().replace("\n", " ")[:500] or None
            self._render_live_table_locked(force=True)

    def task_finished(
        self, task_id: str, *, failed: bool = False, detail: str | None = None
    ) -> None:
        if not self.live:
            return
        with self._output_lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.state = "failed" if failed else "completed"
                task.detail = detail or ("失败" if failed else "已完成")
                self._render_live_table_locked(force=True)
            if getattr(self._thread_task, "task_id", None) == task_id:
                del self._thread_task.task_id

    def research_step(self, participant_id: str, detail: str, *, waiting: bool = False) -> None:
        """Update only the independent Desk question bound to this worker thread."""
        if not self.live:
            return
        with self._output_lock:
            task_id = getattr(self._thread_task, "task_id", None)
            task = self._tasks.get(task_id)
            if (task is None or not task.task_id.startswith("fast-desk-")
                    or task.participant_id != participant_id):
                return
            task.state = "pending" if waiting else "running"
            task.stage = "research"
            task.stream_state = None
            task.detail = detail
            self._render_live_table_locked(force=True)

    def research_backend_unavailable(self, participant_id: str, backend_id: str, reason: str) -> None:
        """Keep a backend failure attached to its question through later model steps."""
        if not self.live or backend_id != "openalex":
            return
        with self._output_lock:
            task_id = getattr(self._thread_task, "task_id", None)
            task = self._tasks.get(task_id)
            if (task is None or not task.task_id.startswith("fast-desk-")
                    or task.participant_id != participant_id):
                return
            task.backend_warning = reason.strip().replace("\n", " ")[:500]
            self._render_live_table_locked(force=True)

    def exploration_actor(self, participant_id: str) -> None:
        self._thread_task.exploration_actor = participant_id

    def exploration_desk(self, participant_id: str, state: str, detail: str) -> None:
        if not self.live:
            self._write("调研", f"{participant_id} · {detail}", CYAN)
            return
        with self._output_lock:
            task = next(
                (item for item in self._tasks.values() if item.participant_id == participant_id),
                None,
            )
            if task is None:
                return
            normalized_state = state if state in {"pending", "running", "completed", "failed"} else "running"
            if task.desk_state == normalized_state and task.desk_detail == detail:
                return
            task.desk_state = normalized_state
            task.desk_detail = detail
            self._render_live_table_locked(force=True)

    def _auto_exploration_desk_locked(self, participant_id: str) -> bool:
        task = next(
            (item for item in self._tasks.values() if item.participant_id == participant_id),
            None,
        )
        return task is not None and task.desk_auto_updates

    def _find_task_locked(
        self, participant_id: str, preferred_states: tuple[str, ...]
    ) -> _LiveTask | None:
        bound_id = getattr(self._thread_task, "task_id", None)
        if bound_id in self._tasks:
            return self._tasks[bound_id]
        matches = [
            task
            for task in self._tasks.values()
            if task.participant_id == participant_id
        ]
        for desired in preferred_states:
            for task in matches:
                if task.state == desired:
                    return task
        return matches[-1] if matches else None

    @staticmethod
    def _research_call_label(task: _LiveTask, stage: str) -> str | None:
        if not task.task_id.startswith("fast-desk-"):
            return None
        if stage == "research_claim_normalization":
            return "主张规范化中"
        if stage == "research_evidence_synthesis":
            return "证据综合中"
        if stage == "research_evidence_synthesis_source_review":
            return "补读后复核中"
        if stage.startswith("research_"):
            return "证据校验中"
        return None

    def _ensure_task_locked(
        self,
        participant_id: str,
        stage: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        persona: str | None = None,
    ) -> _LiveTask:
        if (
            self._exploration_layout
            and participant_id != "RESEARCH_DESK"
            and not any(task.participant_id == participant_id for task in self._tasks.values())
        ):
            # The four planning submissions have ended.  A following Chair
            # call is a new task, not a fifth exploratory Representative.
            self._clear_live_table_locked()
            self._tasks.clear()
            self._exploration_layout = False
            self._batch_title = "研究总纲 · 主席整理"
            self._live_note = None
        task = self._find_task_locked(
            participant_id, ("pending", "running", "completed")
        )
        if task is not None:
            return task
        task_id = f"{participant_id}:{stage}:{len(self._tasks)}"
        identity = participant_id
        if provider_id and model_id:
            identity += f" · {provider_id}:{model_id}"
        if persona is not None:
            identity += f" · {persona_display_label(persona)}"
        task = _LiveTask(task_id, participant_id, identity)
        self._tasks[task_id] = task
        return task

    def call_started(self, participant_id: str, stage: str) -> None:
        if not self.live:
            self._write("调用", f"{participant_id} 正在处理 {stage}", BLUE)
            return
        with self._output_lock:
            if self._exploration_layout and participant_id == "RESEARCH_DESK":
                actor = getattr(self._thread_task, "exploration_actor", None)
                if actor:
                    if self._auto_exploration_desk_locked(actor):
                        self.exploration_desk(actor, "running", "检索/综合中")
                    return
            task = self._ensure_task_locked(participant_id, stage)
            task.stage = stage
            task.state = "running"
            task.detail = self._research_call_label(task, stage) or "已调用；等待响应"
            self._render_live_table_locked(force=True)

    def call_started_with_runtime(
        self,
        participant_id: str,
        stage: str,
        provider_id: str,
        model_id: str,
        persona: str | None,
    ) -> None:
        if not self.live:
            identity = f"{participant_id} · {provider_id}:{model_id}"
            if persona is not None:
                identity += f" · {persona_display_label(persona)}"
            self._write("调用", f"{identity} · 正在处理 {stage}", BLUE)
            return
        with self._output_lock:
            if self._exploration_layout and participant_id == "RESEARCH_DESK":
                actor = getattr(self._thread_task, "exploration_actor", None)
                if actor:
                    if self._auto_exploration_desk_locked(actor):
                        self.exploration_desk(actor, "running", "检索/综合中")
                    return
            task = self._ensure_task_locked(
                participant_id, stage, provider_id, model_id, persona
            )
            # A row may have been reserved from a frozen registry before a
            # runtime replacement. The invocation reports the actual model.
            if not task.label_locked:
                task.identity = f"{participant_id} · {provider_id}:{model_id}"
                if persona is not None:
                    task.identity += f" · {persona_display_label(persona)}"
            task.stage = stage
            task.state = "running"
            task.detail = self._research_call_label(task, stage) or "已调用；等待响应"
            self._render_live_table_locked(force=True)

    def call_completed(self, participant_id: str, stage: str) -> None:
        if not self.live:
            self._write("完成", f"{participant_id} 已完成 {stage}", GREEN)
            return
        with self._output_lock:
            if self._exploration_layout and participant_id == "RESEARCH_DESK":
                actor = getattr(self._thread_task, "exploration_actor", None)
                if actor:
                    if self._auto_exploration_desk_locked(actor):
                        self.exploration_desk(actor, "running", "响应已返回；校验中")
                    return
            task = self._find_task_locked(
                participant_id, ("running", "pending", "completed")
            ) or self._ensure_task_locked(participant_id, stage)
            task.stage = stage
            drafting_artifact_pending = (
                self._rendering_step is not None
                and self._rendering_step.kind == "literature"
                and self._rendering_step.stage == "draft"
                and self._status_context is not None
                and "主笔撰写" in self._status_context
            )
            if task.completion_requires_commit or drafting_artifact_pending:
                task.state = "running"
                task.detail = (
                    "模型响应已返回；初稿及检索结果尚未完整落盘"
                    if drafting_artifact_pending else
                    "模型响应已返回；校验中"
                    if task.task_id.startswith("fast-desk-") else
                    "提问已返回；继续探索/规划"
                    if stage.startswith("research_planning_exploration") else
                    "方案已返回；校验/提交中"
                )
            else:
                task.state = "completed"
                task.detail = "已完成"
            self._render_live_table_locked(force=True)

    def artifact_committed(self, participant_id: str, detail: str) -> None:
        """Mark a multi-call draft batch green only after its artifact is frozen."""
        if not self.live:
            return
        with self._output_lock:
            if self._rendering_step is None or self._rendering_step.stage != "draft":
                return
            if participant_id != "WRITER":
                return
            for task in self._tasks.values():
                if task.state != "failed":
                    task.state = "completed"
                    task.detail = detail
            self._render_live_table_locked(force=True)

    def stream_progress(
        self,
        participant_id: str,
        stage: str,
        state: str,
        reasoning_chars: int,
        content_chars: int,
    ) -> None:
        """Use provider streaming only as a liveness signal; never print fragments."""

        if not self.live:
            return
        with self._output_lock:
            if self._exploration_layout and participant_id == "RESEARCH_DESK":
                actor = getattr(self._thread_task, "exploration_actor", None)
                if actor:
                    if self._auto_exploration_desk_locked(actor):
                        self.exploration_desk(actor, "running", "正在接收检索响应")
                    return
            task = self._find_task_locked(
                participant_id, ("running", "pending", "completed")
            ) or self._ensure_task_locked(participant_id, stage)
            state_changed = task.stream_state != state
            task.stage = stage
            task.reasoning_chars = reasoning_chars
            task.content_chars = content_chars
            task.stream_state = state
            phase = self._research_call_label(task, stage)
            if state == "connected":
                task.detail = (phase + "；等待响应") if phase else "连接成功；等待首个响应"
            elif state == "reasoning":
                task.detail = f"{phase + ' · ' if phase else '生成中 · '}推理 {reasoning_chars:,} 字符"
            elif state == "content":
                task.detail = f"{phase + ' · ' if phase else '生成中 · '}正文 {content_chars:,} 字符"
            elif state == "done":
                task.detail = f"响应完整 · 正文 {content_chars:,} 字符"
            elif state == "unsupported":
                task.detail = "等待完整响应（无流式进度）"
            else:
                task.detail = f"响应活动 · 已接收 {reasoning_chars + content_chars:,} 字符"
            self._render_live_table_locked(
                force=state_changed or state in {"connected", "done", "unsupported"}
            )

    def token_usage(self, telemetry: TokenTelemetry) -> None:
        def token_amount(value: int | None) -> str:
            return f"{value:,} tokens" if value is not None else "未报告"

        def token_count(value: int | None) -> str:
            return f"{value:,}" if value is not None else "未报告"

        rate = (
            f"{telemetry.cache_hit_rate:.1%}"
            if telemetry.cache_hit_rate is not None
            else "未报告"
        )
        if self.live:
            with self._output_lock:
                if self._exploration_layout and telemetry.participant_id == "RESEARCH_DESK":
                    return
                task = self._find_task_locked(
                    telemetry.participant_id, ("completed", "running", "pending")
                )
                if task is not None:
                    usage_detail = (
                        f"上传 {token_count(telemetry.prompt_tokens)}"
                        f" · 生成 {token_count(telemetry.completion_tokens)}"
                        f" · 缓存命中 {rate}"
                    )
                    phase = self._research_call_label(task, telemetry.stage)
                    task.detail = f"{phase} · {usage_detail}" if phase else usage_detail
                    self._render_live_table_locked(force=True)
                    return
        completion = token_amount(telemetry.completion_tokens)
        if telemetry.reasoning_tokens is not None:
            completion += f"（其中推理 {telemetry.reasoning_tokens:,} tokens）"
        with self._output_lock:
            self._clear_live_table_locked()
            self._print_event_locked(
                "用量",
                f"{telemetry.participant_id} · 上传 {token_amount(telemetry.prompt_tokens)}；"
                f"生成 {completion}；总计 {token_amount(telemetry.total_tokens)}",
                DIM,
            )
            cache_line = (
                f"           缓存 · 命中 {token_amount(telemetry.cached_tokens)}；未命中 "
                f"{token_amount(telemetry.cache_miss_tokens)}；缓存命中率 {rate}"
            )
            print(styled(cache_line, DIM, enabled=self.color), file=self.output, flush=True)
            self._render_live_table_locked(force=True)

    def retrying(
        self,
        participant_id: str,
        retry_number: int,
        maximum_retries: int,
        delay_seconds: float,
        error_type: str,
    ) -> None:
        message = (
            f"{participant_id} 瞬时失败 {error_type}；等待 {delay_seconds:g} 秒后"
            f"进行第 {retry_number}/{maximum_retries} 次重试"
        )
        if self.live:
            with self._output_lock:
                task = self._find_task_locked(
                    participant_id, ("running", "pending", "completed")
                )
                if task is not None:
                    task.state = "running"
                    task.detail = (
                        f"重试 {retry_number}/{maximum_retries}"
                        f" · 等待 {delay_seconds:g} 秒"
                    )
                    self._render_live_table_locked(force=True)
                    return
        self._write("重试", message, YELLOW)

    def speech(self, participant_id: str, label: str, content: str) -> None:
        with self._output_lock:
            self._clear_live_table_locked()
            heading = f"┌─ [{self._timestamp()}] 发言 · {participant_id} · {label} "
            remaining = max(1, self.width - _display_width(heading))
            print(
                styled(
                    heading + "─" * remaining,
                    BOLD,
                    MAGENTA,
                    enabled=self.color,
                ),
                file=self.output,
                flush=True,
            )
            for line in content.rstrip().splitlines() or [""]:
                print(f"│  {line}", file=self.output, flush=True)
            print(
                styled(
                    "└" + "─" * (self.width - 1),
                    MAGENTA,
                    enabled=self.color,
                ),
                file=self.output,
                flush=True,
            )
            self._render_live_table_locked(force=True)

    def info(self, message: str) -> None:
        if self.live and self._tasks:
            with self._output_lock:
                self._live_note = message
                self._render_live_table_locked(force=True)
            return
        self._write("信息", message, CYAN)

    def chair_ruling_notice(self, message: str, *, failed: bool = False) -> None:
        """Print a complete Chair ruling without writing through the live table.

        Raw CLI prints while a table is visible move the cursor beyond its
        tracked height, so the next redraw clears the ruling instead of the
        old table and leaves repeated chapter headers behind.
        """
        self._write("警告" if failed else "代裁", message, RED if failed else LIGHT_GRAY)

    def paused(self, reason_code: str, participant_id: str) -> None:
        with self._output_lock:
            if self.live:
                task = self._find_task_locked(
                    participant_id, ("running", "pending", "completed")
                )
                if task is not None:
                    task.state = "failed"
                    task.detail = reason_code
            self._clear_live_table_locked()
            self._rule_locked(RED)
            self._print_event_locked(
                "暂停", f"{reason_code} · participant={participant_id}", BOLD, RED
            )
            self._render_live_table_locked(force=True)
