import io
import json
import os
import stat
import sys
import termios
import pty
import select

import pytest

from project_ensemble.config import EmailNotificationConfig, EnsembleConfig, NotificationsConfig, ProjectConfig, ProviderConfig
from project_ensemble.domain import DecisionRigor, DeliverableType, GenerationResponse, InheritanceMode, MeetingType, ModelDescriptor, ReasoningEffort
from project_ensemble.startup import (
    StartupSelection,
    StartupWizardCancelled,
    TerminalWizard,
    assert_models_were_discovered,
    decode_terminal_input,
    discover_models,
    enable_utf8_terminal_erase,
    collapse_reasoning_effort,
    _delete_last_grapheme,
    _terminal_display_width,
    start_meeting,
    terminal_input,
)


def test_setup_back_reopens_previous_menu_and_retains_earlier_choice():
    answers = iter(["1", "1", "b", "2", "1"])
    wizard = TerminalWizard(input_fn=lambda _prompt: next(answers), output=io.StringIO())
    wizard.language = "zh"
    def three_menus(_config):
        first = wizard._choose_one("第一步", [("a", "甲"), ("b", "乙")])
        second = wizard._choose_one("第二步", [("a", "甲"), ("b", "乙")])
        third = wizard._choose_one("第三步", [("a", "甲"), ("b", "乙")])
        return first, second, third
    wizard._collect_once = three_menus
    assert wizard.collect(object()) == ("a", "b", "a")
    assert "返回上一步" in wizard.output.getvalue()


def test_setup_back_from_first_menu_returns_to_home():
    wizard = TerminalWizard(input_fn=lambda _prompt: "b", output=io.StringIO())
    wizard._collect_once = lambda _config: wizard._choose_one("第一步", [("a", "甲")])
    with pytest.raises(StartupWizardCancelled):
        wizard.collect(object())


def test_successor_topic_dialogue_receives_bounded_source_orientation(tmp_path):
    source = tmp_path / "LR-SOURCE"
    (source / "public/final").mkdir(parents=True)
    (source / "public/task.json").write_text(
        json.dumps({"description": "调查已完成的主题"}), encoding="utf-8",
    )
    (source / "public/final/literature_review_report.md").write_text(
        "# 已完成报告\n\n" + "证据内容。" * 1000, encoding="utf-8",
    )
    context = TerminalWizard._source_meeting_topic_context(str(source))
    assert "调查已完成的主题" in context
    assert "已完成报告" in context
    assert len(context) < 6500


def test_successor_preparatory_chair_receives_full_original_prompt_on_every_call(monkeypatch, tmp_path):
    source = tmp_path / "LR-SOURCE"
    (source / "public").mkdir(parents=True)
    original_prompt = "原始任务开始。" + "完整要求与限制。" * 9000 + "原始任务结束。"
    (source / "original_prompt.txt").write_text(original_prompt, encoding="utf-8")
    (source / "public/task.json").write_text(
        json.dumps({"description": "不应替代原始提示词的短摘要"}), encoding="utf-8",
    )
    source_context = TerminalWizard._source_meeting_topic_context(str(source))
    assert source_context is not None
    assert original_prompt in source_context
    assert "不应替代原始提示词的短摘要" not in source_context

    requests = []

    class Advisor:
        def generate(self, request):
            requests.append(request)
            return GenerationResponse(
                text="[READY] 可以定稿。" if len(requests) == 1 else "新的完整委托。",
                provider_id="fake", model_id=request.model_id,
            )

    monkeypatch.setattr(
        "project_ensemble.startup.build_adapters",
        lambda *_args, **_kwargs: {"fake": Advisor()},
    )
    answers = iter(["请修改研究范围。", "1"])
    wizard = TerminalWizard(input_fn=lambda _prompt: next(answers), output=io.StringIO())
    draft, _development = wizard._develop_literature_task(
        config(tmp_path), ("fake", "m1"), ReasoningEffort.DEFAULT,
        preparatory=True, source_context=source_context,
    )
    assert draft == "新的完整委托。"
    assert len(requests) == 2
    assert all(original_prompt in request.user_text for request in requests)
    assert all("来源会议材料" in request.user_text for request in requests)


def config(tmp_path, *, email_enabled=True):
    return EnsembleConfig(
        project=ProjectConfig(workspace=str(tmp_path / "workspace")),
        providers={
            "fake": ProviderConfig(
                kind="openai_compatible",
                base_url="https://unused.invalid",
                api_key_env="FAKE_API_KEY",
            )
        },
        notifications=NotificationsConfig(
            email=EmailNotificationConfig(
                enabled=email_enabled,
                host="smtp.example.test",
                from_address="ensemble@example.test",
            )
        ),
    )


def test_terminal_wizard_collects_required_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    answers = iter(
        [
            "5",  # deliberation meeting, shown after research workflows
            "1",  # provider
            "1",  # representative model
            "1",  # representative provider default reasoning
            "1",  # Chair model
            "1",  # Chair provider default reasoning
            "1",  # Research Desk disabled
            "1",  # strict decision rigor
            "1",  # conservative parallelism
            "Investigate the bounded task",
            "human@example.test",
            "1",  # Technician disabled
            "y",
        ]
    )
    output = io.StringIO()
    selection = TerminalWizard(input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=output).collect(config(tmp_path))
    assert selection.meeting_type == MeetingType.DELIBERATION
    assert selection.providers == ("fake",)
    assert selection.models == (("fake", "m1"),)
    assert selection.chair_model == ("fake", "m1")
    assert selection.representative_reasoning_effort.value == "default"
    assert selection.chair_reasoning_effort.value == "default"
    assert not selection.research_enabled
    assert selection.escalation_email == "human@example.test"
    rendered = output.getvalue()
    assert "Project ENSEMBLE" in rendered
    assert "重要决策须获全体合格投票者至少四分之三赞成" in rendered
    assert "这些重要决策只须获全体合格投票者过半赞成" in rendered
    menu = rendered.split("1/10 选择会议类型", 1)[-1]
    assert (
        menu.index("文献调研：")
        < menu.index("接续文献调研：")
        < menu.index("学术化重绘：")
        < menu.index("命题核实：")
        < menu.index("议事会议：")
    )
    assert "审计会议：" not in rendered


def test_home_introduction_explains_purpose_without_changing_menu():
    output = io.StringIO()
    wizard = TerminalWizard(input_fn=lambda _: "1", output=output)
    wizard.show_home()
    assert wizard._choose_one("开始使用", [("new", "召开新会议")]) == "new"
    rendered = output.getvalue()
    assert "多模型研究与审议工作台" in rendered
    assert "文献综述" in rendered
    assert "接续会议向主席提问" in rendered


