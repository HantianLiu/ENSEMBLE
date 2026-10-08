import io
import json
import re
from types import SimpleNamespace

import pytest

from project_ensemble.domain import DeliverableType, GenerationResponse, ReasoningEffort
from project_ensemble.errors import RepresentativeUnavailableError, ResearchRequestRejectedError
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.literature_report_execution import (
    CoverageAssessment,
    FormalModuleReview,
    LiteratureReportExecutionRunner,
    ModuleDraft,
    ModuleQuestionSubmission,
    _source_identity_key,
)
from project_ensemble.orchestration.literature_style import (
    FORMULA_READER_RULES,
    FORMULA_REVIEW_RULES,
    LITERATURE_WRITING_RULES,
    is_identifier_only_rewrite,
    leaked_internal_identifiers,
    replace_reader_module_ids,
)
from project_ensemble.orchestration.literature_report import OutlineModule
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.research.models import (
    DocumentArchiveStatus,
    EvidenceSource,
    EvidenceUseClass,
)
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.runtime.progress import ConsoleProgressReporter


class NoopNotifier:
    def send_escalation(self, **kwargs):
        return True


def test_literature_writing_contract_requires_source_backed_quantitative_definitions():
    assert "数学定义" in LITERATURE_WRITING_RULES
    assert "量纲及单位或约化单位" in LITERATURE_WRITING_RULES
    assert "输入控制参数、实测量和推导估计量" in LITERATURE_WRITING_RULES
    assert "热浴设定温度和实测动能温度" in LITERATURE_WRITING_RULES
    assert "不得凭模型记忆补造" in LITERATURE_WRITING_RULES
    assert "公式表每项应能独立理解" in FORMULA_READER_RULES
    assert "定义方向或符号约定" in FORMULA_READER_RULES
    assert "不得凭学科常识或模型记忆补猜" in FORMULA_READER_RULES
    assert "检查稿件是否清楚保留这一限制" in FORMULA_REVIEW_RULES


def test_report_body_length_excludes_references_and_appendices():
    report = (
        "# 标题\n\n## 引言\n\n证据支持结论。\n\n"
        "## 未解决问题附录\n\n此处不计。\n\n## 参考文献\n\n[1] 文献。\n"
    )
    actual = LiteratureReportExecutionRunner._count_report_body_characters(report)
    assert actual == len("#标题##引言证据支持结论。")


def test_front_glossary_does_not_consume_body_length_budget():
    report = (
        "# 标题\n\n## 摘要\n\n摘要。\n\n## 术语表\n\n"
        "- **线张力**：这是独立预算内的较长定义。\n\n"
        "## 引言\n\n正文应继续计数。\n\n## 参考文献\n\n[1] 文献。\n"
    )
    actual = LiteratureReportExecutionRunner._count_report_body_characters(report)
    assert actual == len("#标题##摘要摘要。##引言正文应继续计数。")


def test_full_synthesis_normalizes_c_source_ids_without_changing_frozen_writer_text(tmp_path):
    repo = make_repo(tmp_path)
    original = "public/literature_report/synthesis/v1.json"
    repo.docs.write_once(original, json.dumps({
        "title": "Report", "abstract": "Supported [C00001-00001].",
        "introduction": "", "methods": "", "cross_module_synthesis": "",
        "conclusion": "", "cited_packet_ids": ["C00001-00001"],
    }))
    repo.docs.write_once(
        "public/literature_report/modules/RM-01/research/chapter_citation_catalog.json",
        json.dumps({"sources": [{"citation_id": "C00001-00001", "packet_ids": ["RP-ONE"]}]}),
    )
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    runner.repo = repo
    checked = []
    runner._validate_citations = lambda identifiers: checked.append(identifiers)
    original_bytes = (repo.root / original).read_bytes()
    derivative = runner._normalize_whole_synthesis_source_metadata(
        repo.root / original, [{"module_id": "RM-01"}],
    )
    assert derivative != repo.root / original
    assert json.loads(derivative.read_text(encoding="utf-8"))["cited_packet_ids"] == []
    assert checked == [[]]
    assert (repo.root / original).read_bytes() == original_bytes
    assert runner._normalize_whole_synthesis_source_metadata(
        repo.root / original, [{"module_id": "RM-01"}],
    ) == derivative


def test_assembly_omits_unbacked_unresolved_item_without_blocking(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    missing = "无法找到原始证明的开放问题"
    supported = "RP-SUPPORTED"
    draft_path = "public/literature_report/modules/RM-01/drafts/test.json"
    repo.docs.write_once(draft_path, json.dumps({
        "title": "测试模块", "body_markdown": "已核对的正文。",
        "short_summary": "摘要。", "cited_packet_ids": [],
        "unresolved_ids": [missing, supported],
    }, ensure_ascii=False))
    synthesis_path = "public/literature_report/synthesis/test.json"
    repo.docs.write_once(synthesis_path, json.dumps({
        "title": "测试报告", "abstract": "", "introduction": "", "methods": "",
        "cross_module_synthesis": "", "conclusion": "", "cited_packet_ids": [],
    }, ensure_ascii=False))
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk(),
    )
    monkeypatch.setattr(
        runner, "_load_packet",
        lambda item: SimpleNamespace(unresolved_questions=["有证据支持的待解问题？"])
        if item == supported else None,
    )
    report = runner._assemble_report_markdown(
        None, [{"module_id": "RM-01", "status": "ADOPTED", "draft_path": draft_path}],
        repo.root / synthesis_path, footnotes=[],
    )
    assert "有证据支持的待解问题？" in report
    assert missing not in report
    records = list((repo.root / "audit_private/literature_report/publication_omitted_unresolved")
                   .glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["item"] == missing


def test_assembly_renders_known_citations_added_in_unresolved_appendix(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    packet_id = "RP-SUPPORTED"
    citation_id = "C00001-00001"
    source = EvidenceSource(
        source_id="SOURCE-1", title="A relevant review", publication_year=2024,
        url="https://example.org/review", evidence_use_class=EvidenceUseClass.REVIEW,
    )
    repo.docs.write_once(
        "public/literature_report/modules/RM-01/research/chapter_citation_catalog.json",
        json.dumps({"sources": [{"citation_id": citation_id, "packet_ids": [packet_id],
                                 "doi": None, "url": source.url}]}),
    )
    draft_path = "public/literature_report/modules/RM-01/drafts/test.json"
    repo.docs.write_once(draft_path, json.dumps({
        "title": "Evidence", "body_markdown": "Supported statement.",
        "short_summary": "Brief summary.", "cited_packet_ids": [],
        "unresolved_ids": [packet_id],
    }, ensure_ascii=False))
    synthesis_path = "public/literature_report/synthesis/test.json"
    repo.docs.write_once(synthesis_path, json.dumps({
        "title": "Test report", "abstract": "", "introduction": "", "methods": "",
        "cross_module_synthesis": "", "conclusion": "", "cited_packet_ids": [],
    }, ensure_ascii=False))
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk(),
    )
    monkeypatch.setattr(
        runner, "_load_packet",
        lambda _packet_id: SimpleNamespace(
            unresolved_questions=[f"Question retained for review [{citation_id}]."],
            sources=[source],
        ),
    )

    report = runner._assemble_report_markdown(
        None, [{"module_id": "RM-01", "status": "ADOPTED", "draft_path": draft_path}],
        repo.root / synthesis_path, footnotes=[],
    )

    assert f"Question retained for review [1]." in report
    assert "A relevant review" in report
    assert citation_id not in report


def test_glossary_queries_share_the_first_round_six_question_limit():
    submission = ModuleQuestionSubmission(
        questions=["本章核心研究问题？"],
        glossary_questions=["术语的原始操作性定义是什么？"],
    )
    assert submission.glossary_questions
    with pytest.raises(ValueError, match="total exceeds six"):
        ModuleQuestionSubmission(
            questions=[f"普通问题 {index}" for index in range(6)],
            glossary_questions=["额外术语问题"],
        )


@pytest.mark.parametrize("formula", [
    r"\gamma = \partial F / \partial L",
    "$$\n" + r"\gamma = \partial F / \partial L" + "\n$$\n" + r"其中 \(F\) 是自由能。",
])
def test_science_reviewed_rolling_glossary_is_published_before_body(tmp_path, formula):
    repo = make_repo(tmp_path)
    glossary_path = "public/literature_report/writing_v071/glossary_after_RM-01.json"
    repo.docs.write_once(glossary_path, json.dumps([{
        "term": "线张力", "explanation_mode": "FORMULA",
        "explanation": "二维界面中，线张力表示在指定统计状态和形变约束下，界面长度变化的响应系数；它不能与任意轮廓长度比值混同。",
        "formula": formula,
        "source_citation_ids": [],
    }], ensure_ascii=False))
    draft_path = "public/literature_report/modules/RM-01/drafts/final.json"
    repo.docs.write_once(draft_path, json.dumps({
        "title": "目标量", "body_markdown": "本章先界定线张力。",
        "short_summary": "界面定义。", "cited_packet_ids": [],
    }, ensure_ascii=False))
    synthesis_path = "public/literature_report/synthesis/glossary-test.json"
    repo.docs.write_once(synthesis_path, json.dumps({
        "title": "线张力综述", "abstract": "摘要内容。", "introduction": "引言内容。",
        "methods": "", "cross_module_synthesis": "", "conclusion": "",
        "cited_packet_ids": [],
    }, ensure_ascii=False))
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk(),
    )
    runner.manifest["literature_writing_policy"] = "fast"
    markdown = runner._assemble_report_markdown(
        None, [{"module_id": "RM-01", "status": "ADOPTED", "draft_path": draft_path,
                "glossary_path": glossary_path}], repo.root / synthesis_path, footnotes=[],
    )
    assert markdown.index("## 摘要") < markdown.index("## 术语表") < markdown.index("## 引言")
    assert "界面长度变化的响应系数" in markdown
    assert r"\gamma = \partial F / \partial L" in markdown
    assert "\n$$\n$$\n" not in markdown
    from project_ensemble.orchestration.academic_html import render_academic_review_html
    rendered = render_academic_review_html(markdown, meeting_id=repo.meeting_id)
    article = rendered.split("<main><article>", 1)[1].split("</article>", 1)[0]
    assert article.count('<div class="math display"') == 1
    assert r'data-tex="\(' not in article
    if "其中" in formula:
        assert "是自由能。" in article
    assert runner._count_report_body_characters(markdown) < len("".join(markdown.split()))


