import io
import json
import os
import sys
import termios
import time

import pytest

from project_ensemble.domain import MeetingPhase
from project_ensemble.errors import ModelReplacementRequested
from project_ensemble.runtime.progress import ConsoleProgressReporter, TaskProgressItem
from project_ensemble.runtime.progress import _display_width
from project_ensemble.runtime.telemetry import TokenTelemetry
from project_ensemble.user_settings import save_interface_language


def test_fast_workflow_nests_module_stages_under_active_major_step(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_interface_language("zh")
    progress = ConsoleProgressReporter(io.StringIO(), color=False, live=True)
    monkeypatch.setattr(progress, "_terminal_height", lambda: 50)
    progress.fast_workflow_stage(4)
    progress.literature_step(
        section_id="RM-03", section_index=3, section_total=6,
        title="临床证据", stage="draft", detail="主笔第 1 稿",
    )
    progress.task_batch_started([
        TaskProgressItem(task_id="writer", participant_id="WRITER"),
    ], title="主笔撰写")
    rendered = "\n".join(progress._table_lines_locked())
    assert rendered.index("● 4/5") < rendered.index("模块 3/6")
    assert rendered.index("模块 3/6") < rendered.index("写作提纲")
    assert rendered.index("写作提纲") < rendered.index("○ 5/5")
    assert "✓ RM-01" in rendered and "○ RM-04" in rendered


def test_deliberation_and_rendering_share_major_step_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_interface_language("zh")
    for phase, expected in (
        (MeetingPhase.BALLOT, "● 3/5 · 表决"),
        (MeetingPhase.SCHOLARLY_SCIENCE_REVIEW, "● 3/5 · 科学事实审阅"),
    ):
        progress = ConsoleProgressReporter(io.StringIO(), color=False, live=True)
        monkeypatch.setattr(progress, "_terminal_height", lambda: 50)
        progress.status(phase, "当前任务")
        progress.task_batch_started([
            TaskProgressItem(task_id="one", participant_id="R-TEST"),
        ])
        assert expected in "\n".join(progress._table_lines_locked())


def test_progress_chrome_uses_english_without_translating_meeting_content(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_interface_language("en")
    output = io.StringIO()
    progress = ConsoleProgressReporter(output, color=False, live=True)
    progress.task_batch_started([TaskProgressItem(
        task_id="one", participant_id="R-TEST", label="原始会议文本",
    )])
    rendered = output.getvalue()
    assert "Completed 0/1" in rendered
    assert "Task" in rendered and "Status" in rendered
    assert "Keys: Ctrl+C stop safely" in rendered
    assert "原始会议文本" in rendered


def test_consultation_display_does_not_repaint_stale_model_batch():
    output = io.StringIO()
    progress = ConsoleProgressReporter(output, color=False, live=True)
    progress.task_batch_started([TaskProgressItem(task_id="one", participant_id="WRITER")])
    with progress.consultation_display():
        boundary = len(output.getvalue())
        progress.info("模型补充状态")
        progress.task_finished("one")
        assert len(output.getvalue()) == boundary
    assert progress._rendered_table_lines == 0
    assert "模型补充状态" not in output.getvalue()[boundary:]


def test_live_task_uses_current_replacement_model_not_frozen_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "project_ensemble.runtime.model_replacements.current_runtime_for",
        lambda _repo, _participant_id: ("codex", "gpt-6-luna"),
    )
    progress = ConsoleProgressReporter(io.StringIO(), color=False, live=True, meeting_root=tmp_path)
    progress.task_batch_started([TaskProgressItem(
        task_id="one", participant_id="R-TEST", provider_id="codex",
        model_id="gpt-6-sol", persona="librarian", initial_state="completed",
    )])
    assert "codex:gpt-6-luna" in progress._tasks["one"].identity
    assert "codex:gpt-6-sol" not in progress._tasks["one"].identity
    assert "初始配置 codex:gpt-6-sol" in progress._tasks["one"].detail
    progress.call_started_with_runtime(
        "R-TEST", "research_question", "codex", "gpt-6-sol", "librarian"
    )
    assert "codex:gpt-6-sol" in progress._tasks["one"].identity


def test_live_footer_counts_unique_literature_and_web_sources(tmp_path):
    records = tmp_path / "public/research/literature_bundle/records"
    records.mkdir(parents=True)
    for name, source_id, source_type in (
        ("a", "https://openalex.org/W123", "article"),
        ("b", "https://openalex.org/W123", "article"),
        ("c", "https://example.org/page", "web_page"),
    ):
        (records / f"{name}.json").write_text(json.dumps({
            "source_id": source_id, "source_type": source_type,
        }), encoding="utf-8")
    progress = ConsoleProgressReporter(io.StringIO(), color=False, live=True, meeting_root=tmp_path)
    progress.task_batch_started([TaskProgressItem(
        task_id="one", participant_id="R-TEST", label="阅读",
    )])
    assert "已查阅来源：文献 1 · 网页 1" in progress.output.getvalue()


def test_console_progress_shows_status_calls_and_speech_content():
    output = io.StringIO()
    progress = ConsoleProgressReporter(output)
    progress.status(MeetingPhase.INITIAL_DRAFT, "正在起草")
    progress.call_started_with_runtime(
        "R-TEST",
        "initial_draft",
        "glm",
        "glm-5.3-flash",
        "pragmatic_minimalist",
    )
    progress.stream_progress("R-TEST", "initial_draft", "connected", 0, 0)
    progress.stream_progress("R-TEST", "initial_draft", "reasoning", 1234, 0)
    progress.stream_progress("R-TEST", "initial_draft", "content", 1234, 567)
    progress.stream_progress("R-TEST", "initial_draft", "done", 1234, 567)
    progress.call_completed("R-TEST", "initial_draft")
    progress.token_usage(
        TokenTelemetry(
            exchange_id="X-TEST",
            participant_id="R-TEST",
            provider_id="fake",
            model_id="m",
            stage="initial_draft",
            prompt_tokens=100,
            cached_tokens=25,
            cache_miss_tokens=75,
            completion_tokens=40,
            reasoning_tokens=10,
            total_tokens=140,
            cache_hit_rate=0.25,
        )
    )
    progress.retrying("R-TEST", 1, 3, 30, "TransientProviderError")
    progress.speech("R-TEST", "D0 初始草案", "第一行\n第二行")
    progress.info("窗口已冻结")
    progress.paused("NEEDS_HUMAN", "R-TEST")

    rendered = output.getvalue()
    assert "状态" in rendered and "INITIAL_DRAFT" in rendered
    assert "调用" in rendered and "完成" in rendered
    assert "流式连接" not in rendered
    assert "隐藏推理流正在增长" not in rendered
    assert "最终正文流正在增长" not in rendered
    assert "响应流结束" not in rendered
    assert "glm:glm-5.3-flash" in rendered and "Monitor / 监管者" in rendered
    assert "用量" in rendered and "缓存命中率 25.0%" in rendered
    assert "上传 100 tokens" in rendered and "生成 40 tokens" in rendered
    assert "命中 25 tokens" in rendered
    assert "重试" in rendered and "1/3" in rendered and "30 秒" in rendered
    assert "发言" in rendered and "第一行" in rendered and "第二行" in rendered
    assert "窗口已冻结" in rendered
    assert "暂停" in rendered and "NEEDS_HUMAN" in rendered
    assert "─" in rendered and "┌─" in rendered and "└" in rendered
    assert "\x1b[" not in rendered


def test_console_progress_uses_color_only_when_enabled():
    output = io.StringIO()
    progress = ConsoleProgressReporter(output, color=True)
    progress.status(MeetingPhase.INITIAL_DRAFT, "正在起草")
    progress.call_completed("R-TEST", "initial_draft")
    progress.paused("NEEDS_HUMAN", "R-TEST")

    rendered = output.getvalue()
    assert "\x1b[36m" in rendered
    assert "\x1b[32m" in rendered
    assert "\x1b[31m" in rendered


class _TTYBuffer(io.StringIO):
    def isatty(self):
        return True


def test_exploration_two_columns_waits_for_durable_plan_before_green():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False, live=True)
    progress.task_batch_started(
        [TaskProgressItem(
            task_id="R-ONE:0", participant_id="R-ONE", provider_id="glm", model_id="m",
            completion_requires_commit=True, exploration_layout=True,
        )],
        title="研究规划",
    )
    progress.exploration_actor("R-ONE")
    progress.call_started_with_runtime("R-ONE", "research_planning_exploration", "glm", "m", None)
    progress.call_completed("R-ONE", "research_planning_exploration")
    assert progress._tasks["R-ONE:0"].state == "running"
    progress.call_started_with_runtime("RESEARCH_DESK", "research_exploration", "deepseek", "m", None)
    progress.stream_progress("RESEARCH_DESK", "research_exploration", "reasoning", 100, 0)
    progress.call_completed("RESEARCH_DESK", "research_exploration")
    assert len(progress._tasks) == 1
    assert progress._tasks["R-ONE:0"].desk_state == "running"
    progress.exploration_desk("R-ONE", "completed", "检索结果已提交")
    assert progress._tasks["R-ONE:0"].state == "running"
    progress.call_started_with_runtime("R-ONE", "research_decomposition", "glm", "m", None)
    progress.call_completed("R-ONE", "research_decomposition")
    assert progress._tasks["R-ONE:0"].state == "running"
    progress.task_finished("R-ONE:0", detail="方案已校验并提交")
    assert progress._tasks["R-ONE:0"].state == "completed"
    assert "代表 · 规划方案" in output.getvalue()
    assert "Research Desk · 探索检索" in output.getvalue()