def test_first_launch_selects_english_and_meeting_guide_is_english(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    output = io.StringIO()
    answers = iter(["2"])
    wizard = TerminalWizard(input_fn=lambda _prompt: next(answers), output=output)
    wizard.ensure_language_selected()
    wizard.show_home()
    wizard.show_meeting_guide()
    rendered = output.getvalue()
    assert "First launch" in rendered
    assert "每次会议的成文语言" in rendered
    assert "starting each meeting" in rendered
    assert "How meetings work" in rendered
    assert "how to choose models" in rendered
    assert "an auditable normative document, not an executed task or literature review" in rendered
    assert "Interface language does not set report language" in rendered
    from project_ensemble.user_settings import interface_language
    assert interface_language() == "en"
    second = TerminalWizard(input_fn=lambda _prompt: pytest.fail("asked again"), output=io.StringIO())
    second.ensure_language_selected()
    assert second.language == "en"


def test_selected_meeting_workflow_explains_how_request_is_processed():
    output = io.StringIO()
    wizard = TerminalWizard(input_fn=lambda _prompt: "", output=output)
    wizard.language = "zh"
    wizard._show_selected_workflow("fast_literature_review")
    rendered = output.getvalue()
    assert "任务书与模块范围" in rendered
    assert "Research Desk 检索并核查来源" in rendered
    assert "未解决的关键争议" in rendered


@pytest.mark.parametrize("language,expected", [
    ("zh", "交付的是带有决策轨迹的规范性文书"),
    ("en", "The deliverable is an auditable normative document"),
])
def test_deliberation_workflow_explains_deliverable_and_non_goals(language, expected):
    output = io.StringIO()
    wizard = TerminalWizard(input_fn=lambda _prompt: "", output=output)
    wizard.language = language
    wizard._show_selected_workflow(MeetingType.DELIBERATION.value)
    rendered = output.getvalue()
    assert expected in rendered
    assert ("不" if language == "zh" else "not") in rendered


def test_menu_back_shortcut_only_when_back_option_exists():
    choices = iter(["b"])
    wizard = TerminalWizard(input_fn=lambda _: next(choices), output=io.StringIO())
    assert wizard._choose_one("选择会议", [("meeting", "会议"), ("back", "返回")]) == "back"


def test_provider_menu_shows_provider_name_not_only_transport_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_API_KEY", "test-only")
    description = TerminalWizard._provider_description("fake", config(tmp_path))
    assert description.startswith("fake · 凭据已检测")
    assert "openai_compatible" not in description


def test_terminal_wizard_freezes_relaxed_high_threshold_choice(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    answers = iter([
        "5", "1", "1", "1", "1", "1", "1",
        "2",  # relaxed high threshold
        "1",  # conservative parallelism
        "Investigate a bounded task", "", "1", "y",
    ])
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()
    ).collect(config(tmp_path))
    assert selection.decision_rigor == DecisionRigor.RELAXED
    repo = start_meeting(
        config(tmp_path), selection,
        governance_docs="docs/governance", output_directory=tmp_path / "workspace",
    )
    import json

    public = json.loads(repo.docs.read_text("public/meeting_manifest.json"))
    private = json.loads(repo.docs.read_text("identity_private/meeting_manifest.json"))
    policy = json.loads(repo.docs.read_text("public/decision_policy.json"))
    assert repo.meeting_id.startswith("DL-")
    assert public["decision_rigor"] == private["decision_rigor"] == "relaxed"
    assert policy["high_threshold_formula"] == "floor(N_ACTIVE/2)+1"


def test_terminal_wizard_offers_research_only_meeting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    answers = iter(
        [
            "4",  # research-only meeting
            "1",  # provider
            "1",  # Research Desk model
            "1",  # default reasoning
            "",  # OpenAlex suggested limit: 12 candidates per query
            "标准毛细波分析要求界面可表示为单值高度场。",
            "",  # no email
            "1",  # Technician disabled
            "y",
        ]
    )
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()
    ).collect(config(tmp_path))
    assert selection.meeting_type == MeetingType.RESEARCH
    assert selection.models == ()
    assert selection.chair_model is None
    assert selection.research_enabled
    assert selection.research_model == ("fake", "m1")
    assert selection.openalex_max_results_per_query == 12
    assert selection.openalex_quota_policy == "wait"


def test_terminal_wizard_can_choose_tavily_for_openalex_daily_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    cfg = config(tmp_path)
    cfg.research.tavily.enabled = True
    answers = iter([
        "4", "1", "1", "1", "", "一项可核查的事实主张。", "", "1", "y",
    ])
    selection = TerminalWizard(
        input_fn=lambda prompt: (
            "1" if prompt.startswith("通用网页搜索") else "2" if prompt.startswith("选择 1–2；回车默认等待") else (
                "" if prompt.startswith(("每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)
            )
        ), output=io.StringIO(),
    ).collect(cfg)
    assert selection.openalex_quota_policy == "tavily"


def test_research_claim_can_be_developed_with_preparatory_chair(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    requests = []

    class Advisor:
        def generate(self, request):
            requests.append(request)
            return GenerationResponse(
                text=(
                    "请明确比较的体系和时间范围。" if len(requests) == 1
                    else "截至 2025 年，标准毛细波分析是否要求界面可表示为单值高度场？"
                ),
                provider_id="fake",
                model_id="m1",
            )

    monkeypatch.setattr("project_ensemble.startup.build_adapters", lambda cfg, require_keys: {"fake": Advisor()})
    answers = iter([
        "4", "1", "1", "1", "",  # research setup and OpenAlex default
        "2", "1",  # converse, using the Research Desk model
        "我想核查毛细波方法的适用条件。", "/draft", "1",
        "", "1", "y",  # no email; Technician disabled; confirm meeting
    ])
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()
    ).collect(config(tmp_path), prompt_for_claim_dialogue=True)
    assert selection.chair_model is None  # The temporary advisor is not a formal Chair.
    assert selection.task_description.startswith("截至 2025 年")
    assert selection.prompt_development is not None
    assert selection.prompt_development["turns"][0]["role"] == "human"
    assert len(requests) == 2
    repo = start_meeting(
        config(tmp_path), selection,
        governance_docs="docs/governance", output_directory=tmp_path / "meetings",
    )
    trace = json.loads(repo.docs.read_text("human_private/prompt_development.json"))
    assert trace["confirmed_task"] == selection.task_description
    assert repo.docs.read_text("original_prompt.txt") == selection.task_description


def test_research_claim_dialogue_can_fall_back_to_direct_input(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    monkeypatch.setattr("project_ensemble.startup.build_adapters", lambda cfg, require_keys: {"fake": object()})
    answers = iter([
        "4", "1", "1", "1", "", "2", "1", "/back",
        "人工直接确认的命题。", "", "1", "y",
    ])
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()
    ).collect(config(tmp_path), prompt_for_claim_dialogue=True)
    assert selection.task_description == "人工直接确认的命题。"
    assert selection.prompt_development is None


def test_terminal_wizard_accepts_fifty_openalex_results_but_rejects_higher_values(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    answers = iter([
        "4", "1", "1", "1", "51", "0", "50",
        "一项可核查的事实主张。", "", "1", "y",
    ])
    output = io.StringIO()
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=output
    ).collect(config(tmp_path))
    assert selection.openalex_max_results_per_query == 50
    assert output.getvalue().count("请输入 1–50 的整数") == 2


def test_terminal_wizard_offers_derived_literature_review(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    monkeypatch.setattr("project_ensemble.startup.indexed_meetings", lambda _: [])
    monkeypatch.setattr("project_ensemble.startup.discover_local_meetings", lambda _: [])
    source = tmp_path / "M-SOURCE"
    (source / "public").mkdir(parents=True)
    (source / "public/meeting_manifest.json").write_text(
        '{"meeting_id":"M-SOURCE"}', encoding="utf-8"
    )
    answers = iter([
            "2", "2", "b", "2", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "", "3", "1", "1", str(source),
        "形成完整文献调研报告", "1", "", "3", "3", "4", "50000", "1", "", "1", "y",
    ])
    output = io.StringIO()
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=output
    ).collect(config(tmp_path), prompt_for_literature_dialogue=True)
    assert selection.meeting_type == MeetingType.DELIBERATION
    assert selection.deliverable_type == DeliverableType.LITERATURE_REVIEW
    assert selection.parent_meeting_path == str(source.resolve())
    assert selection.research_enabled
    assert selection.research_model == ("fake", "m1")
    assert selection.research_max_concurrent_claim_groups == 3
    assert selection.literature_language == "zh"
    assert selection.literature_full_abstract is False
    assert selection.literature_section_abstracts is False
    assert selection.literature_segmentation == 3
    assert selection.literature_liveliness == 3
    assert selection.literature_target_body_characters == 50000
    assert selection.literature_writing_policy == "v071"
    assert "接续文献调研 · 如何确认新的研究题目" in output.getvalue()
    assert "会议类型: 接续文献调研 · 完整流程" in output.getvalue()
    assert "会议类型: deliberation" not in output.getvalue()
    assert "已返回上一步" in output.getvalue()


def test_derived_literature_review_can_use_fast_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [
            ModelDescriptor(provider_id="fake", model_id="m1"),
            ModelDescriptor(provider_id="fake", model_id="m2"),
        ],
    )
    source = tmp_path / "M-SOURCE"
    source.mkdir()
    monkeypatch.setattr(TerminalWizard, "_choose_source_meeting", lambda self, cfg: str(source))
    answers = iter([
        "2", "1", "1", "1,2", "1", "1", "1", "1", "1,2", "1", "1", "", "",
        "1", "调查一个有界的问题", "1", "", "3", "3", "4", "10000", "1", "", "1", "y",
    ])
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)),
        output=io.StringIO(),
    ).collect(config(tmp_path))
    assert selection.parent_meeting_path == str(source)
    assert selection.literature_writing_policy == "fast"
    assert selection.chair_model is None