class FakeBundle:
    def rebuild_download_bundle(self):
        return None


class FakeResearchDesk:
    def __init__(self):
        self.literature_bundle = FakeBundle()

    def research(self, *args, **kwargs):  # pragma: no cover - STOP path should not retrieve
        raise AssertionError("the scripted follow-up chose STOP")


class ExecutionAdapter(ScriptedProviderAdapter):
    def __init__(self):
        super().__init__("fake", ["m"])

    def generate(self, request):
        text = request.user_text
        if '"scope_summary"' in text:
            version = re.search(r'"version"\s*:\s*"(G\d+)"', text)
            payload = {
                "version": version.group(1) if version else "G0",
                "scope_summary": "Evidence-bounded global summary.",
                "completed_modules": [],
                "cross_module_links": [],
                "unresolved_ids": [],
                "evidence_index_notes": [],
                "source_packet_ids": [],
            }
        elif '"normalized_claim"' in text and '"packet_ids"' in text:
            qid = re.search(r'"question_id"\s*:\s*"([^"]+)"', text)
            payload = {
                "question_id": qid.group(1),
                "normalized_claim": "A bounded test claim.",
                "status": "NOT_SATISFIED",
                "packet_ids": [],
                "rationale": "The empty evidence index does not answer it.",
            }
        elif '"body_markdown"' in text and '"short_summary"' in text and '"abstract"' not in text:
            payload = {
                "title": "Evidence module",
                "body_markdown": "The available test evidence is limited.",
                "short_summary": "A bounded module summary.",
                "cited_packet_ids": [],
                "inference_labels": [],
                "assumption_labels": [],
                "unresolved_ids": [],
            }
        elif all(key in text for key in ('"abstract"', '"introduction"', '"methods"')):
            payload = {
                "title": "Test literature review",
                "abstract": "A concise abstract.",
                "introduction": "A concise introduction.",
                "methods": "Evidence was handled through the governed workflow.",
                "cross_module_synthesis": "One module was reviewed.",
                "conclusion": "The conclusion remains bounded by the evidence.",
                "cited_packet_ids": [],
            }
        elif '"patches"' in text:
            payload = {"patches": []}
        elif '"position"' in text and '"opposition"' in text:
            payload = {"position": "ACCEPT", "opposition": None}
        elif '"ranked_option_ids"' in text:
            payload = {"ranked_option_ids": ["unused"]}
        elif '"target_text"' in text and '"replacement_text"' in text:
            payload = {
                "action": "NO_AMENDMENT",
                "target_text": None,
                "replacement_text": None,
                "rationale": None,
            }
        elif '"dissent"' in text:
            payload = {"dissent": None}
        elif '"fact_neutral"' in text:
            payload = {"fact_neutral": True}
        elif '"READABLE"' in text and '"REVISION_REQUIRED"' in text:
            payload = {"status": "READABLE", "issues": []}
        elif '"vote"' in text and '"YES"' in text:
            payload = {"vote": "YES"}
        elif '"issue"' in text and '"RAISE_ISSUE"' in text:
            payload = {"action": "NO_OBJECTION", "issue": None}
        elif '"issues"' in text and '"NO_OBJECTION"' in text:
            payload = {"action": "NO_OBJECTION", "issues": []}
        elif '"requests"' in text and '"REFINE"' in text:
            payload = {"action": "STOP", "requests": []}
        elif '"questions"' in text:
            payload = {"questions": []}
        else:
            raise AssertionError(f"unhandled schema request: {text[-1000:]}")
        return GenerationResponse(
            text=json.dumps(payload), provider_id="fake", model_id=request.model_id
        )


class PatchAdapter(ExecutionAdapter):
    def generate(self, request):
        if '"patches"' in request.user_text:
            return GenerationResponse(
                text=json.dumps(
                    {
                        "patches": [
                            {
                                "patch_id": "CHAIR-P1",
                                "kind": "READABILITY",
                                "original_text": "alpha wording",
                                "revised_text": "clear wording",
                                "rationale": "Improve readability without changing meaning.",
                                "knowledge_status": "ASSUMPTION",
                                "citation_refs": [],
                            }
                        ]
                    }
                ),
                provider_id="fake",
                model_id=request.model_id,
            )
        return super().generate(request)


class RequeueAdapter(ExecutionAdapter):
    def __init__(self):
        super().__init__()
        self.followup_submissions = 0

    def generate(self, request):
        if "Research Desk 认为一项请求" in request.user_text:
            return GenerationResponse(
                text=json.dumps({"claim": "A concrete revised external factual claim."}),
                provider_id="fake",
                model_id=request.model_id,
            )
        if '"requests"' in request.user_text and '"REFINE"' in request.user_text:
            self.followup_submissions += 1
            payload = (
                {
                    "action": "REQUEST",
                    "requests": [
                        {
                            "action": "EXPAND",
                            "claim": "Please re-judge internal coverage and list packet IDs.",
                            "rationale": "Coverage needs review.",
                        },
                        {
                            "action": "EXPAND",
                            "claim": "A second concrete external factual claim.",
                            "rationale": "This claim needs evidence.",
                        },
                    ],
                }
                if self.followup_submissions == 1
                else {"action": "STOP", "requests": []}
            )
            return GenerationResponse(
                text=json.dumps(payload), provider_id="fake", model_id=request.model_id
            )
        return super().generate(request)


class RequeueResearchDesk:
    def __init__(self):
        self.claims = []
        self.literature_bundle = FakeBundle()

    def research(self, request, **kwargs):
        self.claims.append(request.claim)
        if request.claim.startswith("Please re-judge"):
            raise ResearchRequestRejectedError("administrative request")
        return SimpleNamespace(
            packet_id=f"RP-{len(self.claims):04d}",
            knowledge_status=SimpleNamespace(value="SOURCE_BACKED"),
        )


class RejectingModelPriorDesk:
    def __init__(self, *, fail_if_called=False):
        self.calls = 0
        self.fail_if_called = fail_if_called
        self.literature_bundle = FakeBundle()

    def research(self, request, **kwargs):
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("a persisted Research Desk rejection must be restored")
        raise ResearchRequestRejectedError(
            "The sentence is an internal procedural constraint, not an external empirical claim."
        )