def test_chair_followup_does_not_inherit_representative_desk_column():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    progress.task_batch_started(
        [TaskProgressItem(
            task_id="R-ONE:0", participant_id="R-ONE",
            completion_requires_commit=True, exploration_layout=True,
            initial_state="completed",
        )],
        title="研究规划",
    )
    progress.call_started_with_runtime("CHAIR", "research_outline_clustering", "glm", "m", None)
    assert not progress._exploration_layout
    assert [task.participant_id for task in progress._tasks.values()] == ["CHAIR"]


def test_live_shortcut_hint_is_only_in_refreshable_table_footer():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    progress.status(MeetingPhase.INITIAL_DRAFT, "正在起草")
    progress.status(MeetingPhase.GENERAL_POSITION, "进入立场窗口")
    assert "快捷键：Ctrl+C" not in output.getvalue()

    progress.task_batch_started(
        [TaskProgressItem(task_id="draft", participant_id="CHAIR")],
        title="初稿起草",
    )
    assert output.getvalue().count("快捷键：Ctrl+C") == 1
    progress.task_started("draft")
    assert sum("快捷键：Ctrl+C" in line for line in progress._table_lines_locked()) == 1


def test_live_progress_uses_one_refreshable_task_table_without_stream_fragments():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    progress.task_batch_started(
        [
            TaskProgressItem(
                task_id="R-A",
                participant_id="R-A",
                provider_id="gemini",
                model_id="flash",
                persona="systems_integrator",
            ),
            TaskProgressItem(
                task_id="R-B",
                participant_id="R-B",
                provider_id="glm",
                model_id="flash",
                persona="librarian",
            ),
        ],
        title="总则第 2/3 轮 · 独立立场提交",
    )
    progress.call_started_with_runtime(
        "R-A", "general_position", "gemini", "flash", "systems_integrator"
    )
    progress.stream_progress("R-A", "general_position", "reasoning", 1200, 0)
    progress.stream_progress("R-A", "general_position", "content", 1200, 80)
    progress.call_completed("R-A", "general_position")
    progress.info("已收到 1/2 份立场；正文继续密封")

    rendered = output.getvalue()
    assert "总则第 2/3 轮 · 独立立场提交 · 完成 1/2" in rendered
    assert "R-A · gemini:flash · Builder / 建构者" in rendered
    assert "R-B · glm:flash · Librarian / 智库长" in rendered
    assert "生成中 · 推理 1,200 字符" in rendered
    assert "生成中 · 正文 80 字符" in rendered
    assert "信息：已收到 1/2 份立场；正文继续密封" in rendered
    assert "\x1b[1A\x1b[2K\r" in rendered
    assert "] 调用" not in rendered
    assert "] 完成" not in rendered