def test_literature_startup_can_refine_full_task_with_selected_chair(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    requests = []
    replies = iter([
        "报告主要供谁阅读？",
        "[READY] 已明确研究对象和读者，可以生成完整任务。",
        "请撰写面向临床读者的疫苗质量控制综述。\n\n证据要求：核对原始资料。",
        "[READY] 已加入比较各国监管实践的要求。",
        "请撰写面向临床读者的各国疫苗质量控制比较综述。\n\n证据要求：核对原始资料。",
    ])

    class Chair:
        def generate(self, request):
            requests.append(request)
            return GenerationResponse(text=next(replies), provider_id="fake", model_id="m1")

    monkeypatch.setattr("project_ensemble.startup.build_adapters", lambda cfg, require_keys: {"fake": Chair()})
    answers = iter([
        "1", "2", "2", "1",  # new literature review, full workflow, AI dialogue, provider
        "1", "1", "1", "1",  # representatives and Chair
        "1", "1", "1", "1",  # Research Desk and writer
        "", "", "1", "1",  # search settings, rigor, parallelism
        "研究疫苗质量控制。", "面向临床读者。", "2",  # first draft, continue
        "还要比较不同国家的做法。", "1",  # revised full draft, confirm
        "1", "", "3", "3", "4", "5000", "1",  # writing preferences and palette
        "", "1", "y",  # email, Technician disabled, confirmation
    ])
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()
    ).collect(config(tmp_path), prompt_for_literature_dialogue=True)
    assert selection.deliverable_type == DeliverableType.LITERATURE_REVIEW
    assert selection.chair_model == ("fake", "m1")
    assert "各国疫苗质量控制" in selection.task_description
    assert selection.prompt_development["kind"] == "LITERATURE_PROMPT_PREFLIGHT"
    assert len(requests) == 5
    assert all("你是" not in request.system_text for request in requests)
    repo = start_meeting(
        config(tmp_path), selection,
        governance_docs="docs/governance", output_directory=tmp_path / "meetings",
    )
    assert repo.docs.read_text("original_prompt.txt") == selection.task_description
    trace = json.loads(repo.docs.read_text("human_private/prompt_development.json"))
    assert trace["confirmed_task"] == selection.task_description
    assert any(turn["role"] == "chair_draft" for turn in trace["turns"])