def make_repo(tmp_path, *, research_parallelism=None):
    repo = MeetingRepository.create(
        tmp_path,
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Produce a complete evidence-bounded literature review.",
        research_enabled=True,
        research_model=("fake", "m"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        research_max_concurrent_claim_groups=research_parallelism,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
    )
    repo.docs.write_once(
        "public/literature_report/frozen_research_outline.json",
        json.dumps(
            {
                "report_title": "Test review",
                "scope_note": "A bounded test scope.",
                "modules": [
                    {
                        "module_id": "RM-01",
                        "title": "Evidence module",
                        "research_questions": ["What evidence is available?"],
                        "included_scope": ["test evidence"],
                        "excluded_scope": ["unsupported claims"],
                        "required_evidence": ["traceable sources"],
                        "cross_module_links": [],
                        "source_submission_refs": ["R-TEST:M1"],
                    }
                ],
                "clustering_notes": [],
                "review_dispositions": [],
            },
            ensure_ascii=False,
        ),
    )
    return repo


def test_question_submission_enforces_per_representative_limit():
    with pytest.raises(ValueError):
        ModuleQuestionSubmission(questions=[f"question {index}" for index in range(7)])


def test_coverage_assessment_accepts_audited_provider_extras_and_long_rationale():
    assessment = CoverageAssessment.model_validate(
        {
            "question_id": "RM-01-Q05",
            "normalized_claim": "A bounded coverage question.",
            "status": "NOT_SATISFIED",
            "packet_ids": [],
            "rationale": "x" * 1200,
            "packet_ids_note": "No relevant packet IDs available.",
            "additional_notes": None,
        }
    )

    assert assessment.packet_ids == []
    assert len(assessment.rationale) == 1200
    assert "packet_ids_note" not in assessment.model_dump()


def test_module_draft_keeps_epistemic_metadata_without_machine_headings():
    draft = ModuleDraft.model_validate(
        {
            "title": "Bounded draft",
            "body_markdown": "The body refers to INF-01 and ASM-01.",
            "short_summary": "Bounded summary.",
            "cited_packet_ids": [],
            "inference_labels": [
                "INF-01: inference text（推论（建议额外核查））。"
            ],
            "assumption_labels": ["ASM-01: assumption text（分析假设）。"],
            "unresolved_ids": [],
            "model_prior_claims": [],
        }
    )

    assert draft.body_markdown == "The body refers to INF-01 and ASM-01."
    assert draft.inference_labels and draft.assumption_labels


def test_nonfinal_review_can_carry_advisory_style_note_without_substantive_issue():
    review = FormalModuleReview(action="NO_OBJECTION", issues=[], style_note="缩短第二段。")
    assert review.action == "NO_OBJECTION"
    assert review.style_note == "缩短第二段。"


def test_chapter_citation_validation_derives_packet_metadata_from_inline_sources(tmp_path):
    records = []
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(
        meeting_id="LR-TEST",
        events=SimpleNamespace(append=lambda *args, **kwargs: records.append((args, kwargs))),
    )
    module = OutlineModule(
        module_id="RM-01", title="Evidence", research_questions=["What is known?"],
        required_evidence=["Primary literature"], source_submission_refs=["S-1"],
    )
    catalog = tmp_path / "chapter_citation_catalog.json"
    catalog.write_text(json.dumps({"sources": [{
        "citation_id": "C1-1", "packet_ids": ["RP-FIRST"],
    }]}), encoding="utf-8")
    draft = ModuleDraft(
        title="Evidence", body_markdown="First claim [C1-1].",
        short_summary="Summary.", cited_packet_ids=["RP-FIRST", "RP-SECOND"],
    )
    normalized = runner._validate_chapter_source_citations(module, draft, catalog)
    assert normalized.cited_packet_ids == ["RP-FIRST"]
    assert records[0][0][1]["removed_packet_ids"] == ["RP-SECOND"]
    draft.body_markdown += " Second claim [RP-SECOND]."
    assert runner._validate_chapter_source_citations(
        module, draft, catalog
    ).cited_packet_ids == ["RP-FIRST", "RP-SECOND"]

    omitted = ModuleDraft(
        title="Evidence", body_markdown="First claim [C1-1].",
        short_summary="Summary.", cited_packet_ids=[],
    )
    inferred = runner._validate_chapter_source_citations(module, omitted, catalog)
    assert inferred.cited_packet_ids == ["RP-FIRST"]
    assert records[-1][0][1]["chapter_source_to_packets"] == {"C1-1": ["RP-FIRST"]}

    # Existing meetings can contain a frozen Writer response that filled the
    # legacy packet field with the visible C source ID. It is metadata, not an
    # unknown evidence packet, and must be resolved without altering the prose.
    visible_id_in_packet_field = ModuleDraft(
        title="Evidence", body_markdown="First claim [C1-1].",
        short_summary="Summary.", cited_packet_ids=["C1-1"],
    )
    normalized_visible_id = runner._validate_chapter_source_citations(
        module, visible_id_in_packet_field, catalog,
    )
    assert normalized_visible_id.cited_packet_ids == ["RP-FIRST"]
    assert normalized_visible_id.body_markdown == visible_id_in_packet_field.body_markdown


def test_chapter_citation_validation_removes_unused_catalog_packet_metadata(tmp_path):
    records = []
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(
        meeting_id="LR-TEST",
        events=SimpleNamespace(append=lambda *args, **kwargs: records.append((args, kwargs))),
    )
    module = OutlineModule(
        module_id="RM-03", title="Evidence", research_questions=["What is known?"],
        required_evidence=["Primary literature"], source_submission_refs=["S-1"],
    )
    catalog = tmp_path / "chapter_citation_catalog.json"
    catalog.write_text(json.dumps({"sources": [
        {"citation_id": "C3-1", "packet_ids": ["RP-USED"]},
        {"citation_id": "C3-2", "packet_ids": ["RP-UNUSED"]},
    ]}), encoding="utf-8")
    original = ModuleDraft(
        title="Evidence", body_markdown="Supported claim [C3-1].",
        short_summary="Summary.", cited_packet_ids=["RP-USED", "RP-UNUSED"],
    )

    normalized = runner._validate_chapter_source_citations(module, original, catalog)

    assert original.cited_packet_ids == ["RP-USED", "RP-UNUSED"]
    assert normalized.cited_packet_ids == ["RP-USED"]
    assert normalized.body_markdown == original.body_markdown
    assert records[0][0][0] == "LITERATURE_CHAPTER_CITATION_PROVENANCE_NORMALIZED"
    assert records[0][0][1]["removed_packet_ids"] == ["RP-UNUSED"]


def test_chapter_citation_validation_preserves_distinct_packets_for_same_source(tmp_path):
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    module = OutlineModule(
        module_id="RM-02", title="Evidence", research_questions=["What is known?"],
        required_evidence=["Primary literature"], source_submission_refs=["S-1"],
    )
    catalog = tmp_path / "chapter_citation_catalog.json"
    catalog.write_text(json.dumps({"sources": [{
        "citation_id": "C2-1", "packet_ids": ["RP-CLAIM-A", "RP-CLAIM-B"],
    }]}), encoding="utf-8")
    draft = ModuleDraft(
        title="Evidence", body_markdown="Two claims from one source [C2-1].",
        short_summary="Summary.", cited_packet_ids=["RP-CLAIM-A", "RP-CLAIM-B"],
    )

    normalized = runner._validate_chapter_source_citations(module, draft, catalog)

    assert normalized.cited_packet_ids == ["RP-CLAIM-A", "RP-CLAIM-B"]


def test_chapter_citation_validation_keeps_all_ambiguous_source_mappings(tmp_path):
    records = []
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(
        meeting_id="LR-TEST",
        events=SimpleNamespace(append=lambda *args, **kwargs: records.append((args, kwargs))),
    )
    module = OutlineModule(
        module_id="RM-02", title="Evidence", research_questions=["What is known?"],
        required_evidence=["Primary literature"], source_submission_refs=["S-1"],
    )
    catalog = tmp_path / "chapter_citation_catalog.json"
    catalog.write_text(json.dumps({"sources": [{
        "citation_id": "C2-1", "packet_ids": ["RP-CLAIM-A", "RP-CLAIM-B"],
    }]}), encoding="utf-8")
    draft = ModuleDraft(
        title="Evidence", body_markdown="Supported claim [C2-1].",
        short_summary="Summary.", cited_packet_ids=[],
    )

    normalized = runner._validate_chapter_source_citations(module, draft, catalog)

    assert normalized.cited_packet_ids == ["RP-CLAIM-A", "RP-CLAIM-B"]
    assert records[0][0][1]["chapter_source_to_packets"] == {
        "C2-1": ["RP-CLAIM-A", "RP-CLAIM-B"]
    }


def test_chapter_citation_validation_rejects_unknown_or_unmapped_source(tmp_path):
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    module = OutlineModule(
        module_id="RM-02", title="Evidence", research_questions=["What is known?"],
        required_evidence=["Primary literature"], source_submission_refs=["S-1"],
    )
    catalog = tmp_path / "chapter_citation_catalog.json"
    catalog.write_text(json.dumps({"sources": [{
        "citation_id": "C2-1", "packet_ids": [],
    }]}), encoding="utf-8")
    unknown = ModuleDraft(
        title="Evidence", body_markdown="Unsupported [C2-2].",
        short_summary="Summary.", cited_packet_ids=[],
    )
    with pytest.raises(ValueError, match="unknown chapter sources"):
        runner._validate_chapter_source_citations(module, unknown, catalog)
    unmapped = ModuleDraft(
        title="Evidence", body_markdown="Unsupported [C2-1].",
        short_summary="Summary.", cited_packet_ids=[],
    )
    with pytest.raises(ValueError, match="no evidence packet"):
        runner._validate_chapter_source_citations(module, unmapped, catalog)


def test_chair_may_remove_redundant_legacy_packet_marker_without_stranding_metadata(tmp_path):
    records = []
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(
        meeting_id="LR-TEST",
        events=SimpleNamespace(append=lambda *args, **kwargs: records.append((args, kwargs))),
    )
    module = OutlineModule(
        module_id="RM-01", title="Evidence", research_questions=["What is known?"],
        required_evidence=["Primary literature"], source_submission_refs=["S-1"],
    )
    catalog = tmp_path / "chapter_citation_catalog.json"
    catalog.write_text(json.dumps({"sources": [{
        "citation_id": "C1-1", "packet_ids": ["RP-FIRST"],
    }]}), encoding="utf-8")
    builder = ModuleDraft(
        title="Evidence", body_markdown="Supported claim [C1-1][RP-EXTRA].",
        short_summary="Summary.", cited_packet_ids=["RP-FIRST", "RP-EXTRA"],
    )
    integrated = ModuleDraft(
        title="Evidence", body_markdown="Supported claim [C1-1].",
        short_summary="Summary.", cited_packet_ids=["RP-FIRST", "RP-EXTRA"],
    )

    normalized = runner._validate_chapter_source_citations(
        module, integrated, catalog, previous_draft=builder
    )

    assert normalized.cited_packet_ids == ["RP-FIRST"]
    assert normalized.body_markdown == integrated.body_markdown
    assert integrated.cited_packet_ids == ["RP-FIRST", "RP-EXTRA"]
    assert records[0][0][0] == "LITERATURE_CHAPTER_CITATION_PROVENANCE_NORMALIZED"
    assert records[0][0][1]["removed_packet_ids"] == ["RP-EXTRA"]


def test_chair_cannot_drop_packet_metadata_without_an_existing_chapter_citation(tmp_path):
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    module = OutlineModule(
        module_id="RM-01", title="Evidence", research_questions=["What is known?"],
        required_evidence=["Primary literature"], source_submission_refs=["S-1"],
    )
    catalog = tmp_path / "chapter_citation_catalog.json"
    catalog.write_text(json.dumps({"sources": [{
        "citation_id": "C1-1", "packet_ids": ["RP-FIRST"],
    }]}), encoding="utf-8")
    builder = ModuleDraft(
        title="Evidence", body_markdown="First claim [C1-1]. Second claim [RP-EXTRA].",
        short_summary="Summary.", cited_packet_ids=["RP-FIRST", "RP-EXTRA"],
    )
    integrated = ModuleDraft(
        title="Evidence", body_markdown="First claim [C1-1]. Second claim.",
        short_summary="Summary.", cited_packet_ids=["RP-FIRST", "RP-EXTRA"],
    )

    with pytest.raises(ValueError, match="RP-EXTRA"):
        runner._validate_chapter_source_citations(
            module, integrated, catalog, previous_draft=builder
        )


def test_internal_identifier_lint_does_not_reject_ordinary_scholarly_identifiers():
    assert leaked_internal_identifiers("DOI 10.1000/example；PMID 1234；RM-01 与 RP-ABC") == [
        "RM-01", "RP-ABC"
    ]
    assert leaked_internal_identifiers("This assumption is limited. ASSUMPTION is an internal code.") == [
        "ASSUMPTION"
    ]


def test_reader_prose_prompt_separates_workflow_language_from_scholarly_terms():
    from project_ensemble.orchestration.literature_style import (
        LITERATURE_WRITING_RULES, READER_PROSE_LEXICON_RULES,
    )

    assert "不得照抄提示词" in READER_PROSE_LEXICON_RULES
    assert "不要把多个逻辑关系压成名词链" in READER_PROSE_LEXICON_RULES
    assert "题名级" in READER_PROSE_LEXICON_RULES
    assert "记账" in READER_PROSE_LEXICON_RULES
    assert "不是审查记录的转述者" in READER_PROSE_LEXICON_RULES
    assert "段落围绕一个主要问题组织" in READER_PROSE_LEXICON_RULES
    assert "句数和句长随论证需要变化" in READER_PROSE_LEXICON_RULES
    assert "相变中的冻结" in READER_PROSE_LEXICON_RULES
    assert "C00007-XXXX" in READER_PROSE_LEXICON_RULES
    assert "熵账" in READER_PROSE_LEXICON_RULES
    assert "禁止空泛的自我介绍、写作过程说明和目录预告" in READER_PROSE_LEXICON_RULES
    assert "不得把来源定义冲突写成‘原文矛盾、本文统一处理’" in READER_PROSE_LEXICON_RULES
    assert "你是把经过核对的研究内容写给读者" not in LITERATURE_WRITING_RULES
    assert not leaked_internal_identifiers("相变发生冻结；该证据只能从题名确认。")


def test_reader_report_replaces_module_handles_even_with_unicode_dashes():
    original = "RM-01 与 RM–02 相互参照；RM-xx 尚待确认。"
    repaired, trace = replace_reader_module_ids(
        original, {"RM-01": 2, "RM-02": 3}, language="zh",
    )
    assert repaired == "第 2 章 与 第 3 章 相互参照；相关研究章节 尚待确认。"
    assert [item["original"] for item in trace] == ["RM-01", "RM–02", "RM-xx"]
    assert not leaked_internal_identifiers(repaired)


def test_lightweight_identifier_repair_cannot_change_scientific_prose():
    original = "RM-01 的结果仅支持有限条件下的结论 [1]。"
    assert is_identifier_only_rewrite(original, "本章的结果仅支持有限条件下的结论 [1]。")
    assert not is_identifier_only_rewrite(original, "本章的结果普遍支持无条件结论 [1]。")
    assert not is_identifier_only_rewrite(original, "本章的结果仅支持有限条件下的结论 [2]。")


def test_final_references_follow_first_textual_appearance_and_deduplicate_work():
    packet_map, chapter_map, lines, trace = (
        LiteratureReportExecutionRunner._number_sources_by_first_appearance(
            prose=["Later source [C1-2]. Earlier source [C1-1][RP-FIRST]."],
            packet_numbers={"RP-FIRST": [1]},
            chapter_numbers={"C1-1": 1, "C1-2": 2},
            reference_lines=["[1] Earlier.", "", "[2] Later.", ""],
            trace=[{"reference_number": 1}, {"reference_number": 2}],
        )
    )
    assert chapter_map == {"C1-1": 2, "C1-2": 1}
    assert packet_map["RP-FIRST"] == [2]
    assert lines == ["[1] Later.", "", "[2] Earlier.", ""]
    assert [item["reference_number"] for item in trace] == [1, 2]


def test_first_appearance_keeps_chapter_ids_inside_prose_bearing_brackets():
    packet_map, chapter_map, lines, trace = (
        LiteratureReportExecutionRunner._number_sources_by_first_appearance(
            prose=["Conflicting reports [C00009-00050 对 C00009-00051]。"],
            packet_numbers={},
            chapter_numbers={"C00009-00050": 1, "C00009-00051": 2},
            reference_lines=["[1] First source.", "", "[2] Second source.", ""],
            trace=[{"reference_number": 1}, {"reference_number": 2}],
        )
    )
    assert packet_map == {}
    assert chapter_map == {"C00009-00050": 1, "C00009-00051": 2}
    assert lines == ["[1] First source.", "", "[2] Second source.", ""]
    assert [item["reference_number"] for item in trace] == [1, 2]


def test_source_identity_uses_strong_ids_before_url_and_never_fuzzy_title():
    assert _source_identity_key("https://doi.org/10.1000/X", "https://one") == (
        _source_identity_key("doi:10.1000/x", "https://two")
    )
    assert _source_identity_key(None, "https://pubmed.ncbi.nlm.nih.gov/12345/") == "pmid:12345"
    assert _source_identity_key(None, "https://arxiv.org/pdf/2501.12345v2") == "arxiv:2501.12345"


def test_module_draft_normalizes_grouped_packet_citations():
    draft = ModuleDraft.model_validate(
        {
            "title": "Bounded draft",
            "body_markdown": "Supported statement [RP-AAAA, RP-BBBB].",
            "short_summary": "Bounded summary.",
            "cited_packet_ids": ["RP-AAAA", "RP-BBBB"],
            "inference_labels": [],
            "assumption_labels": [],
            "unresolved_ids": [],
            "model_prior_claims": [],
        }
    )

    assert draft.body_markdown == "Supported statement [RP-AAAA][RP-BBBB]."
    assert LiteratureReportExecutionRunner._render_packet_citations(
        draft.body_markdown,
        {"RP-AAAA": [1], "RP-BBBB": [2, 3]},
    ) == "Supported statement [1, 2, 3]."


def test_module_draft_normalizes_grouped_chapter_citations_and_final_numbers():
    draft = ModuleDraft(
        title="Chapter", body_markdown="Two sources [C1-1, C1-2].",
        short_summary="Same pair [C1-1; C1-2].", cited_packet_ids=["RP-AAAA"],
    )
    assert draft.body_markdown == "Two sources [C1-1][C1-2]."
    assert draft.short_summary == "Same pair [C1-1][C1-2]."
    assert LiteratureReportExecutionRunner._render_packet_citations(
        draft.body_markdown, {}, chapter_citation_map={"C1-1": 2, "C1-2": 4},
    ) == "Two sources [2, 4]."


def test_chinese_grouped_temporary_citations_never_reach_reader_text():
    draft = ModuleDraft(
        title="Chapter",
        body_markdown="两项结果 [C00001-00008；C00001-00010、C00001-00077]。",
        short_summary="另见 [C00001-00008，C00001-00010]。",
        cited_packet_ids=[],
    )
    assert draft.body_markdown == (
        "两项结果 [C00001-00008][C00001-00010][C00001-00077]。"
    )
    assert LiteratureReportExecutionRunner._render_packet_citations(
        draft.body_markdown, {}, chapter_citation_map={
            "C00001-00008": 3, "C00001-00010": 7, "C00001-00077": 12,
        },
    ) == "两项结果 [3, 7, 12]。"
    assert LiteratureReportExecutionRunner._render_packet_citations(
        "记录 [RP-AAAA；RP-BBBB]。", {"RP-AAAA": [1], "RP-BBBB": [2]},
    ) == "记录 [1, 2]。"
    assert LiteratureReportExecutionRunner._render_packet_citations(
        "单条证据（[C00001-00008]）与另一条证据（[RP-AAAA]）。",
        {"RP-AAAA": [9]}, chapter_citation_map={"C00001-00008": 8},
    ) == "单条证据[8]与另一条证据[9]。"
    assert LiteratureReportExecutionRunner._render_packet_citations(
        "正文。（[C00001-00008]；[C00001-00010]；[C00001-00077]）", {},
        chapter_citation_map={
            "C00001-00008": 5, "C00001-00010": 6, "C00001-00077": 7,
        },
    ) == "正文。[5, 6, 7]"
    with pytest.raises(ValueError, match="unresolved internal citation"):
        LiteratureReportExecutionRunner._render_packet_citations(
            "遗漏 [C00001-00099；C00001-00100]。", {},
            chapter_citation_map={"C00001-00099": 1},
        )


def test_mixed_bracket_groups_render_temporary_citations_without_leaking_ids():
    render = LiteratureReportExecutionRunner._render_packet_citations
    mapping = {"C00001-00036": 39, "C00001-00045": 40}

    assert render(
        "Sources [C00001-00036, [40], C00001-00045].", {},
        chapter_citation_map=mapping,
    ) == "Sources [39, 40]."
    assert render(
        "Paired [C00001-00036 对 [40], C00001-00045].", {},
        chapter_citation_map=mapping,
    ) == "Paired [[39] 对 [40], [40]]."


def test_assembly_maps_bare_catalog_ids_without_changing_frozen_trace(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    source_one = EvidenceSource(
        source_id="SOURCE-1", title="First source", publication_year=2024,
        url="https://example.org/first", evidence_use_class=EvidenceUseClass.REVIEW,
    )
    source_two = EvidenceSource(
        source_id="SOURCE-2", title="Second source", publication_year=2025,
        url="https://example.org/second", evidence_use_class=EvidenceUseClass.REVIEW,
    )
    packets = {
        "RP-FIRST": SimpleNamespace(sources=[source_one]),
        "RP-SECOND": SimpleNamespace(sources=[source_two]),
    }
    repo.docs.write_once(
        "public/literature_report/modules/RM-01/research/chapter_citation_catalog.json",
        json.dumps({"chapter_number": 1, "sources": [
            {"citation_id": "C00001-00001", "packet_ids": ["RP-FIRST"],
             "doi": None, "url": "https://example.org/first"},
            {"citation_id": "C00001-00002", "packet_ids": ["RP-SECOND"],
             "doi": None, "url": "https://example.org/second"},
        ]}),
    )
    synthesis = "public/literature_report/synthesis/test.json"
    repo.docs.write_once(synthesis, json.dumps({
        "title": "Test report", "abstract": "", "introduction": "", "methods": "",
        "cross_module_synthesis": "", "conclusion": "", "cited_packet_ids": [],
    }))
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk(),
    )
    monkeypatch.setattr(runner, "_load_packet", lambda packet_id: packets.get(packet_id))
    first_draft = "public/literature_report/modules/RM-01/drafts/first.json"
    repo.docs.write_once(first_draft, json.dumps({
        "title": "Evidence", "body_markdown": "First [C00001-00001].",
        "short_summary": "Brief summary.", "cited_packet_ids": ["RP-FIRST"],
    }))
    runner._assemble_report_markdown(
        None, [{"module_id": "RM-01", "status": "CONFIRMED", "draft_path": first_draft}],
        repo.root / synthesis, footnotes=[],
    )
    frozen_trace = (repo.root / "public/literature_report/citation_trace.json").read_bytes()
    second_draft = "public/literature_report/modules/RM-01/drafts/second.json"
    repo.docs.write_once(second_draft, json.dumps({
        "title": "Evidence", "body_markdown": "Second C00001-00002; first [C00001-00001].",
        "short_summary": "Brief summary.", "cited_packet_ids": ["RP-FIRST"],
    }))
    report = runner._assemble_report_markdown(
        None, [{"module_id": "RM-01", "status": "CONFIRMED", "draft_path": second_draft}],
        repo.root / synthesis, footnotes=[],
    )
    assert "Second [1]; first [2]." in report
    assert "C00001-" not in report
    assert (repo.root / "public/literature_report/citation_trace.json").read_bytes() == frozen_trace
    supplemental = json.loads((repo.root / "public/literature_report/citation_trace_assembly_v4.json")
                              .read_text(encoding="utf-8"))
    assert supplemental["style"] == "NUMBERED_FIRST_APPEARANCE_V4"
    assert len(supplemental["references"]) == 2


def test_assembly_canonicalizes_chapter_citation_padding_and_fullwidth_brackets(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    sources = {
        "RP-FIRST": EvidenceSource(
            source_id="SOURCE-1", title="First source", publication_year=2024,
            url="https://example.org/first", evidence_use_class=EvidenceUseClass.REVIEW,
        ),
        "RP-SECOND": EvidenceSource(
            source_id="SOURCE-2", title="Second source", publication_year=2025,
            url="https://example.org/second", evidence_use_class=EvidenceUseClass.REVIEW,
        ),
    }
    repo.docs.write_once(
        "public/literature_report/modules/RM-02/research/chapter_citation_catalog.json",
        json.dumps({"chapter_number": 2, "sources": [
            {"citation_id": "C00002-00001", "packet_ids": ["RP-FIRST"],
             "doi": None, "url": "https://example.org/first"},
            {"citation_id": "C00002-00002", "packet_ids": ["RP-SECOND"],
             "doi": None, "url": "https://example.org/second"},
        ]}),
    )
    synthesis_path = "public/literature_report/synthesis/citation-aliases.json"
    repo.docs.write_once(synthesis_path, json.dumps({
        "title": "Test report", "abstract": "Synthesis【C2-1】.",
        "introduction": "", "methods": "", "cross_module_synthesis": "",
        "conclusion": "", "cited_packet_ids": [],
    }, ensure_ascii=False))
    draft_path = "public/literature_report/modules/RM-02/drafts/citation-aliases.json"
    repo.docs.write_once(draft_path, json.dumps({
        "title": "Evidence", "body_markdown": "Claim【C00002-0002】.",
        "short_summary": "Summary [C00002-0001].", "cited_packet_ids": [],
    }, ensure_ascii=False))
    original_draft = (repo.root / draft_path).read_bytes()
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk(),
    )
    monkeypatch.setattr(runner, "_writing_preferences", lambda: {
        "language": "zh", "full_abstract": True, "section_abstracts": True,
    })
    monkeypatch.setattr(
        runner, "_load_packet",
        lambda packet_id: SimpleNamespace(sources=[sources[packet_id]]),
    )

    report = runner._assemble_report_markdown(
        None, [{"module_id": "RM-02", "status": "ADOPTED", "draft_path": draft_path}],
        repo.root / synthesis_path, footnotes=[],
    )

    assert "Synthesis[1]." in report
    assert "Claim[2]." in report
    assert "Summary [1]." in report
    assert "First source" in report and "Second source" in report
    assert "C00002-" not in report
    assert (repo.root / draft_path).read_bytes() == original_draft


def test_assembly_renders_chapter_ids_inside_prose_bearing_brackets(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    packet_id = "RP-PAIR"
    sources = [
        EvidenceSource(
            source_id="SOURCE-50", title="First source", publication_year=2024,
            url="https://example.org/first", evidence_use_class=EvidenceUseClass.REVIEW,
        ),
        EvidenceSource(
            source_id="SOURCE-51", title="Second source", publication_year=2025,
            url="https://example.org/second", evidence_use_class=EvidenceUseClass.REVIEW,
        ),
    ]
    repo.docs.write_once(
        "public/literature_report/modules/RM-09/research/chapter_citation_catalog.json",
        json.dumps({"chapter_number": 9, "sources": [
            {"citation_id": "C00009-00050", "packet_ids": [packet_id],
             "doi": None, "url": sources[0].url},
            {"citation_id": "C00009-00051", "packet_ids": [packet_id],
             "doi": None, "url": sources[1].url},
        ]}),
    )
    synthesis_path = "public/literature_report/synthesis/prose-citation-ids.json"
    repo.docs.write_once(synthesis_path, json.dumps({
        "title": "Test report", "abstract": "", "introduction": "", "methods": "",
        "cross_module_synthesis": "", "conclusion": "", "cited_packet_ids": [],
    }, ensure_ascii=False))
    draft_path = "public/literature_report/modules/RM-09/drafts/prose-citation-ids.json"
    repo.docs.write_once(draft_path, json.dumps({
        "title": "Evidence", "body_markdown": "Conflicting reports [C00009-00050 对 C00009-00051]。",
        "short_summary": "Brief summary.", "cited_packet_ids": [],
    }, ensure_ascii=False))
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk(),
    )
    monkeypatch.setattr(
        runner, "_load_packet",
        lambda _packet_id: SimpleNamespace(sources=sources),
    )

    report = runner._assemble_report_markdown(
        None, [{"module_id": "RM-09", "status": "ADOPTED", "draft_path": draft_path}],
        repo.root / synthesis_path, footnotes=[],
    )

    assert "[1] 对 [2]" in report
    assert "First source" in report and "Second source" in report
    assert "C00009-00050" not in report and "C00009-00051" not in report


@pytest.mark.parametrize("graphics_available", [True, False])
def test_programmatic_figure_citations_are_assembled_and_published(tmp_path, monkeypatch, graphics_available):
    from project_ensemble.orchestration import academic_figures
    native_renderer = academic_figures.render_figure
    if not graphics_available:
        def broken_renderer(_spec):
            raise RuntimeError("可选绘图器不可用")
        monkeypatch.setattr(academic_figures, "render_figure", broken_renderer)
    repo = make_repo(tmp_path)
    source = EvidenceSource(source_id="SOURCE-1", title="Measured data", publication_year=2024,
                            url="https://example.org/data", evidence_use_class=EvidenceUseClass.REVIEW)
    packet = SimpleNamespace(sources=[source])
    repo.docs.write_once("public/literature_report/modules/RM-01/research/chapter_citation_catalog.json",
                         json.dumps({"sources": [{"citation_id": "C1-1", "packet_ids": ["RP-DATA"],
                                                   "doi": None, "url": source.url}]}))
    figure = dict(id="response", kind="bar", title="响应比较", caption="共同口径下的比较。",
                  alt_text="第二组响应更高。", evidence_basis="extracted", data_note="可读表一直接给出的值。",
                  source_citation_ids=["C1-1"], x_label="组别", y_label="响应 (nm)",
                  categories=["A", "B"], series=[dict(label="响应", y=[1, 2])])
    draft_path = "public/literature_report/modules/RM-01/drafts/figures.json"
    repo.docs.write_once(draft_path, ModuleDraft(title="结果", body_markdown="同口径比较如下。\n\n[[FIGURE:response]]",
                                               short_summary="比较两组。", figures=[figure]).model_dump_json())
    synthesis_path = "public/literature_report/synthesis/test.json"
    repo.docs.write_once(synthesis_path, json.dumps({"title": "Test figures", "abstract": "", "introduction": "",
                                                   "methods": "", "cross_module_synthesis": "", "conclusion": "",
                                                   "cited_packet_ids": []}))
    runner = LiteratureReportExecutionRunner(
        repo=repo, engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk())
    monkeypatch.setattr(runner, "_load_packet", lambda _packet_id: packet)
    monkeypatch.setattr(runner, "_compact_evidence_index", lambda: [])
    completed = [{"module_id": "RM-01", "status": "CONFIRMED", "draft_path": draft_path}]
    original = (repo.root / draft_path).read_bytes()
    markdown = runner._assemble_report_markdown(None, completed, repo.root / synthesis_path, footnotes=[])
    assert "C1-1" not in markdown and "[[FIGURE:" not in markdown
    assert "figures/generated-" in markdown and "[1]" in markdown
    result = runner._publish(markdown, completed, 0)
    assert result.status == "HANDOFF_READY" and result.final_pdf_path
    html = (repo.root / result.final_html_path).read_text()
    assert ('class="ensemble-figure"' in html) == graphics_available
    manifest = json.loads((repo.root / result.audit_manifest_path).read_text())
    assert manifest["rendered_figure_count"] == int(graphics_available)
    assert manifest["figure_text_fallback_count"] == int(not graphics_available)
    assert (repo.root / manifest["figure_manifest_path"]).is_file()
    assert (repo.root / draft_path).read_bytes() == original
    monkeypatch.setattr(academic_figures, "render_figure", native_renderer)
    assert runner._publish(markdown, completed, 0).final_markdown_path == result.final_markdown_path


def test_assembly_nests_module_headings_without_touching_code_blocks():
    body = (
        "# RM-03 Mechanism map (v4)\n\n"
        "## §1 Scope\n\n### §1.1 Detail\n\n"
        "```python\n# this is code, not a heading\n```"
    )
    normalized = LiteratureReportExecutionRunner._normalize_module_markdown(body, "RM-03")
    assert "# RM-03 Mechanism map" not in normalized
    assert "#### §1 Scope" in normalized
    assert "##### §1.1 Detail" in normalized
    assert "# this is code, not a heading" in normalized


def test_assembly_recovers_inline_packet_citations_and_marks_source_less_packet(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    source = EvidenceSource(
        source_id="SOURCE-1", title="A relevant review", authors=["A. Researcher"],
        publication_year=2025, doi="10.1000/review",
        url="https://example.org/review", evidence_use_class=EvidenceUseClass.REVIEW,
    )
    packets = {
        "RP-WITHSOURCE": SimpleNamespace(
            sources=[source], knowledge_status=SimpleNamespace(value="SOURCE_BACKED")
        ),
        "RP-NOSOURCE": SimpleNamespace(
            sources=[], knowledge_status=SimpleNamespace(value="UNRESOLVED")
        ),
    }
    draft_relative = "public/literature_report/modules/RM-01/drafts/assembly-test.json"
    repo.docs.write_once(draft_relative, json.dumps({
        "title": "Evidence module",
        "body_markdown": (
            "# RM-01 Evidence module\n\n## §1 Findings\n\n"
            "Supported [C1-1]; unresolved [RP-NOSOURCE]."
        ),
        "short_summary": "Bounded summary.",
        "cited_packet_ids": ["RP-WITHSOURCE"],
        "inference_labels": [], "assumption_labels": [], "unresolved_ids": [],
    }))
    repo.docs.write_once(
        "public/literature_report/modules/RM-01/research/chapter_citation_catalog.json",
        json.dumps({"chapter_number": 1, "sources": [{
            "citation_id": "C1-1", "source_id": "SOURCE-1",
            "packet_ids": ["RP-WITHSOURCE"], "doi": "10.1000/review",
            "url": "https://example.org/review",
        }]}),
    )
    synthesis_relative = "public/literature_report/assembly-synthesis-test.json"
    repo.docs.write_once(synthesis_relative, json.dumps({
        "title": "Assembly test", "abstract": "Abstract.",
        "introduction": "Introduction [C1-1；C1-1].", "methods": "Methods.",
        "cross_module_synthesis": "Synthesis.", "conclusion": "Conclusion.",
        "cited_packet_ids": [],
    }))
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk(),
    )
    monkeypatch.setattr(runner, "_load_packet", lambda packet_id: packets.get(packet_id))
    markdown = runner._assemble_report_markdown(
        None, [{"module_id": "RM-01", "status": "CONFIRMED", "draft_path": draft_relative}],
        repo.root / synthesis_relative, footnotes=[],
    )
    assert "### 1. Evidence module" in markdown
    assert "# RM-01 Evidence module" not in markdown
    assert "#### §1 Findings" in markdown
    assert "Supported [1]" in markdown
    assert "Introduction [1]" in markdown
    assert "C1-1" not in markdown
    assert "[RP-WITHSOURCE]" not in markdown
    assert "（本次检索未取得可编号的公开来源）" in markdown
    assert "RP-NOSOURCE" not in markdown
    assert "文献追溯与审计说明" not in markdown
    supplement = json.loads((repo.root / "public/literature_report/citation_trace_assembly_v3.json")
                            .read_text(encoding="utf-8"))
    assert supplement["unnumbered_packet_ids"] == ["RP-NOSOURCE"]
    assert supplement["references"][0]["packet_ids"] == ["RP-WITHSOURCE"]


def test_approved_article_sections_control_assembly_without_forced_abstract(tmp_path):
    repo = make_repo(tmp_path)
    repo.docs.write_once(
        "public/literature_report/writing_preferences.json",
        json.dumps({"language": "en", "full_abstract": False, "section_abstracts": True}),
    )
    draft_path = "public/literature_report/modules/RM-01/drafts/final.json"
    repo.docs.write_once(draft_path, json.dumps({
        "title": "Research chapter", "body_markdown": "A bounded finding.",
        "short_summary": "Chapter evidence in brief.", "cited_packet_ids": [],
    }))
    synthesis_path = "public/literature_report/synthesis/final.json"
    repo.docs.write_once(synthesis_path, json.dumps({
        "title": "Reader report", "abstract": "This should remain hidden.",
        "body_sections": [
            {"kind": "TEXT", "heading": "Why this matters", "body_markdown": "Opening argument."},
            {"kind": "MODULES", "heading": "Evidence", "body_markdown": ""},
            {"kind": "TEXT", "heading": "What remains", "body_markdown": "A bounded close."},
        ],
    }))
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(repo=repo, adapters={"fake": ExecutionAdapter()}, notifier=NoopNotifier()),
        governance_docs="docs/governance", research_desk=FakeResearchDesk(),
    )
    markdown = runner._assemble_report_markdown(
        None, [{"module_id": "RM-01", "status": "CONFIRMED", "draft_path": draft_path}],
        repo.root / synthesis_path, footnotes=[],
    )
    assert "This should remain hidden" not in markdown
    assert markdown.index("## Why this matters") < markdown.index("## Evidence")
    assert markdown.index("## Evidence") < markdown.index("## What remains")
    assert "Chapter summary" in markdown
    assert "RM-01" not in markdown