def test_live_progress_renders_restored_results_as_completed():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    progress.task_batch_started(
        [
            TaskProgressItem(
                task_id="R-A:0",
                participant_id="R-A",
                initial_state="completed",
                initial_detail="已恢复完整结果",
            ),
            TaskProgressItem(task_id="R-B:1", participant_id="R-B"),
        ],
        title="恢复中的密封批次",
    )

    assert progress._tasks["R-A:0"].state == "completed"
    assert progress._tasks["R-B:1"].state == "pending"
    rendered = output.getvalue()
    assert "恢复中的密封批次 · 完成 1/2" in rendered
    assert "✓ R-A" in rendered
    assert "已恢复完整结果" in rendered


def test_live_workload_and_complete_token_usage_remain_visible_across_batches():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    progress.set_workload("原文块 1–4/399；重绘章节 1/61")
    progress.task_batch_started(
        [TaskProgressItem(task_id="chair", participant_id="CHAIR")],
        title="SR-001 · 主席重绘",
    )
    progress.call_completed("CHAIR", "scholarly_redraw_SR-001")
    progress.token_usage(
        TokenTelemetry(
            exchange_id="X-RENDER",
            participant_id="CHAIR",
            provider_id="fake",
            model_id="m",
            stage="scholarly_redraw_SR-001",
            prompt_tokens=12345,
            cached_tokens=10000,
            cache_miss_tokens=2345,
            completion_tokens=678,
            reasoning_tokens=100,
            total_tokens=13023,
            cache_hit_rate=10000 / 12345,
        )
    )
    table = "\n".join(progress._table_lines_locked())
    assert "原文块 1–4/399；重绘章节 1/61" in table
    assert "上传 12,345" in table
    assert "生成 678" in table
    assert "缓存命中 81.0%" in table
    assert progress._tasks["chair"].detail == "上传 12,345 · 生成 678 · 缓存命中 81.0%"
    progress.task_batch_started(
        [TaskProgressItem(task_id="review", participant_id="R-TEST")],
        title="SR-001 · 科学审阅",
    )
    assert "原文块 1–4/399；重绘章节 1/61" in "\n".join(progress._table_lines_locked())