def test_deliberation_can_refine_its_brief_without_starting_a_vote(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    requests = []
    replies = iter([
        "[READY] 议题、交付形式与人类保留的决定均已明确。",
        "请就所给方案的风险提出备选方案、交叉审阅并形成建议；人类裁定最终取舍。",
    ])

    class Chair:
        def generate(self, request):
            requests.append(request)
            return GenerationResponse(text=next(replies), provider_id="fake", model_id="m1")

    monkeypatch.setattr("project_ensemble.startup.build_adapters", lambda cfg, require_keys: {"fake": Chair()})
    answers = iter([
        "5", "1", "1", "1", "1", "1",  # meeting, provider, representative, chair
        "1", "1", "1",  # Research Desk off, strict, parallel off
        "2", "比较两种方案的风险与可行性。", "1",  # dialogue, initial brief, confirm draft
        "3", "", "1", "y",  # English document, email, Technician off, final confirmation
    ])
    selection = TerminalWizard(input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()).collect(
        config(tmp_path), prompt_for_deliberation_dialogue=True,
    )
    assert selection.meeting_type == MeetingType.DELIBERATION
    assert selection.deliverable_type == DeliverableType.NORMATIVE_INSTRUMENT
    assert selection.deliberation_language == "en"
    assert selection.prompt_development["kind"] == "DELIBERATION_PROMPT_PREFLIGHT"
    assert len(requests) == 2
    assert "不预先审议、投票" in requests[0].system_text
    assert "不预设表决结果" in requests[1].system_text
    assert all("你是" not in request.system_text for request in requests)
    repo = start_meeting(
        config(tmp_path), selection,
        governance_docs="docs/governance", output_directory=tmp_path / "meetings",
    )
    preferences = json.loads(repo.docs.read_text("public/deliberation_writing_preferences.json"))
    assert preferences["language"] == "en"


def test_source_meeting_selector_lists_registered_and_local_meetings(monkeypatch, tmp_path):
    from project_ensemble.storage.meeting_index import inspect_meeting

    registered = tmp_path / "DL-REGISTERED"
    local = tmp_path / "LR-LOCAL"
    for root, title in ((registered, "已经登记的研究"), (local, "当前目录中的研究")):
        (root / "public").mkdir(parents=True)
        (root / "public/meeting_manifest.json").write_text(
            json.dumps({"meeting_id": root.name, "title": title, "created_at": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
    monkeypatch.setattr("project_ensemble.startup.indexed_meetings", lambda _: [inspect_meeting(registered)])
    monkeypatch.setattr("project_ensemble.startup.discover_local_meetings", lambda _: [local])
    output = io.StringIO()
    chosen = TerminalWizard(input_fn=lambda _: "2", output=output)._choose_source_meeting(config(tmp_path))
    assert chosen == str(local.resolve())
    assert "已经登记的研究" in output.getvalue()
    assert "当前目录中的研究" in output.getvalue()
    assert "手动输入源会议" in output.getvalue()


def test_chair_title_is_used_when_available_and_falls_back_on_invalid_output(monkeypatch, tmp_path):
    from project_ensemble.domain import GenerationResponse

    cfg = config(tmp_path)
    monkeypatch.setenv("FAKE_API_KEY", "test-only")
    class Adapter:
        def __init__(self, text):
            self.text = text
        def generate(self, request):
            assert request.user_text == "调查一个有界的问题"
            return GenerationResponse(text=self.text, provider_id="fake", model_id="m1")
    monkeypatch.setattr("project_ensemble.startup.build_adapters", lambda *_args, **_kwargs: {"fake": Adapter("  某问题的研究进展  ")})
    wizard = TerminalWizard(output=io.StringIO())
    assert wizard._chair_title(cfg, ("fake", "m1"), ReasoningEffort.DEFAULT, "调查一个有界的问题") == "某问题的研究进展"
    monkeypatch.setattr("project_ensemble.startup.build_adapters", lambda *_args, **_kwargs: {"fake": Adapter("标题\n附加解释")})
    assert wizard._chair_title(cfg, ("fake", "m1"), ReasoningEffort.DEFAULT, "调查一个有界的问题") is None


def test_terminal_wizard_offers_from_scratch_literature_review(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m1")],
    )
    answers = iter([
            "1", "2", "1", "1", "1", "1", "1", "1", "1", "1", "1", "", "", "1", "1",
        "从零形成完整文献调研报告", "1", "", "3", "3", "4", "30000", "1", "", "1", "y",
    ])
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()
    ).collect(config(tmp_path))
    assert selection.meeting_type == MeetingType.DELIBERATION
    assert selection.deliverable_type == DeliverableType.LITERATURE_REVIEW
    assert selection.parent_meeting_path is None
    assert selection.research_enabled
    assert selection.research_model == ("fake", "m1")
    assert selection.research_max_concurrent_claim_groups == 4
    assert selection.literature_language == "zh"
    assert selection.literature_target_body_characters == 30000


def test_simple_literature_flow_does_not_offer_deliberation_chair_dialogue(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [
            ModelDescriptor(provider_id="fake", model_id="m1"),
            ModelDescriptor(provider_id="fake", model_id="m2"),
        ],
    )
    answers = iter([
        "1", "1", "1", "1", "1,2", "1", "1", "1", "1", "1,2", "1", "1", "", "",
        "调查一个有界的问题", "1", "", "3", "3", "4", "10000", "1", "", "1", "y",
    ])
    output = io.StringIO()
    selection = TerminalWizard(input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=output).collect(
        config(tmp_path), prompt_for_literature_dialogue=True,
        prompt_for_deliberation_dialogue=True,
    )
    assert selection.literature_writing_policy == "fast"
    assert selection.chair_model is None
    assert "如何确定本次议事会议的任务委托" not in output.getvalue()
    assert "如何确认研究题目" in output.getvalue()
    assert "选择文献调研流程" in output.getvalue()


def test_simple_literature_direct_brief_gets_model_proposed_title(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_API_KEY", "test-only")
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [
            ModelDescriptor(provider_id="fake", model_id="writer"),
            ModelDescriptor(provider_id="fake", model_id="reviewer"),
        ],
    )
    requests = []

    class Writer:
        def generate(self, request):
            requests.append(request)
            return GenerationResponse(
                text="疫苗质量控制策略比较", provider_id="fake", model_id=request.model_id,
            )

    monkeypatch.setattr(
        "project_ensemble.startup.build_adapters",
        lambda *_args, **_kwargs: {"fake": Writer()},
    )
    answers = iter([
        "1", "1", "1", "1", "1,2", "1", "1", "1", "1", "1,2", "1", "1", "", "",
        "比较各国疫苗质量控制策略。", "", "1", "", "3", "3", "4", "10000", "1", "", "1", "y",
    ])
    selection = TerminalWizard(input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()).collect(
        config(tmp_path), prompt_for_literature_dialogue=True, prompt_for_title=True,
    )
    assert selection.task_description == "比较各国疫苗质量控制策略。"
    assert selection.meeting_title == "疫苗质量控制策略比较"
    assert selection.chair_model is None
    assert len(requests) == 1
    assert requests[0].model_id == "writer"


def test_simple_literature_preparatory_chair_dialogue_preserves_formal_roles(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_API_KEY", "test-only")
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [
            ModelDescriptor(provider_id="fake", model_id="writer"),
            ModelDescriptor(provider_id="fake", model_id="reviewer"),
        ],
    )
    replies = iter([
        "[READY] 任务范围已明确，可以起草完整委托。",
        "比较各国疫苗质量控制策略，核对原始法规文本并说明适用范围。",
        "疫苗质量控制策略比较",
    ])
    requests = []

    class Advisor:
        def generate(self, request):
            requests.append(request)
            return GenerationResponse(
                text=next(replies), provider_id="fake", model_id=request.model_id,
            )

    monkeypatch.setattr(
        "project_ensemble.startup.build_adapters",
        lambda *_args, **_kwargs: {"fake": Advisor()},
    )
    answers = iter([
        "1", "1", "2", "1", "1,2", "1", "1", "1", "1", "1,2", "1", "1", "", "",
        "2", "2", "1", "希望比较各国疫苗质量控制策略。\n还要核对原始法规文本。", "1", "",
        "1", "", "3", "3", "4", "10000", "1", "", "1", "y",
    ])
    output = io.StringIO()
    selection = TerminalWizard(input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=output).collect(
        config(tmp_path), prompt_for_literature_dialogue=True, prompt_for_title=True,
    )
    assert selection.task_description.startswith("比较各国疫苗质量控制策略")
    assert selection.meeting_title == "疫苗质量控制策略比较"
    assert selection.chair_model is None
    assert selection.prompt_development["kind"] == "LITERATURE_PROMPT_PREFLIGHT"
    assert selection.prompt_development["chair_model"] == "fake:reviewer"
    assert len(requests) == 3
    assert all(request.model_id == "reviewer" for request in requests)
    assert "希望比较各国疫苗质量控制策略。\n还要核对原始法规文本。" in requests[0].user_text
    assert "筹备主席只协助拟定开题委托" in output.getvalue()
    assert "\n  ── 我 · 已发送（2 行） ──\n\n" in output.getvalue()
    assert "\n  ── 主席 ──\n任务范围已明确，可以起草完整委托。\n\n" in output.getvalue()


def test_preparatory_dialogue_replies_have_separate_visual_turns():
    output = io.StringIO()
    wizard = TerminalWizard(input_fn=lambda _prompt: "", output=output, color=False)
    wizard.language = "zh"
    wizard._dialogue_reply("主席", "Chair", "第一轮回复。")
    wizard._dialogue_reply("主席", "Chair", "第二轮回复。")
    assert output.getvalue() == (
        "\n  ── 主席 ──\n第一轮回复。\n\n"
        "\n  ── 主席 ──\n第二轮回复。\n\n"
    )


def test_dialogue_human_input_is_confirmed_without_duplicate_text():
    output = io.StringIO()
    wizard = TerminalWizard(input_fn=lambda _prompt: "", output=output, color=True)
    wizard.language = "zh"
    wizard._dialogue_human_turn("第一行。\n第二行。")
    wizard._dialogue_reply("主席", "Chair", "我看到了两行。")
    rendered = output.getvalue()
    assert "\x1b[32m  ── 我 · 已发送（2 行） ──\x1b[0m" in rendered
    assert "第一行。\n第二行。" not in rendered
    assert "\x1b[36m  ── 主席 ──\x1b[0m\n我看到了两行。" in rendered


@pytest.mark.parametrize(("language", "expected"), [("zh", "我: "), ("en", "Me: ")])
def test_dialogue_input_prompt_uses_first_person_label(language, expected):
    prompts = []
    wizard = TerminalWizard(input_fn=lambda prompt: prompts.append(prompt) or "", output=io.StringIO())
    wizard.language = language
    wizard.input("我: ")
    assert prompts == [expected]


@pytest.mark.parametrize("direct_continuation", [False, True])
def test_terminal_wizard_collects_scholarly_rendering_configuration(
    monkeypatch, tmp_path, direct_continuation
):
    monkeypatch.setattr(
        "project_ensemble.startup.discover_models",
        lambda cfg, providers: [
            ModelDescriptor(provider_id="fake", model_id="m1"),
            ModelDescriptor(provider_id="fake", model_id="m2"),
        ],
    )
    source = tmp_path / "M-SOURCE"
    (source / "public/final").mkdir(parents=True)
    (source / "public/meeting_manifest.json").write_text(
        '{"meeting_id":"M-SOURCE"}', encoding="utf-8"
    )
    (source / "public/final/literature_review_report.md").write_text(
        "# Source\n\nReviewed result.\n", encoding="utf-8"
    )
    answers = iter(
        (["3"] if not direct_continuation else [])
        + [
            "1",  # provider
            "1,2",  # science models
            "1,2",  # citation models
            "1",  # reviewer effort
            "1",  # Chair
            "1",  # Chair effort
            "1",  # Research Desk
            "1",  # Research Desk effort
                *([str(source)] if not direct_continuation else []),
                "1",  # full rendering; preserves the existing length-budget flow
                "1",  # Chinese
            "1",  # strict academic skeleton
            "",  # no summaries
            "3",  # segmentation
            "2",  # liveliness
            "50000",  # reader-facing body target, characters
                    "2,4",  # Markdown + PDF
                "1",  # Human decides science objections by default
                "2，1",  # Human-frozen science order; Chinese comma
            "",  # OpenAlex suggested limit
            "1",  # conservative parallelism
            "将冻结综述重绘为学术文章",
            "1",  # report palette
            "",  # no email
            "1",  # Technician disabled
            "y",
        ]
    )
    selection = TerminalWizard(
        input_fn=lambda prompt: ("" if prompt.startswith(("选择 1–2；回车默认等待", "每个模型最多同时调用多少次", "通用网页搜索", "学术搜索引擎")) else next(answers)), output=io.StringIO()
    ).collect(
        config(tmp_path),
        parent_meeting_path=str(source.resolve()) if direct_continuation else None,
        inheritance_mode=InheritanceMode.BOTH if direct_continuation else None,
        forced_meeting_type=MeetingType.SCHOLARLY_RENDERING if direct_continuation else None,
    )
    assert selection.meeting_type == MeetingType.SCHOLARLY_RENDERING
    assert selection.deliverable_type == DeliverableType.SCHOLARLY_RENDERING
    assert selection.rendering_science_models == (("fake", "m1"), ("fake", "m2"))
    assert selection.rendering_citation_models == (("fake", "m1"), ("fake", "m2"))
    assert selection.rendering_science_order == ("fake:m2", "fake:m1")
    assert selection.rendering_output_formats == ("md", "pdf")
    assert selection.rendering_target_body_characters == 50000
    assert selection.parent_meeting_path == str(source.resolve())


def test_rendering_format_prompt_defaults_to_html_on_enter():
    wizard = TerminalWizard(input_fn=lambda _: "", output=io.StringIO())
    selected = wizard._choose_many(
        "选择输出格式", [("html", "HTML"), ("md", "Markdown"), ("latex", "LaTeX"), ("pdf", "PDF")],
        default_values=["html"],
    )
    assert selected == ["html"]


def test_meeting_multi_select_accepts_mixed_separators_and_inclusive_ranges():
    wizard = TerminalWizard(input_fn=lambda _prompt: "4，1 - 2、3", output=io.StringIO())
    selected = wizard._choose_many(
        "models", [(f"m{index}", f"Model {index}") for index in range(1, 5)],
    )
    assert selected == ["m4", "m1", "m2", "m3"]


def test_meeting_multi_select_rejects_duplicates_after_range_expansion():
    answers = iter(["1-3，2", "1;3"])
    output = io.StringIO()
    wizard = TerminalWizard(input_fn=lambda _prompt: next(answers), output=output)
    selected = wizard._choose_many("models", [("a", "A"), ("b", "B"), ("c", "C")])
    assert selected == ["a", "c"]
    assert "不重复" in output.getvalue()


def test_terminal_input_decodes_utf8_and_gb18030_chinese_without_replacement():
    task = (
        "请你们分析一下，使用特殊的阻尼方案破坏分子动力学里LJ对称二维二元液体"
        "（../bin_liquid_liquid中的配置）的gaussian速率分布的方案，要是已经有的文献里的方案"
    )
    assert decode_terminal_input(task.encode("utf-8")) == task
    assert decode_terminal_input(task.encode("gb18030")) == task

    large_paste = (task + "；cache α=0.25，路径 ../数据。\n") * 32
    assert decode_terminal_input(large_paste.encode("utf-8")) == large_paste

    # readline receives undecodable bytes through surrogateescape so that the
    # original clipboard payload can still be decoded by the GB18030 fallback.
    raw_gb18030 = task.encode("gb18030")
    preserved = raw_gb18030.decode("utf-8", errors="surrogateescape")
    assert decode_terminal_input(
        preserved.encode("utf-8", errors="surrogateescape")
    ) == task


def test_unicode_editor_deletes_one_complete_chinese_or_combining_cluster():
    assert _terminal_display_width("A中文") == 5
    chinese = list("中文")
    _delete_last_grapheme(chinese)
    assert "".join(chinese) == "中"

    combining = list("Cafe\u0301")
    _delete_last_grapheme(combining)
    assert "".join(combining) == "Caf"


def test_explicit_multiline_mode_preserves_first_line_and_newlines(monkeypatch):
    stream = io.BytesIO(b"/paste\nfirst line\nsecond line\n/end\n")
    fake_stdin = type("Input", (), {"isatty": lambda self: False, "buffer": stream})()
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "stdout", output)
    assert terminal_input("Task: ") == "first line\nsecond line"
    assert "/end" in output.getvalue()


@pytest.mark.skipif(not hasattr(pty, "fork"), reason="requires a POSIX pseudo-terminal")
@pytest.mark.parametrize(
    ("pasted", "expected_first", "expected_second"),
    [
        (b"alpha\nbeta\n", b"FIRST=alpha", b"SECOND=beta"),
        (
            b"\x1b[200~" + "第一行\n第二行".encode("utf-8") + b"\x1b[201~\nnext\n",
            "FIRST=第一行|第二行".encode("utf-8"), b"SECOND=next",
        ),
    ],
)
def test_fallback_editor_does_not_lose_lines_in_one_paste(pasted, expected_first, expected_second):
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        try:
            sys.stdin = os.fdopen(os.dup(0), "r", encoding="utf-8", buffering=1)
            sys.stdout = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
            first = terminal_input("FIRST: ")
            os.write(1, b"FIRST=" + first.replace("\n", "|").encode("utf-8") + b"\n")
            second = terminal_input("SECOND: ")
            os.write(1, b"SECOND=" + second.encode("utf-8") + b"\n")
        finally:
            os._exit(0)
    observed = b""
    try:
        while b"FIRST: " not in observed:
            ready, _, _ = select.select([master_fd], [], [], 3)
            assert ready, observed
            observed += os.read(master_fd, 4096)
        os.write(master_fd, pasted)
        while expected_second not in observed:
            ready, _, _ = select.select([master_fd], [], [], 3)
            assert ready, observed
            try:
                observed += os.read(master_fd, 4096)
            except OSError:
                break
    finally:
        os.close(master_fd)
        os.waitpid(child_pid, 0)
    assert expected_first in observed
    assert expected_second in observed


@pytest.mark.skipif(not hasattr(pty, "fork"), reason="requires a POSIX pseudo-terminal")
def test_interactive_unicode_editor_supports_cjk_backspace_and_cursor_navigation():
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        try:
            # Wrapped streams exercise the dependency-free fallback editor;
            # normal CLI startup uses GNU readline.
            sys.stdin = os.fdopen(os.dup(0), "r", encoding="utf-8", buffering=1)
            sys.stdout = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
            result = terminal_input("输入: ")
            os.write(1, b"RESULT:" + result.encode("utf-8") + b"\n")
        finally:
            os._exit(0)
    try:
        observed = b""
        while "输入: ".encode() not in observed:
            observed += os.read(master_fd, 256)
        # Move before the final character, insert in the middle, move to the
        # end, then delete the final character.  Both editor implementations
        # must keep the logical value and visual cursor in sync for wide glyphs.
        os.write(
            master_fd,
            "中文".encode("utf-8") + b"\x1b[D" + "X".encode() + b"\x1b[C\x7f\n",
        )
        while True:
            try:
                chunk = os.read(master_fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            observed += chunk
        os.waitpid(child_pid, 0)
    finally:
        os.close(master_fd)
    assert "RESULT:中X".encode("utf-8") in observed
    # There should be one committed line ending, not a blank line introduced
    # by a full-line redraw after Backspace.
    assert b"\n\n" not in observed


@pytest.mark.skipif(not hasattr(pty, "fork"), reason="requires a POSIX pseudo-terminal")
def test_interactive_editor_reprompts_after_undecodable_clipboard_bytes():
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        try:
            sys.stdin = os.fdopen(os.dup(0), "r", encoding="utf-8", buffering=1)
            sys.stdout = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
            result = terminal_input("输入: ")
            os.write(1, b"RESULT:" + result.encode("utf-8") + b"\n")
        finally:
            os._exit(0)
    try:
        observed = b""
        while "输入: ".encode() not in observed:
            observed += os.read(master_fd, 256)
        os.write(master_fd, b"\xff\n")
        warning = "输入包含无法识别的字节".encode("utf-8")
        while warning not in observed:
            observed += os.read(master_fd, 1024)
        os.write(master_fd, "重新粘贴成功\n".encode("utf-8"))
        while True:
            try:
                chunk = os.read(master_fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            observed += chunk
        os.waitpid(child_pid, 0)
    finally:
        os.close(master_fd)
    assert "RESULT:重新粘贴成功".encode("utf-8") in observed


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="IUTF8 is a Linux terminal flag")
def test_terminal_backspace_erases_a_complete_chinese_character():
    master_fd, slave_fd = pty.openpty()
    try:
        attributes = termios.tcgetattr(slave_fd)
        iutf8 = getattr(termios, "IUTF8", 0x4000)
        attributes[0] &= ~iutf8
        attributes[3] |= termios.ICANON
        termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)

        with os.fdopen(os.dup(slave_fd), "r", encoding="utf-8") as terminal_stream:
            assert enable_utf8_terminal_erase(terminal_stream)
        assert termios.tcgetattr(slave_fd)[0] & iutf8

        erase = termios.tcgetattr(slave_fd)[6][termios.VERASE]
        if isinstance(erase, int):
            erase = bytes([erase])
        os.write(master_fd, "中".encode("utf-8") + erase + "文\n".encode("utf-8"))
        assert os.read(slave_fd, 32) == "文\n".encode("utf-8")
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_provider_picker_blank_selects_all_options():
    output = io.StringIO()
    prompts = []
    wizard = TerminalWizard(input_fn=lambda prompt: prompts.append(prompt) or "", output=output)
    selected = wizard._choose_many(
        "providers",
        [("deepseek", "first"), ("kimi", "second"), ("gemini", "third")],
        blank_means_all=True,
    )
    assert selected == ["deepseek", "kimi", "gemini"]
    assert "直接回车选择全部" in prompts[0]


def test_configured_provider_discovery_ignores_legacy_enabled_flag(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.providers["fake"].enabled = False
    seen = {}

    class Adapter:
        def list_models(self):
            return [ModelDescriptor(provider_id="fake", model_id="m1")]

    def build(selected, require_keys=True):
        seen["enabled"] = selected.providers["fake"].enabled
        return {"fake": Adapter()}

    monkeypatch.setattr("project_ensemble.startup.build_adapters", build)
    assert [model.model_id for model in discover_models(cfg, ["fake"])] == ["m1"]
    assert seen["enabled"] is True
    assert cfg.providers["fake"].enabled is False


def test_discovery_uses_configured_selectable_models_in_config_order(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.providers["fake"].selectable_models = ["m2", "m1"]

    class CatalogAdapter:
        def list_models(self):
            return [
                ModelDescriptor(provider_id="fake", model_id="m1", supported_methods=["chat/completions"]),
                ModelDescriptor(provider_id="fake", model_id="noise", supported_methods=["chat/completions"]),
                ModelDescriptor(provider_id="fake", model_id="m2", supported_methods=["chat/completions"]),
            ]

    monkeypatch.setattr("project_ensemble.startup.build_adapters", lambda cfg, require_keys: {"fake": CatalogAdapter()})
    assert [model.model_id for model in discover_models(cfg, ["fake"])] == ["m2", "m1"]


def test_hidden_models_are_omitted_from_pickers_but_available_in_visibility_manager(monkeypatch, tmp_path):
    cfg = config(tmp_path)

    class CatalogAdapter:
        def list_models(self):
            return [
                ModelDescriptor(provider_id="fake", model_id="m1", supported_methods=["chat/completions"]),
                ModelDescriptor(provider_id="fake", model_id="m2", supported_methods=["chat/completions"]),
            ]

    monkeypatch.setattr("project_ensemble.startup.build_adapters", lambda cfg, require_keys: {"fake": CatalogAdapter()})
    monkeypatch.setattr("project_ensemble.user_settings.hidden_model_ids", lambda provider_id: {"m1"})

    assert [model.model_id for model in discover_models(cfg, ["fake"])] == ["m2"]
    assert [model.model_id for model in discover_models(cfg, ["fake"], include_hidden=True)] == ["m1", "m2"]


def test_start_persists_task_and_contact_in_separate_compartments(tmp_path):
    cfg = config(tmp_path)
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"),),
        chair_model=("fake", "m1"),
        task_description="A public-to-participants task",
        escalation_email="human@example.test",
    )
    repo = start_meeting(cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "workspace")
    assert "A public-to-participants task" in (repo.root / "public/task.json").read_text()
    assert "human@example.test" not in (repo.root / "public/task.json").read_text()
    assert "human@example.test" in (repo.root / "human_private/escalation_contact.json").read_text()
    assert stat.S_IMODE(repo.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((repo.root / "human_private/escalation_contact.json").stat().st_mode) == 0o600
    assert repo.events.verify()


def test_literature_start_freezes_reader_writing_preferences(tmp_path):
    cfg = config(tmp_path)
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        providers=("fake",), models=(("fake", "m1"),),
        chair_model=("fake", "m1"), task_description="Review evidence.",
        escalation_email=None, research_enabled=True,
        research_model=("fake", "m1"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_language="fr", literature_full_abstract=True,
        literature_section_abstracts=False,
        literature_segmentation=4, literature_liveliness=2,
        literature_target_body_characters=42000,
    )
    repo = start_meeting(
        cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "review"
    )
    frozen = json.loads((repo.root / "public/literature_report/writing_preferences.json").read_text())
    assert frozen == {
        "language": "fr", "full_abstract": True, "section_abstracts": False,
        "segmentation_1_to_5": 4, "liveliness_1_to_5": 2, "signposting_1_to_5": 4,
        "target_body_characters": 42000,
        "target_length_policy": "ADVISORY_ONLY; NO_HARD_LIMIT; FINAL_LENGTH_NOT_GUARANTEED",
        "fact_first_writing": True,
    }


def test_fast_start_freezes_selected_split_proposers_separately_from_reviewers(tmp_path):
    cfg = config(tmp_path)
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
        providers=("fake",), models=(("fake", "m1"), ("fake", "m2")),
        chair_model=None, task_description="Investigate a bounded question.",
        escalation_email=None, research_enabled=True,
        research_model=("fake", "m1"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        research_max_concurrent_claim_groups=5,
        writer_model=("fake", "m1"), writer_reasoning_effort=ReasoningEffort.DEFAULT,
        literature_writing_policy="fast",
        fast_planner_models=(("fake", "m2"), ("fake", "m1")),
        maximum_parallelism=True,
    )
    repo = start_meeting(
        cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "fast-review",
    )
    manifest = json.loads((repo.root / "identity_private/meeting_manifest.json").read_text())
    assert manifest["fast_planner_models"] == [["fake", "m2"], ["fake", "m1"]]
    assert manifest["model_concurrency_limits"] == {"fake:m1": 5, "fake:m2": 5}
    assert manifest["fast_planner_reasoning_effective"] == {
        "FAST_PLANNER_1": "default", "FAST_PLANNER_2": "default",
    }
    registry = json.loads((repo.root / "identity_private/representative_registry.json").read_text())
    assert len(registry) == 2


def test_human_reference_is_frozen_and_retrievable_but_not_verified(tmp_path):
    from project_ensemble.research.human_references import HumanReferenceRetriever
    from project_ensemble.research.documents import LiteratureBundleManager
    from project_ensemble.research.models import EvidenceSource, EvidenceUseClass

    source = tmp_path / "reading note.md"
    source.write_text("# Method note\n\nThe measured interface width depends on the sampling window.\n", encoding="utf-8")
    selection = StartupSelection(
        meeting_type=MeetingType.RESEARCH, providers=("fake",), models=(),
        chair_model=None, task_description="Check the interface-width claim.",
        escalation_email=None, research_enabled=True,
        research_model=("fake", "m1"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        human_reference_paths=(str(source),),
    )
    repo = start_meeting(config(tmp_path), selection,
                         governance_docs=tmp_path, output_directory=tmp_path / "review")
    manifest = json.loads((repo.root / "public/human_references/manifest.json").read_text())
    record = manifest["references"][0]
    assert record["evidence_status"] == "CANDIDATE_NOT_VERIFIED"
    assert (repo.root / record["file_path"]).read_bytes() == source.read_bytes()
    result = HumanReferenceRetriever(repo).retrieve_exploratory("interface width sampling window")
    assert result.effective_backend_ids == ("human_reference",)
    assert len(result.candidates) == 1
    assert "sampling window" in result.candidates[0]["abstract"]
    candidate = result.candidates[0]
    source_record = EvidenceSource(
        source_id=candidate["source_id"], title=candidate["title"],
        url=candidate["url"], evidence_use_class=EvidenceUseClass.PROVISIONAL,
    )
    archived = LiteratureBundleManager(repo=repo, fetcher=object()).archive_sources(
        packet_id="RP-LOCAL", sources=[source_record], candidates=[candidate],
    )[0]
    assert archived.archive_status.value == "ARCHIVED"
    assert archived.archived_sha256 == record["file_sha256"]
    assert (repo.root / archived.archived_path).read_bytes() == source.read_bytes()


def test_start_freezes_reported_model_concurrency_with_safe_fallback(tmp_path):
    import json

    cfg = config(tmp_path)
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "reported"), ("fake", "unknown")),
        chair_model=("fake", "reported"),
        task_description="task",
        escalation_email=None,
    )
    repo = start_meeting(
        cfg,
        selection,
        governance_docs=tmp_path,
        output_directory=tmp_path / "workspace",
        model_catalog=[
            ModelDescriptor(
                provider_id="fake",
                model_id="reported",
                max_concurrent_requests=3,
            ),
            ModelDescriptor(provider_id="fake", model_id="unknown"),
        ],
    )
    manifest = json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text()
    )
    assert manifest["model_concurrency_limits"] == {
        "fake:reported": 3,
        "fake:unknown": 1,
    }
    assert manifest["model_concurrency_sources"] == {
        "fake:reported": "PROVIDER_CATALOG",
        "fake:unknown": "SAFE_FALLBACK",
    }


def test_configured_model_concurrency_overrides_provider_catalog(tmp_path):
    import json

    cfg = config(tmp_path)
    cfg.providers["fake"].model_max_concurrent_requests = {"m1": 2}
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"),),
        chair_model=("fake", "m1"),
        task_description="task",
        escalation_email=None,
    )
    repo = start_meeting(
        cfg,
        selection,
        governance_docs=tmp_path,
        output_directory=tmp_path / "workspace",
        model_catalog=[
            ModelDescriptor(
                provider_id="fake", model_id="m1", max_concurrent_requests=5
            )
        ],
    )
    manifest = json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text()
    )
    assert manifest["model_concurrency_limits"] == {"fake:m1": 2}
    assert manifest["model_concurrency_sources"] == {"fake:m1": "CONFIGURED"}