def test_literature_reference_uses_academic_entry_without_audit_metadata():
    source = EvidenceSource(
        source_id="SOURCE-1",
        title="A bounded scientific result",
        authors=["A. Researcher", "B. Scholar"],
        publication_year=2025,
        doi="10.1000/bounded",
        url="https://example.org/bounded",
        venue="Journal of Bounded Results",
        evidence_use_class=EvidenceUseClass.PRIMARY,
        archive_status=DocumentArchiveStatus.NOT_AVAILABLE,
    )

    rendered = LiteratureReportExecutionRunner._format_academic_reference(source)

    assert rendered == (
        "A. Researcher, B. Scholar. A bounded scientific result. "
        "*Journal of Bounded Results*. 2025. https://doi.org/10.1000/bounded."
    )
    assert "证据包" not in rendered
    assert "PRIMARY" not in rendered


def test_one_module_literature_report_reaches_visible_publication(tmp_path):
    repo = make_repo(tmp_path)
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ExecutionAdapter()},
        notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    result = LiteratureReportExecutionRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        research_desk=FakeResearchDesk(),
        max_output_tokens=4000,
    ).run()

    assert result.status == "HANDOFF_READY"
    assert result.completed_module_count == 1
    assert result.contested_module_count == 0
    assert (repo.root / result.final_markdown_path).is_file()
    assert (repo.root / result.final_pdf_path).read_bytes().startswith(b"%PDF")
    assert result.final_html_path is not None
    assert (repo.root / result.final_html_path).read_text(encoding="utf-8").startswith("<!doctype html>")
    assert (repo.root / "LITERATURE_REVIEW.html").resolve() == (
        repo.root / result.final_html_path
    ).resolve()
    readability = json.loads(
        (repo.root / "public/literature_report/chair_readability_certification.json").read_text()
    )
    assert readability["status"] == "READABLE"
    assert (repo.root / "LITERATURE_REVIEW.md").resolve() == (
        repo.root / result.final_markdown_path
    ).resolve()
    ballot = json.loads(
        (repo.root / "public/literature_report/modules/RM-01/ballots/confirmation1.json").read_text()
    )
    assert ballot["active_count"] == 4
    assert ballot["required_yes_votes"] == 3
    assert ballot["yes_votes"] == 4
    positions = json.loads(
        (repo.root / "public/literature_report/final_position_tally.json").read_text()
    )
    assert positions["publication_policy"] == "UNCONDITIONAL"