def test_rendering_progress_groups_section_stage_and_round_without_status_stream():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    progress.rendering_step(
        section_id="SR-017", section_index=17, section_total=61,
        title="递送系统", stage="science", detail="第 2 次修订 · 第 1/2 轮审阅",
    )
    progress.status(MeetingPhase.SCHOLARLY_SCIENCE_REVIEW, "旧式状态消息")
    progress.task_batch_started(
        [TaskProgressItem(task_id="R-A", participant_id="R-A")],
        title="科学事实核校",
    )
    table = "\n".join(progress._table_lines_locked())
    assert "第 17/61 个重绘章节 · SR-017 · 递送系统" in table
    assert "✓ 重绘撰写" in table
    assert "● 科学性审阅" in table
    assert "└─ 第 2 次修订 · 第 1/2 轮审阅" in table
    assert "○ 引文审阅" in table
    assert "科学事实核校 · 完成 0/1" in table
    assert "旧式状态消息" not in output.getvalue()
    progress.rendering_step(
        section_id="SR-017", section_index=17, section_total=61,
        title="递送系统", stage="citation", detail="第 1/2 轮审阅",
    )
    assert "✓ 科学性审阅" in "\n".join(progress._table_lines_locked())
    assert progress._tasks == {}


def test_rendering_progress_non_tty_keeps_one_compact_stage_record():
    output = io.StringIO()
    progress = ConsoleProgressReporter(output, color=False, live=False)
    progress.rendering_step(
        section_id="SR-002", section_index=2, section_total=8,
        title="研究范围", stage="citation", detail="第 2/2 轮审阅",
    )
    progress.status(MeetingPhase.SCHOLARLY_CITATION_REVIEW, "旧式状态消息")
    assert "章节 2/8 · SR-002 · 研究范围 / 引文审阅 / 第 2/2 轮审阅" in output.getvalue()
    assert "旧式状态消息" not in output.getvalue()


def test_v071_literature_progress_groups_module_stage_and_round():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    progress.literature_step(
        section_id="RM-03", section_index=3, section_total=9,
        title="力学路线", stage="science", detail="第 2/2 轮 · 智库长科学清单",
    )
    progress.status(MeetingPhase.LITERATURE_MODULE_REVIEW, "旧式状态消息")
    progress.task_batch_started(
        [TaskProgressItem(task_id="R-A", participant_id="R-A")],
        title="本轮智库长审阅",
    )
    table = "\n".join(progress._table_lines_locked())
    assert "第 3/9 个研究模块 · RM-03 · 力学路线" in table
    assert "✓ 写作提纲" in table
    assert "✓ 学术主笔" in table
    assert "● 科学性审阅" in table
    assert "○ 引文与组装" in table
    assert "第 2/2 轮" in table
    assert "旧式状态消息" not in output.getvalue()