def test_maximum_parallelism_freezes_four_in_flight_calls_per_representative_model(tmp_path):
    import json

    cfg = config(tmp_path)
    cfg.providers["fake"].model_max_concurrent_requests = {"m1": 1}
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"), ("fake", "m2"), ("fake", "m3")),
        chair_model=("fake", "chair"),
        task_description="task",
        escalation_email=None,
        maximum_parallelism=True,
    )
    repo = start_meeting(
        cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "workspace"
    )
    manifest = json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text()
    )
    assert manifest["maximum_parallelism"] is True
    assert manifest["model_concurrency_limits"] == {
        "fake:m1": 4,
        "fake:m2": 4,
        "fake:m3": 4,
        "fake:chair": 1,
    }
    assert all(
        manifest["model_concurrency_sources"][f"fake:m{index}"] == "HUMAN_MAX_PARALLEL_4"
        for index in (1, 2, 3)
    )


def test_maximum_parallelism_matches_research_desk_task_limit(tmp_path):
    import json

    cfg = config(tmp_path)
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"), ("fake", "m2")),
        chair_model=("fake", "chair"),
        task_description="task",
        escalation_email=None,
        maximum_parallelism=True,
        research_enabled=True,
        research_model=("fake", "desk"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        research_max_concurrent_claim_groups=7,
    )
    repo = start_meeting(
        cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "workspace"
    )
    manifest = json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text()
    )
    assert manifest["model_concurrency_limits"] == {
        "fake:m1": 7,
        "fake:m2": 7,
        "fake:desk": 7,
        "fake:chair": 1,
    }
    assert manifest["model_concurrency_sources"] == {
        "fake:m1": "PARALLELISM_MATCHED_TO_RESEARCH_DESK",
        "fake:m2": "PARALLELISM_MATCHED_TO_RESEARCH_DESK",
        "fake:desk": "PARALLELISM_MATCHED_TO_RESEARCH_DESK",
        "fake:chair": "SAFE_FALLBACK",
    }