def test_html_publication_survives_pdf_renderer_failure(tmp_path, monkeypatch):
    from project_ensemble.orchestration import literature_report_execution as execution

    repo = make_repo(tmp_path)
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ExecutionAdapter()},
        notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        research_desk=FakeResearchDesk(),
        max_output_tokens=4000,
    )
    def broken_pdf(*_args, **_kwargs):
        raise ValueError("PDF_MATH_RENDERING_FAILED: test formula")

    monkeypatch.setattr(execution, "render_academic_review_pdf", broken_pdf)
    result = runner.run()
    assert result.status == "HANDOFF_READY"
    assert result.final_pdf_path is None
    html_path = repo.root / "public/final/literature_review_report.html"
    assert html_path.is_file()
    assert (repo.root / "LITERATURE_REVIEW.html").resolve() == html_path.resolve()
    assert (repo.root / "public/final/literature_review_report.md").is_file()
    manifest = json.loads((repo.root / "public/final/literature_review_publication_manifest.json").read_text())
    assert manifest["pdf_render_status"] == "PENDING_REPAIR"
    assert manifest["report_pdf_path"] is None
    assert list((repo.root / "public/final/pdf_render_failures").glob("*.json"))


def test_parallel_specialists_share_one_immutable_builder_snapshot(tmp_path):
    repo = make_repo(tmp_path)
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ExecutionAdapter()},
        notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 3},
    )

    result = LiteratureReportExecutionRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        research_desk=FakeResearchDesk(),
        max_output_tokens=4000,
    ).run()

    assert result.status == "HANDOFF_READY"
    snapshot = (
        repo.root
        / "public/literature_report/modules/RM-01/drafts/v1-builder-source.json"
    )
    assert snapshot.is_file()
    specialist_files = list(
        (
            repo.root
            / "governance_private/literature_report/module_cycles/RM-01/v1"
        ).glob("specialist-*.json")
    )
    assert len(specialist_files) == 3


