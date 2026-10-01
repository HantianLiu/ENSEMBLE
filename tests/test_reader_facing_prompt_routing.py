import json
from types import SimpleNamespace

from project_ensemble.orchestration.literature_report_execution import (
    GlobalEvidenceSummary, LiteratureReportExecutionRunner, ModuleDraft,
    WholeReportSynthesis,
)
from project_ensemble.orchestration.literature_fast import FastBreadthSearchPlan, FastWholeSynthesis
from project_ensemble.orchestration.literature_writing_v071 import (
    ModuleWritingOutline, OutlineBallot, WriterChapter,
)
from project_ensemble.orchestration.readability_policy import reader_facing_prose_contract
from project_ensemble.orchestration.scholarly_rendering import (
    ChairScienceRevision, RenderedSection, RenderingPlan, ScholarlyRenderingRunner,
)


def _capture_engine():
    calls = []

    def invoke(_participant_id, **kwargs):
        calls.append(kwargs)
        return object()

    engine = SimpleNamespace(
        find_recorded_response=lambda *_args, **_kwargs: None,
        invoke_participant=invoke,
        validate_structured_response=lambda *_args, **_kwargs: object(),
    )
    return engine, calls


def test_reader_contract_uses_named_levels_and_preserves_evidence_boundaries():
    contract = reader_facing_prose_contract(
        {"signposting_1_to_5": 4, "segmentation_1_to_5": 3,
         "liveliness_1_to_5": 2},
        {"软物质物理": 2},
    )
    assert '"ordinary_level":4' in contract
    assert '"complex_level":5' in contract
    assert '"软物质物理"' in contract
    assert "压缩重复，不压缩异议或不确定性" in contract
    assert "不能藏进注释" in contract
    assert "不得照抄提示词" in contract
    assert "题名级" in contract
    assert "这些词只是在写作规则中用于识别不合适的表达" in contract
    assert "临床试验注册" in contract
    assert "尚无研究测量 X" in contract


def test_literature_direct_prose_calls_receive_contract_but_evidence_summary_does_not(tmp_path):
    public = tmp_path / "public/literature_report"
    public.mkdir(parents=True)
    (public / "writing_preferences.json").write_text(
        json.dumps({"signposting_1_to_5": 3}), encoding="utf-8",
    )
    (public / "audience_profile-01.json").write_text(
        json.dumps({"disciplines": {"药学": 1}}), encoding="utf-8",
    )
    runner = object.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine, calls = _capture_engine()
    runner.max_output_tokens = None

    for schema in (ModuleDraft, WholeReportSynthesis, FastWholeSynthesis, WriterChapter):
        runner._invoke_service("WRITER", stage="test", schema=schema,
                               system="原阶段权限。", user={})
    runner._invoke_service("RESEARCH_DESK", stage="test", schema=GlobalEvidenceSummary,
                           system="证据摘要。", user={})

    for call in calls[:4]:
        assert "读者正文写作契约" in call["system_text"]
        assert '"药学"' in call["system_text"]
        assert '"ordinary_level":3' in call["system_text"]
        assert "不得原样搬入正文" in call["system_text"]
    assert calls[4]["system_text"] == "证据摘要。"


def test_legacy_representative_writer_receives_contract(tmp_path):
    runner = object.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine, calls = _capture_engine()
    runner.max_output_tokens = None
    runner.resolver = SimpleNamespace(representative_context_spec=lambda **_kwargs: object())
    runner.assembler = SimpleNamespace(assemble=lambda _spec: "冻结的阶段规则。")
    runner.prompt_family = "literature_research"

    runner._invoke_representative(
        {"representative_id": "R-ONE", "runtime": {"persona": "librarian"}},
        stage="literature_report_draft", schema=WholeReportSynthesis,
        public_files=(), user_prefix="撰写导读。",
    )
    assert "读者正文写作契约" in calls[0]["user_text"]


def test_planning_structured_calls_receive_scoped_prose_and_authority_rules(tmp_path):
    public = tmp_path / "public/literature_report"
    public.mkdir(parents=True)
    (public / "writing_preferences.json").write_text(
        json.dumps({"signposting_1_to_5": 3}), encoding="utf-8",
    )
    (public / "audience_profile-01.json").write_text(
        json.dumps({"disciplines": {"统计物理": 2}}), encoding="utf-8",
    )
    runner = object.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(root=tmp_path)
    runner.engine, calls = _capture_engine()
    runner.max_output_tokens = None

    for schema in (FastBreadthSearchPlan, ModuleWritingOutline, OutlineBallot):
        runner._invoke_service(
            "WRITER", stage="planning-test", schema=schema,
            system="原规划权限。", user={"search_queries": ["exact query"]},
        )

    for call in calls:
        assert "结构化结果的自由文本表达规则" in call["system_text"]
        assert "规划权限边界" in call["system_text"]
        assert "不得决定联网能力" in call["system_text"]
        assert '"ordinary_level":3' in call["system_text"]
        assert '"complex_level":4' in call["system_text"]
        assert '"统计物理"' in call["system_text"]
        assert "读者正文写作契约" not in call["system_text"]
        assert '"search_queries"' in call["user_text"]
        assert '"exact query"' in call["user_text"]


def test_scholarly_rendering_prose_and_revisions_receive_contract_only(tmp_path):
    runner = object.__new__(ScholarlyRenderingRunner)
    runner.manifest = {"rendering_segmentation": 2, "rendering_liveliness": 3}
    runner.engine, calls = _capture_engine()
    runner.max_output_tokens = None
    for schema in (RenderedSection, ChairScienceRevision, RenderingPlan):
        runner._invoke("CHAIR", stage="test", schema=schema,
                       system="冻结原文不可改变。", user={})
    for call in calls[:2]:
        assert "读者正文写作契约" in call["system_text"]
        assert '"level":2' in call["system_text"]
    assert calls[2]["system_text"] == "冻结原文不可改变。"