def test_maximum_parallelism_uses_configured_research_desk_default(tmp_path):
    import json

    cfg = config(tmp_path)
    cfg.research.max_concurrent_claim_groups = 6
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"),),
        chair_model=("fake", "chair"),
        task_description="task",
        escalation_email=None,
        maximum_parallelism=True,
        research_enabled=True,
        research_model=("fake", "desk"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
    )
    repo = start_meeting(
        cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "workspace"
    )
    manifest = json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text()
    )
    assert manifest["model_concurrency_limits"] == {
        "fake:m1": 6,
        "fake:desk": 6,
        "fake:chair": 1,
    }


def test_initial_model_concurrency_override_applies_to_all_selected_models(tmp_path):
    cfg = config(tmp_path)
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"), ("fake", "m2")),
        chair_model=("fake", "chair"),
        task_description="task",
        escalation_email=None,
        maximum_parallelism=True,
        model_concurrency_limit=2,
    )
    repo = start_meeting(
        cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "workspace"
    )
    manifest = json.loads((repo.root / "identity_private/meeting_manifest.json").read_text())
    assert manifest["model_concurrency_limits"] == {
        "fake:m1": 2, "fake:m2": 2, "fake:chair": 2,
    }
    assert set(manifest["model_concurrency_sources"].values()) == {
        "HUMAN_INITIALIZATION_OVERRIDE"
    }