def test_question_and_followup_panels_rotate_to_different_representatives(tmp_path):
    repo = MeetingRepository.create(
        tmp_path,
        selected_models=[("fake", "m1"), ("fake", "m2"), ("fake", "m3")],
        chair_model=("fake", "m1"),
        governance_docs="docs/governance",
        task_description="Test balanced panel rotation.",
        research_enabled=True,
        research_model=("fake", "m1"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
    )
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(
            repo=repo,
            adapters={"fake": ExecutionAdapter()},
            notifier=NoopNotifier(),
        ),
        governance_docs="docs/governance",
        research_desk=FakeResearchDesk(),
    )
    first = runner._balanced_panel("RM-01", "questions", offset=0)
    second = runner._balanced_panel(
        "RM-01",
        "followup",
        offset=1,
        exclude_ids={item["representative_id"] for item in first},
    )
    assert {item["representative_id"] for item in first}.isdisjoint(
        {item["representative_id"] for item in second}
    )
    assert len({item["runtime"]["persona"] for item in first}) == 4
    assert len({item["runtime"]["persona"] for item in second}) == 4


def test_rejected_followup_is_rewritten_and_requeued_after_other_questions(tmp_path):
    repo = make_repo(tmp_path)
    desk = RequeueResearchDesk()
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(
            repo=repo,
            adapters={"fake": RequeueAdapter()},
            notifier=NoopNotifier(),
        ),
        governance_docs="docs/governance",
        research_desk=desk,
    )
    outline = json.loads(
        (repo.root / "public/literature_report/frozen_research_outline.json").read_text()
    )
    from project_ensemble.orchestration.literature_report import OutlineModule

    module = OutlineModule.model_validate(outline["modules"][0])
    coverage_path = (
        repo.root / "public/literature_report/modules/RM-01/research/coverage.json"
    )
    repo.docs.write_once(
        coverage_path.relative_to(repo.root),
        json.dumps({"assessments": []}),
    )

    paths = runner._run_followups(module, 1, runner.active, coverage_path)

    assert len(paths) == 4
    assert desk.claims == [
        "Please re-judge internal coverage and list packet IDs.",
        "A second concrete external factual claim.",
        "A concrete revised external factual claim.",
    ]
    first = json.loads(paths[0].read_text())
    assert first["outcomes"][0]["status"] == "EVIDENCE_PACKET_AVAILABLE"
    assert first["outcomes"][0]["revision_count"] == 0
    assert first["outcomes"][1]["revision_count"] == 1
    assert "RESEARCH_REQUEST_REVISED_AND_REQUEUED" in repo.events.path.read_text()