def test_fast_module_planning_uses_one_live_progress_tree():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False, live=True)
    progress.fast_planning_step(
        section_id="RM-03", section_index=3, section_total=6,
        title="界面定义", detail="步骤 2/5 · 主笔制定执行单",
    )
    progress.task_batch_started(
        [TaskProgressItem(task_id="fast-plan-RM-03", participant_id="WRITER")],
        title="RM-03 · 主笔制定模块执行单",
    )
    progress.task_finished("fast-plan-RM-03", detail="执行单与范围决定已冻结")
    lines = "\n".join(progress._table_lines_locked())
    assert "第 3/6 个研究模块 · RM-03 · 界面定义" in lines
    assert "制定模块执行单" in lines
    assert "✓ WRITER" in lines
    assert "批次" not in output.getvalue()


def test_fast_research_questions_and_desk_share_tree_layout():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False, live=True)
    progress.fast_research_step(
        section_id="RM-03", section_index=3, section_total=6,
        title="界面定义", stage="queries", detail="步骤 3/5 · 第 1/3 轮",
    )
    progress.task_batch_started(
        [TaskProgressItem(task_id="query", participant_id="WRITER")],
        title="RM-03 · 提交待核查问题",
    )
    query_lines = "\n".join(progress._table_lines_locked())
    assert "第 3/6 个研究模块 · RM-03 · 界面定义" in query_lines
    assert "● 主笔确定问题" in query_lines
    assert "○ Research Desk 核查证据" in query_lines
    progress.task_finished("query", detail="问题已提交")
    progress.fast_research_step(
        section_id="ROUND-1", section_index=1, section_total=3,
        title="跨 6 个模块 · 20 个独立问题", stage="desk",
        detail="步骤 3/5 · 并行核查证据",
    )
    desk_lines = "\n".join(progress._table_lines_locked())
    assert "第 1/3 轮资料核查 · 跨 6 个模块 · 20 个独立问题" in desk_lines
    assert "✓ 主笔确定问题" in desk_lines
    assert "● Research Desk 核查证据" in desk_lines
    assert "批次" not in output.getvalue()


def test_fast_desk_distinguishes_model_queue_from_processing_and_packet_commit():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    progress.task_batch_started([
        TaskProgressItem(task_id=f"fast-desk-RM-01-1-{index}",
                         participant_id="RESEARCH_DESK",
                         completion_requires_commit=True)
        for index in range(1, 4)
    ], title="证据核查")
    first = "fast-desk-RM-01-1-1"
    progress.task_started(first, "已分配")
    progress.research_step("RESEARCH_DESK", "等待模型名额 · 主张规范化", waiting=True)
    table = "\n".join(progress._table_lines_locked())
    assert "证据包已落盘 0/3 · 处理 0 · 等模型 1 · 等 OpenAlex 0 · 限速等待 0 · 未启动 2" in table
    assert progress._tasks[first].state == "pending"
    progress.call_started_with_runtime(
        "RESEARCH_DESK", "research_claim_normalization", "deepseek", "flash", None,
    )
    assert progress._tasks[first].state == "running"
    assert "主张规范化中" in progress._tasks[first].detail
    progress.research_step("RESEARCH_DESK", "规范化完成；正在检索外部来源")
    assert "检索外部来源" in progress._tasks[first].detail
    progress.research_step("RESEARCH_DESK", "等待模型名额 · 证据综合", waiting=True)
    assert progress._tasks[first].state == "pending"
    progress.call_started_with_runtime(
        "RESEARCH_DESK", "research_evidence_synthesis", "deepseek", "flash", None,
    )
    assert "证据综合中" in progress._tasks[first].detail
    progress.research_step("RESEARCH_DESK", "校验完成；正在归档证据包")
    progress.task_finished(first, detail="证据包已落盘")
    assert "证据包已落盘 1/3" in "\n".join(progress._table_lines_locked())


def test_fast_desk_keeps_openalex_failure_under_its_own_question_after_completion():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    first = "fast-desk-RM-01-1-1"
    second = "fast-desk-RM-01-1-2"
    progress.task_batch_started([
        TaskProgressItem(task_id=first, participant_id="RESEARCH_DESK", label="Desk · 问题 1"),
        TaskProgressItem(task_id=second, participant_id="RESEARCH_DESK", label="Desk · 问题 2"),
    ], title="证据核查")
    progress.task_started(first, "正在检索")
    progress.research_backend_unavailable(
        "RESEARCH_DESK", "openalex", "HTTP 429; daily_remaining=0; reset_seconds=120",
    )
    progress.task_finished(first, detail="证据包已落盘；Tavily 降级完成")
    lines = "\n".join(progress._table_lines_locked())
    assert "OpenAlex HTTP 429；当日剩余额度 0 credits；当日额度重置倒计时 120 秒" in lines
    assert "Desk · 问题 1" in lines
    assert progress._tasks[first].backend_warning is not None
    assert progress._tasks[second].backend_warning is None