def test_start_persists_chinese_task_as_utf8(tmp_path):
    cfg = config(tmp_path)
    task = "分析二维二元液体中的 Gaussian 速率分布与特殊阻尼方案。"
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"),),
        chair_model=("fake", "m1"),
        task_description=task,
        escalation_email=None,
    )
    repo = start_meeting(cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "chinese")
    persisted = (repo.root / "public/task.json").read_text(encoding="utf-8")
    assert task in persisted
    assert "\\u5206\\u6790" not in persisted


def test_start_allows_optional_email_transport(tmp_path):
    cfg = config(tmp_path, email_enabled=False)
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"),),
        chair_model=("fake", "m1"),
        task_description="task",
        escalation_email="human@example.test",
    )
    repo = start_meeting(cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "workspace")
    assert repo.escalation_email() == "human@example.test"
    assert repo.events.verify()


def test_start_freezes_separate_reasoning_controls_and_private_research_runtime(tmp_path):
    cfg = config(tmp_path)
    cfg.providers["fake"].reasoning_effort_transport = "openai"
    cfg.providers["fake"].reasoning_effort_map = {
        "low": "low",
        "medium": "medium",
        "high": "high",
    }
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "m1"),),
        chair_model=("fake", "chair"),
        task_description="task",
        escalation_email=None,
        representative_reasoning_effort=ReasoningEffort.LOW,
        chair_reasoning_effort=ReasoningEffort.HIGH,
        research_enabled=True,
        research_model=("fake", "research"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
        openalex_max_results_per_query=50,
        openalex_quota_policy="wait",
        research_max_concurrent_claim_groups=3,
    )

    repo = start_meeting(
        cfg,
        selection,
        governance_docs=tmp_path,
        output_directory=tmp_path / "workspace",
    )

    import json

    private_manifest = json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text()
    )
    assert private_manifest["openalex_quota_policy"] == "wait"
    public_manifest = json.loads((repo.root / "public/meeting_manifest.json").read_text())
    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    assert private_manifest["representative_reasoning_effort"] == "low"
    assert private_manifest["chair_reasoning_effort"] == "high"
    assert private_manifest["research_model"] == ["fake", "research"]
    assert private_manifest["research_reasoning_effort"] == "medium"
    assert private_manifest["openalex_max_results_per_query"] == 50
    assert private_manifest["research_max_concurrent_claim_groups"] == 3
    assert public_manifest["research_enabled"] is True
    assert "research_model" not in public_manifest
    assert {item["runtime"]["reasoning_setting"] for item in registry} == {"low"}
    from project_ensemble.cli import _build_research_retriever

    cfg.research.max_results_per_query = 12
    assert _build_research_retriever(cfg, repo).max_results_per_query == 50