def test_non_empirical_model_prior_rejection_does_not_abort_module(tmp_path):
    repo = make_repo(tmp_path)
    desk = RejectingModelPriorDesk()
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(
            repo=repo,
            adapters={"fake": ExecutionAdapter()},
            notifier=NoopNotifier(),
        ),
        governance_docs="docs/governance",
        research_desk=desk,
    )
    from project_ensemble.orchestration.literature_report import OutlineModule

    module = OutlineModule.model_validate(
        json.loads(
            (repo.root / "public/literature_report/frozen_research_outline.json").read_text()
        )["modules"][0]
    )
    draft = ModuleDraft(
        title="Bounded draft",
        body_markdown="This module does not decide policy direction.",
        short_summary="Bounded summary.",
        model_prior_claims=["This module does not decide policy direction."],
    )

    sanitized = runner._verify_model_prior_claims(
        module=module,
        version="v1",
        draft=draft,
        requester_id="R-TEST",
    )

    assert sanitized.model_prior_claims == []
    audit = json.loads(
        (
            repo.root
            / "audit_private/literature_report/model_prior_checks/RM-01/v1.json"
        ).read_text()
    )
    assert audit["checks"][0]["decision"] == "NOT_EXTERNAL_VERIFIABLE_CLAIM"


def test_persisted_model_prior_rejection_is_restored_without_new_call(tmp_path):
    repo = make_repo(tmp_path)
    desk = RejectingModelPriorDesk(fail_if_called=True)
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(
            repo=repo,
            adapters={"fake": ExecutionAdapter()},
            notifier=NoopNotifier(),
        ),
        governance_docs="docs/governance",
        research_desk=desk,
    )
    from project_ensemble.orchestration.literature_report import OutlineModule

    module = OutlineModule.model_validate(
        json.loads(
            (repo.root / "public/literature_report/frozen_research_outline.json").read_text()
        )["modules"][0]
    )
    claim = "This module does not decide policy direction."
    repo.docs.write_once(
        "audit_private/research/requests/RM-01-v1-MODEL-PRIOR-01.json",
        json.dumps(
            {
                "request_id": "RM-01-v1-MODEL-PRIOR-01",
                "request": {
                    "requester_id": "R-TEST",
                    "stage": "LITERATURE_REPORT",
                    "claim": claim,
                    "force_refresh": False,
                    "refresh_reason": None,
                },
                "normalized_claim": {
                    "rejection_reason": "Internal procedure, not an external claim."
                },
                "status": "REJECTED_NOT_CLAIM_SCOPED",
                "packet_path": None,
            }
        ),
    )
    draft = ModuleDraft(
        title="Bounded draft",
        body_markdown=claim,
        short_summary="Bounded summary.",
        model_prior_claims=[claim],
    )

    sanitized = runner._verify_model_prior_claims(
        module=module,
        version="v1",
        draft=draft,
        requester_id="R-TEST",
    )

    assert sanitized.model_prior_claims == []
    assert desk.calls == 0