def test_openalex_unknown_429_warning_does_not_print_unreported_quota_as_a_measurement():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    task_id = "fast-desk-RM-02-1-1"
    progress.task_batch_started([
        TaskProgressItem(task_id=task_id, participant_id="RESEARCH_DESK"),
    ], title="证据核查")
    progress.task_started(task_id, "证据综合")
    progress.research_backend_unavailable(
        "RESEARCH_DESK", "openalex",
        "HTTP 429; daily_remaining=unreported; reset_seconds=unreported",
    )
    lines = "\n".join(progress._table_lines_locked())
    assert "无法判断限流类型" in lines
    assert "unreported" not in lines


def test_openalex_probe_balance_is_shown_as_not_daily_exhaustion():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    task_id = "fast-desk-RM-02-1-1"
    progress.task_batch_started([
        TaskProgressItem(task_id=task_id, participant_id="RESEARCH_DESK"),
    ], title="证据核查")
    progress.task_started(task_id, "证据综合")
    progress.research_backend_unavailable(
        "RESEARCH_DESK", "openalex",
        "HTTP 429; daily_remaining=8760; daily_limit=10000; "
        "reset_seconds=80000; rate_limit_kind=NOT_DAILY",
    )
    lines = "\n".join(progress._table_lines_locked())
    assert "当日余额 8760/10000 credits" in lines
    assert "并非日额度耗尽" in lines


def test_queued_openalex_retry_shows_official_balance_on_the_affected_question():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    task_id = "fast-desk-RM-02-1-1"
    progress.task_batch_started([
        TaskProgressItem(task_id=task_id, participant_id="RESEARCH_DESK"),
    ], title="证据核查")
    progress.task_waiting(task_id, "检索后端限速；约 2 秒后重试（1/5）")
    progress.task_backend_warning(
        task_id,
        "OpenAlex retrieval failed: HTTP 429; daily_remaining=8760; "
        "daily_limit=10000; reset_seconds=80000; rate_limit_kind=NOT_DAILY",
    )
    assert "当日余额 8760/10000 credits" in "\n".join(progress._table_lines_locked())
    assert progress._tasks[task_id].state == "pending"


def test_openalex_reset_wait_is_not_counted_as_unstarted_work():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    task_id = "fast-desk-RM-01-1-1"
    progress.task_batch_started([
        TaskProgressItem(task_id=task_id, participant_id="RESEARCH_DESK"),
    ], title="证据核查")
    progress.task_waiting(task_id, "OpenAlex 当日额度耗尽；约 60 秒后重试")
    table = "\n".join(progress._table_lines_locked())
    assert "等 OpenAlex 1 · 限速等待 0 · 未启动 0" in table
    assert progress._tasks[task_id].state == "pending"


def test_live_progress_marks_failed_task_red():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=True)
    progress.task_batch_started(
        [TaskProgressItem(task_id="R-A", participant_id="R-A")]
    )
    progress.call_started("R-A", "ballot")
    progress.paused("PROVIDER_CALL_FAILED", "R-A")

    rendered = output.getvalue()
    assert "PROVIDER_CALL_FAILED" in rendered
    assert "\x1b[31m" in rendered


def test_chair_ruling_notice_clears_live_table_before_printing_complete_rationale():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=True)
    progress.rendering_step(
        section_id="SR-059", section_index=59, section_total=61,
        title="知识状态附录", stage="science", detail="主席逐项代裁",
    )
    progress.task_batch_started(
        [TaskProgressItem(task_id="chair", participant_id="CHAIR")],
        title="科学事实异议代裁",
    )
    assert any("\x1b[90m" in line and "○ CHAIR" in line
               for line in progress._table_lines_locked())
    progress.call_started("CHAIR", "delegated_science_consultation")
    before = output.getvalue()
    rationale = "这是一条完整的主席裁决理由，不能被进度表截短。"
    progress.chair_ruling_notice(f"HC-SR-059：{rationale}")
    after = output.getvalue()[len(before):]

    assert after.startswith("\x1b[1A\x1b[2K\r")
    assert rationale in after
    assert "\x1b[90m" in after
    assert after.index(rationale) < after.rindex("第 59/61 个重绘章节")