def test_mixed_model_reasoning_uses_union_and_collapses_unsupported_model(tmp_path):
    cfg = config(tmp_path)
    cfg.providers["rich"] = ProviderConfig(
        kind="openai_compatible",
        base_url="https://unused.invalid",
        api_key_env="RICH_API_KEY",
        reasoning_effort_transport="openai",
        reasoning_effort_map={"low": "low", "medium": "medium", "high": "high"},
    )
    options = TerminalWizard._reasoning_options(
        cfg, (("rich", "reasoner"), ("fake", "default-only"))
    )
    assert [value for value, _ in options] == ["default", "low", "medium", "high"]
    assert collapse_reasoning_effort(
        cfg, ("fake", "default-only"), ReasoningEffort.HIGH
    ) == ReasoningEffort.DEFAULT

    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("rich", "fake"),
        models=(("rich", "reasoner"), ("fake", "default-only")),
        chair_model=("rich", "reasoner"),
        task_description="task",
        escalation_email=None,
        representative_reasoning_effort=ReasoningEffort.HIGH,
        chair_reasoning_effort=ReasoningEffort.HIGH,
    )
    repo = start_meeting(
        cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "mixed"
    )
    import json

    manifest = json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text()
    )
    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    assert manifest["representative_reasoning_effort"] == "high"
    assert manifest["representative_reasoning_effective"] == {
        "rich:reasoner": "high",
        "fake:default-only": "default",
    }
    actual = {
        (item["runtime"]["provider_id"], item["runtime"]["model_id"]): item[
            "runtime"
        ]["reasoning_setting"]
        for item in registry
    }
    assert actual[("rich", "reasoner")] == "high"
    assert actual[("fake", "default-only")] == "default"


def test_research_only_start_has_no_representatives_or_chair(tmp_path):
    cfg = config(tmp_path)
    selection = StartupSelection(
        meeting_type=MeetingType.RESEARCH,
        providers=("fake",),
        models=(),
        chair_model=None,
        task_description="标准毛细波分析要求界面可表示为单值高度场。",
        escalation_email=None,
        research_enabled=True,
        research_model=("fake", "m1"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
    )
    repo = start_meeting(
        cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "research"
    )
    import json

    public = json.loads((repo.root / "public/meeting_manifest.json").read_text())
    private = json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text()
    )
    assert public["meeting_type"] == "research"
    assert repo.meeting_id.startswith("LR-")
    assert public["participant_count"] == 0
    assert private["chair_model"] is None
    assert private["research_model"] == ["fake", "m1"]
    assert not (repo.root / "identity_private/representative_registry.json").exists()


def test_audit_start_creates_one_fresh_member_per_model(tmp_path):
    cfg = config(tmp_path)
    selection = StartupSelection(
        meeting_type=MeetingType.AUDIT,
        providers=("fake",),
        models=(("fake", "m1"), ("fake", "m2")),
        chair_model=("fake", "m1"),
        task_description="Audit prior meetings",
        escalation_email="human@example.test",
    )
    repo = start_meeting(cfg, selection, governance_docs=tmp_path, output_directory=tmp_path / "workspace")
    import json

    public = json.loads((repo.root / "public/audit_members.json").read_text())
    private = json.loads((repo.root / "identity_private/audit_member_registry.json").read_text())
    assert len(public) == len(private) == 2
    assert repo.meeting_id.startswith("AU-")
    assert all(x["audit_member_id"].startswith("AUD-") for x in public)
    assert all("persona" not in x for x in private)
    assert not (repo.root / "identity_private/representative_registry.json").exists()


def test_noninteractive_models_must_come_from_live_discovery(tmp_path):
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION,
        providers=("fake",),
        models=(("fake", "missing"),),
        chair_model=("fake", "available"),
        task_description="task",
        escalation_email="human@example.test",
    )
    with pytest.raises(ValueError, match="not returned"):
        assert_models_were_discovered(
            selection,
            [ModelDescriptor(provider_id="fake", model_id="available")],
        )