def test_existing_evidence_coverage_is_released_as_one_batch(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor

    observed_workers = []

    def tracked_executor(*, max_workers):
        observed_workers.append(max_workers)
        return RealThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr(
        "project_ensemble.orchestration.literature_report_execution.ThreadPoolExecutor",
        tracked_executor,
    )
    repo = make_repo(tmp_path, research_parallelism=3)
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ExecutionAdapter()},
        notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    progress_output = io.StringIO()
    engine.progress = ConsoleProgressReporter(progress_output, color=False, live=True)
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        research_desk=FakeResearchDesk(),
    )
    original_invoke = runner._invoke_service
    coverage_attempts = 0

    def fail_one_coverage_attempt(*args, **kwargs):
        nonlocal coverage_attempts
        if (kwargs.get("stage") == "literature_existing_coverage_assessment"
                and kwargs["user"]["question_id"] == "RM-01-Q01"):
            coverage_attempts += 1
            if coverage_attempts == 1:
                raise RepresentativeUnavailableError("temporary outage")
        return original_invoke(*args, **kwargs)

    monkeypatch.setattr(runner, "_invoke_service", fail_one_coverage_attempt)
    question_relative = "public/literature_report/modules/RM-01/research/first_questions.json"
    repo.docs.write_once(
        question_relative,
        json.dumps(
            {
                "questions": [
                    {
                        "question_id": "RM-01-Q01",
                        "question": "Is there direct evidence?",
                        "requester_id": "R-TEST",
                    },
                    {
                        "question_id": "RM-01-Q02",
                        "question": "What are the limitations?",
                        "requester_id": "R-OTHER",
                    },
                ]
            }
        ),
    )
    outline = json.loads(
        (repo.root / "public/literature_report/frozen_research_outline.json").read_text()
    )
    from project_ensemble.orchestration.literature_report import OutlineModule

    module = OutlineModule.model_validate(outline["modules"][0])
    released = runner._assess_coverage(module, repo.root / question_relative)
    payload = json.loads(released.read_text())
    assert payload["status"] == "FROZEN_RELEASED_ATOMICALLY"
    assert [item["question_id"] for item in payload["assessments"]] == [
        "RM-01-Q01",
        "RM-01-Q02",
    ]
    assert observed_workers == [2]
    assert coverage_attempts == 2
    displayed = progress_output.getvalue()
    assert "代表 · 问题已提交" in displayed
    assert "Research Desk · 覆盖检查" in displayed
    assert "已完成 1/1 次核查" in displayed
    assert "自动重提 1/1" in displayed
    assert "R-TEST" in displayed and "R-OTHER" in displayed


def test_chair_wording_patch_requires_librarian_fact_neutral_vote(tmp_path):
    repo = make_repo(tmp_path)
    progress_output = io.StringIO()
    runner = LiteratureReportExecutionRunner(
        repo=repo,
        engine=MeetingEngine(
            repo=repo,
            adapters={"fake": PatchAdapter()},
            notifier=NoopNotifier(),
            configured_concurrency_limits={("fake", "m"): 2},
            progress=ConsoleProgressReporter(progress_output, color=False, live=True),
        ),
        governance_docs="docs/governance",
        research_desk=FakeResearchDesk(),
    )
    revised = runner._chair_patches("# Report\n\nalpha wording\n")
    assert "clear wording" in revised
    assert "alpha wording" not in revised
    audit = json.loads(
        (repo.root / "audit_private/literature_report/publication_patches.json").read_text()
    )
    assert audit["decisions"][0]["applied"] is True
    assert audit["decisions"][0]["required_yes"] == 1
    runner._chair_readability_certify(revised)
    displayed = progress_output.getvalue()
    assert "出版前逐块措辞检查" in displayed
    assert "Chair · 出版稿可读性终检 1/1" in displayed