def test_live_rendering_table_reserves_terminal_column_and_never_scrolls_top(monkeypatch):
    monkeypatch.setattr("project_ensemble.runtime.progress.rule_width", lambda *_args: 80)
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    monkeypatch.setattr(progress, "_terminal_height", lambda: 14)
    progress.rendering_step(
        section_id="SR-059", section_index=59, section_total=61,
        title="知识状态附录", stage="science", detail="主席逐项代裁",
    )
    progress.task_batch_started(
        [TaskProgressItem(task_id=f"R-{number}", participant_id=f"R-{number}")
         for number in range(4)],
        title="科学事实异议代裁",
    )
    lines = progress._table_lines_locked()
    assert progress.width == 79
    assert len(lines) <= 12  # 14-row terminal minus the two-row safety margin.
    assert all(_display_width(line) <= 79 for line in lines)

    monkeypatch.setattr("project_ensemble.runtime.progress.rule_width", lambda *_args: 60)
    progress.task_started("R-0")
    assert progress.width == 59
    assert all(_display_width(line) <= 59 for line in progress._table_lines_locked())


def test_live_progress_binds_concurrent_shared_participant_to_exact_task():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    progress.task_batch_started(
        [
            TaskProgressItem(
                task_id="G-1", participant_id="RESEARCH_DESK", label="G-1 · 文献调研"
            ),
            TaskProgressItem(
                task_id="G-2", participant_id="RESEARCH_DESK", label="G-2 · 文献调研"
            ),
        ]
    )
    progress.task_started("G-2", "检索、筛选与证据综合")
    progress.call_started("RESEARCH_DESK", "research_evidence_synthesis")
    progress.stream_progress(
        "RESEARCH_DESK", "research_evidence_synthesis", "content", 100, 40
    )
    progress.call_completed("RESEARCH_DESK", "research_evidence_synthesis")
    progress.task_finished("G-2", detail="STAGED_PACKET")

    assert progress._tasks["G-1"].state == "pending"
    assert progress._tasks["G-2"].state == "completed"
    assert progress._tasks["G-2"].detail == "STAGED_PACKET"


def test_live_progress_keeps_compact_summary_when_batch_is_replaced():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    progress.task_batch_started(
        [TaskProgressItem(task_id="R-A", participant_id="R-A")],
        title="RM-01 · v1 · 三办公室审阅与主席整合",
    )
    progress.call_started("R-A", "literature_module_specialist_review")
    progress.call_completed("R-A", "literature_module_specialist_review")
    progress.task_batch_started(
        [TaskProgressItem(task_id="R-B", participant_id="R-B")],
        title="RM-01 · round1 · 全体代表正式审阅",
    )

    rendered = output.getvalue()
    assert "批次" in rendered
    assert "RM-01 · v1 · 三办公室审阅与主席整合 · 已结束：完成 1/1" in rendered
    assert "RM-01 · round1 · 全体代表正式审阅 · 完成 0/1" in rendered


def test_status_context_names_unreserved_call_and_retains_completion_on_transition():
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False, live=True)
    progress.status(MeetingPhase.LITERATURE_MODULE_RESEARCH, "模块 2/5 · 第 1/3 轮 · 主笔提问")
    progress.call_started_with_runtime("WRITER", "fast_research_queries", "codex", "gpt-6-sol", None)
    assert "模块 2/5 · 第 1/3 轮 · 主笔提问" in "\n".join(progress._table_lines_locked())
    progress.call_completed("WRITER", "fast_research_queries")
    before = output.getvalue()
    progress.status(MeetingPhase.LITERATURE_MODULE_RESEARCH, "模块 2/5 · 第 1/3 轮 · 证据核查")
    after = output.getvalue()
    assert "模块 2/5 · 第 1/3 轮 · 主笔提问 · 已结束：完成 1/1" in after
    assert len(after) > len(before)
    unchanged = output.getvalue()
    progress.status(MeetingPhase.LITERATURE_MODULE_RESEARCH, "模块 2/5 · 第 1/3 轮 · 证据核查")
    assert output.getvalue() == unchanged


def test_reserved_research_question_keeps_its_label_during_provider_call():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    progress.task_batch_started([TaskProgressItem(
        task_id="question-1", participant_id="RESEARCH_DESK",
        label="Research Desk · RM-02 · 问题 1",
        completion_requires_commit=True,
    )], title="第 1/3 轮 · Research Desk 证据核查")
    progress.task_started("question-1")
    progress.call_started_with_runtime(
        "RESEARCH_DESK", "research_evidence_synthesis", "deepseek", "flash", None,
    )
    assert progress._tasks["question-1"].identity == "Research Desk · RM-02 · 问题 1"
    progress.call_completed("RESEARCH_DESK", "research_evidence_synthesis")
    assert progress._tasks["question-1"].state == "running"
    progress.task_finished("question-1", detail="证据包已落盘")
    assert progress._tasks["question-1"].state == "completed"


def test_two_column_research_keeps_restored_submission_separate_from_desk_completion():
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False, live=True)
    progress.task_batch_started([TaskProgressItem(
        task_id="representative", participant_id="R-TEST", initial_state="completed",
        initial_desk_state="pending", initial_desk_detail="排队等待覆盖检查",
        exploration_layout=True, exploration_actor_title="代表 · 问题已提交",
        exploration_desk_title="Research Desk · 覆盖检查",
    )], title="模块问题覆盖检查")
    table = "\n".join(progress._table_lines_locked())
    assert "代表已提交 1/1 · Desk 已完成 0/1" in table
    assert "Research Desk · 覆盖检查" in table
    progress.exploration_desk("R-TEST", "running", "已完成 1/3 次核查 · 处理中 1 项")
    assert "已完成 1/3 次核查" in "\n".join(progress._table_lines_locked())


def test_large_live_batch_stays_within_terminal_and_shows_active_votes(monkeypatch):
    output = _TTYBuffer()
    progress = ConsoleProgressReporter(output, color=False)
    monkeypatch.setattr(progress, "_terminal_height", lambda: 12)
    progress.task_batch_started(
        [
            TaskProgressItem(task_id=f"vote-{index}", participant_id=f"R-{index}")
            for index in range(50)
        ],
        title="最终出版稿 · 措辞修改投票",
    )
    progress.task_finished("vote-20", failed=True)
    progress.task_started("vote-30")
    progress.task_finished("vote-40")

    lines = progress._table_lines_locked()
    assert len(lines) <= 10  # Keep two terminal lines in reserve for redraws.
    assert progress._rendered_table_lines == len(lines)
    assert any("✗ R-20" in line for line in lines)
    assert any("● R-30" in line for line in lines)
    assert any("其余" in line and "待调用" in line for line in lines)
    assert any("完成 1/50 · 失败 1" in line for line in lines)


def test_tiny_terminal_uses_one_line_live_summary(monkeypatch):
    progress = ConsoleProgressReporter(_TTYBuffer(), color=False)
    monkeypatch.setattr(progress, "_terminal_height", lambda: 7)
    progress.task_batch_started(
        [TaskProgressItem(task_id="vote", participant_id="R-A")],
        title="投票",
    )
    assert len(progress._table_lines_locked()) == 1


def test_ctrl_r_restores_terminal_before_model_replacement_menu(monkeypatch):
    master_fd, slave_fd = os.openpty()
    tty_input = os.fdopen(os.dup(slave_fd), "r", encoding="utf-8", buffering=1)
    original = termios.tcgetattr(tty_input.fileno())
    monkeypatch.setattr(sys, "stdin", tty_input)
    progress = ConsoleProgressReporter(io.StringIO(), color=False, live=True)
    try:
        # The outer run and one provider call both own a listener reference.
        progress.start_control_listener()
        progress.start_control_listener()
        os.write(master_fd, b"\x12")
        deadline = time.monotonic() + 1.0
        while not progress._control_requested.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)

        with pytest.raises(ModelReplacementRequested):
            progress.raise_if_control_requested()

        restored = termios.tcgetattr(tty_input.fileno())
        assert progress._control_thread is None
        assert progress._control_users == 0
        assert restored[3] & termios.ICANON == original[3] & termios.ICANON
        assert restored[3] & termios.ECHO == original[3] & termios.ECHO
    finally:
        progress.shutdown_control_listener()
        tty_input.close()
        os.close(master_fd)
        os.close(slave_fd)
