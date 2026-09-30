import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import reportlab
from pypdf import PdfReader
from pydantic import ValidationError
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph

from project_ensemble.domain import GenerationResponse
from project_ensemble.orchestration.final_publication import (
    ChairReadabilityCertification,
    FinalPublicationRunner,
    render_publication_pdf,
    _format_numeric_citations,
    _inline_markup,
    _join_markdown_paragraph_lines,
    _markdown_story,
    _pdf_styles,
    _safe_pdf_text,
)
from project_ensemble.orchestration.math_rendering import repair_json_decoded_math_commands
from project_ensemble.storage.meeting import MeetingRepository


class _Progress:
    def status(self, *_args):
        pass

    def info(self, *_args):
        pass


class _Engine:
    def __init__(self, *, status="READABLE"):
        self.status = SimpleNamespace(phase=None, paused_reason=None)
        self.progress = _Progress()
        self.readability_status = status
        self.calls = 0

    def find_recorded_response(self, *_args, **_kwargs):
        return None

    def invoke_participant(self, participant_id, *, user_text, stage, **_kwargs):
        assert participant_id == "CHAIR"
        assert stage == "chair_final_publication_readability"
        self.calls += 1
        source_hash = user_text.split("PUBLICATION SOURCE SHA-256: ", 1)[1].splitlines()[0]
        readable = self.readability_status == "READABLE"
        checks = [
            {
                "name": name,
                "status": "PASS" if readable or name != "NAVIGATION_AND_STRUCTURE" else "FAIL",
                "explanation": "Clear" if readable else "Navigation requires revision",
            }
            for name in (
                "MAIN_RESULT_IDENTIFIABLE",
                "ADVISORY_APPENDICES_SEPARATE",
                "NAVIGATION_AND_STRUCTURE",
                "SOURCE_LABELS_CLEAR",
                "SOURCE_TEXT_LEGIBLE",
            )
        ]
        return GenerationResponse(
            text=json.dumps(
                {
                    "status": self.readability_status,
                    "publication_source_sha256": source_hash,
                    "checks": checks,
                    "required_changes": [] if readable else ["Improve appendix navigation."],
                    "summary": "Publication is readable." if readable else "Revision required.",
                }
            ),
            provider_id="fake",
            model_id="fake-chair",
        )

    def validate_structured_response(self, _participant_id, *, response, schema_model, **_kwargs):
        return schema_model.model_validate_json(response.text)


def _font_for_test() -> Path:
    return Path(reportlab.__file__).resolve().parent / "fonts/Vera.ttf"


def _repo_with_publication_sources(tmp_path: Path) -> tuple[MeetingRepository, dict[str, Path]]:
    repo = MeetingRepository(tmp_path / "M-PUBLICATION")
    repo.docs.write_once(
        "public/meeting_manifest.json",
        json.dumps(
            {
                "meeting_id": "M-PUBLICATION",
                "created_at": "2026-09-19T00:00:00+00:00",
                "meeting_type": "deliberation",
                "participant_count": 1,
                "representative_count": 1,
                "research_enabled": False,
            }
        ),
    )
    paths = {
        "certified": repo.docs.write_once(
            "public/final/procedurally_certified_resolution.md",
            "# 正式成果\n\n这是认证后的正文。\n",
        ),
        "handoff": repo.docs.write_once(
            "public/final/chair_execution_handoff_brief.md",
            "# 移交说明\n\n执行者应保留正文约束。\n",
        ),
        "epistemic": repo.docs.write_once(
            "public/final/think_tank_epistemic_reviews.json",
            json.dumps(
                {
                    "reviews": [
                        {
                            "reviewer_id": "R-LIB",
                            "review": {
                                "epistemic_status": "CAUTIONS",
                                "summary": "存在一项证据警示。",
                                "findings": [
                                    {
                                        "severity": "CAUTION",
                                        "category": "EVIDENCE",
                                        "finding": "应复核输入数据。",
                                        "evidence_refs": ["result §2"],
                                        "human_attention": "执行前复核。",
                                    }
                                ],
                                "reconvene_worthy": False,
                                "reconvene_reason": None,
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ),
        "execution": repo.docs.write_once(
            "public/final/think_tank_execution_reviews.json",
            json.dumps(
                {
                    "reviews": [
                        {
                            "reviewer_id": "R-LIB",
                            "review": {
                                "execution_readiness": "READY_WITH_CAUTIONS",
                                "summary": "可执行，但应先复核数据。",
                                "findings": [],
                                "human_decision_points": ["确认数据版本。"],
                                "reconvene_worthy": False,
                                "reconvene_reason": None,
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ),
        "packet": repo.docs.write_once(
            "public/final/human_review_packet.json",
            '{"status":"HANDOFF_READY"}',
        ),
    }
    return repo, paths


def _write_evidence_packet(repo: MeetingRepository) -> None:
    packet = {
        "packet_id": "RP-CITATION001",
        "claim_fingerprint": "1" * 64,
        "original_claim": "The bounded result holds under condition C.",
        "normalized_claim": "The bounded result holds under condition C.",
        "verification_question": "Does the bounded result hold under condition C?",
        "scope_terms": ["condition C"],
        "source_domain": "ACADEMIC",
        "retrieval_backend_ids": ["fixture"],
        "retrieved_at": "2026-09-20T00:00:00Z",
        "freshness_class": "STABLE",
        "maximum_cache_reuse_age_days": 180,
        "cache_expires_at": "2027-03-19T00:00:00Z",
        "supersedes_packet_ids": [],
        "search_scope": "Fixture academic retrieval.",
        "full_text_access_assessment": "One cited source was assessed.",
        "sources": [
            {
                "source_id": "doi:10.1000/bounded",
                "title": "A bounded result under condition C",
                "authors": ["A. Researcher", "B. Scholar"],
                "publication_year": 2025,
                "doi": "10.1000/bounded",
                "url": "https://doi.org/10.1000/bounded",
                "venue": "Fixture Journal",
                "source_type": "article",
                "is_primary_source": True,
                "evidence_use_class": "PRIMARY",
            },
            {
                "source_id": "https://example.test/screened-only",
                "title": "Screened but not used",
                "authors": [],
                "publication_year": 2024,
                "doi": None,
                "url": "https://example.test/screened-only",
                "venue": None,
                "source_type": "web",
                "is_primary_source": False,
                "evidence_use_class": "DISCOVERY_ONLY",
            },
        ],
        "supporting_evidence": [
            {
                "source_id": "doi:10.1000/bounded",
                "direction": "SUPPORTING",
                "evidence_summary": "The source reports the bounded result.",
                "applicability": "Condition C.",
                "limitations": "Single study.",
            }
        ],
        "contradictory_evidence": [],
        "scope_limitations": [],
        "canonical_alternatives": [],
        "counter_search_summary": "All required counter-searches ran.",
        "evidence_conflict_assessment": "No same-scope conflict was found.",
        "consensus": "INSUFFICIENT",
        "unresolved_questions": [],
        "confidence": {
            "coverage": "LOW",
            "source_quality": "MEDIUM",
            "literature_consistency": "LOW",
            "rationale": "One eligible source.",
        },
        "knowledge_status": "SOURCE_BACKED",
    }
    repo.docs.write_once(
        "public/research/evidence_packets/RP-CITATION001.json",
        json.dumps(packet, ensure_ascii=False),
    )


def test_readable_status_rejects_failed_checks():
    with pytest.raises(ValidationError, match="READABLE requires all checks to pass"):
        ChairReadabilityCertification.model_validate(
            {
                "status": "READABLE",
                "publication_source_sha256": "0" * 64,
                "checks": [
                    {
                        "name": name,
                        "status": "FAIL" if index == 0 else "PASS",
                        "explanation": "check",
                    }
                    for index, name in enumerate(
                        (
                            "MAIN_RESULT_IDENTIFIABLE",
                            "ADVISORY_APPENDICES_SEPARATE",
                            "NAVIGATION_AND_STRUCTURE",
                            "SOURCE_LABELS_CLEAR",
                            "SOURCE_TEXT_LEGIBLE",
                        )
                    )
                ],
                "required_changes": [],
                "summary": "invalid",
            }
        )


def test_pdf_renderer_is_deterministic(monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    text = "# 最终成果\n\n正文。\n\n# 附录\n\n- 独立审查意见"
    first, _ = render_publication_pdf(text, meeting_id="M-TEST")
    second, _ = render_publication_pdf(text, meeting_id="M-TEST")

    assert first == second
    assert first.startswith(b"%PDF-")
    assert b"%%EOF" in first[-2048:]


def test_final_publication_pdf_renders_inline_and_display_math(monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    source = "# 最终成果\n\n公式 $E=mc^2$。\n\n$$\\sigma=\\frac{F}{A}$$\n"
    pdf, _ = render_publication_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_pdf_repairs_json_decoded_tex_and_accepts_unbraced_font_commands(monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    damaged = (
        "# 公式修复\n\n"
        + r"两相为 \(\Omega_\alpha-\Omega_" + "\x08eta" + r"\)。"
        + r"极限为 \(L" + "\to" + r"\infty\)。"
        + r"密度为 \(" + "\nho" + r"\)。"
        + r"向量和微分为 \(\mathbf r+\mathrm d\)。"
    )
    repaired = repair_json_decoded_math_commands(damaged)
    assert r"\Omega_\beta" in repaired
    assert r"L\to\infty" in repaired
    assert r"\rho" in repaired
    assert "\x08" not in repaired and "\t" not in repaired
    pdf, _ = render_publication_pdf(damaged, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_inline_math_in_markdown_table_is_rendered_not_exposed_as_tex(monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    source = (
        "# 公式表\n\n| 指标 | 定义 |\n| --- | --- |\n"
        r"| 张力 | \(\gamma=F/L\) |" "\n"
    )
    pdf, _ = render_publication_pdf(source, meeting_id="LR-TEST")
    reader = PdfReader(io.BytesIO(pdf))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert r"\(" not in extracted and r"\gamma" not in extracted
    assert sum(len(page.images) for page in reader.pages) == 0


def test_math_inside_bold_does_not_become_literal_tex_or_symbol_placeholder(monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    source = (
        "# 数学字形\n\n"
        "**轮廓长度 ℓ_𝒪 与 \\(q_max\\) 必须定义。**\n\n"
        "普通表述中也有 ℓ_𝒪。\n"
    )
    from project_ensemble.orchestration.math_rendering import mark_unambiguous_math_atoms

    pdf, _ = render_publication_pdf(mark_unambiguous_math_atoms(source), meeting_id="LR-TEST")
    reader = PdfReader(io.BytesIO(pdf))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "[符号" not in extracted
    assert r"\(" not in extracted
    assert sum(len(page.images) for page in reader.pages) == 0


def test_final_publication_pdf_rejects_unsupported_math(monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    with pytest.raises(ValueError, match="PDF_MATH_RENDERING_FAILED"):
        render_publication_pdf("# 最终成果\n\n$\\notarealmathcommand{x}$\n", meeting_id="LR-TEST")


def test_pdf_text_does_not_insert_spaces_or_zero_width_glyphs_into_chinese():
    assert _join_markdown_paragraph_lines(["这是第一行，", "这是第二行。"] ) == (
        "这是第一行，这是第二行。"
    )
    assert _join_markdown_paragraph_lines(["English soft", "wrap remains readable."]) == (
        "English soft wrap remains readable."
    )
    assert _safe_pdf_text("甲\u200b乙\u00a0丙") == "甲乙 丙"
    rendered = _safe_pdf_text(
        "中→🧪",
        glyph_context=("cjk", frozenset({ord("中")}), "symbols", frozenset({ord("→")})),
    )
    assert 'font name="symbols"' in rendered
    assert "→" in rendered
    assert "[符号 U+1F9EA]" in rendered


def test_inline_markup_keeps_emphasis_outside_font_fallback_spans():
    value = (
        "**会议 M-918092A9 · 程序认证文本 C1 · Chair 程序终检 "
        "CERTIFIED（9/9 项通过，无修正）· Human 处置决定待定**"
    )
    primary = frozenset(ord(character) for character in value if ord(character) > 127)
    fallback = frozenset(ord(character) for character in value if ord(character) < 128)
    fallback_name = "ENSEMBLE-TEST-SYMBOL"
    if fallback_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(fallback_name, str(_font_for_test())))
    pdfmetrics.registerFontFamily(
        fallback_name,
        normal=fallback_name,
        bold=fallback_name,
        italic=fallback_name,
        boldItalic=fallback_name,
    )
    rendered = _inline_markup(
        value,
        glyph_context=("cjk", primary, fallback_name, fallback),
    )

    assert rendered.startswith("<b>")
    assert rendered.endswith("</b>")
    assert f'<font name="{fallback_name}"><b></font>' not in rendered
    assert f'<font name="{fallback_name}"></b></font>' not in rendered
    # Construction itself asks ReportLab to parse the rich text and therefore
    # reproduces the production failure without needing to write a PDF file.
    Paragraph(rendered, getSampleStyleSheet()["BodyText"])


def test_numeric_citations_use_academic_lists_and_ranges():
    assert _format_numeric_citations([6, 2, 3, 4, 2, 9]) == "[2–4, 6, 9]"


def test_reference_lines_are_separate_hanging_indent_paragraphs():
    story = _markdown_story(
        "[1] First author. First paper. 2025. https://doi.org/10.1/first.\n"
        "[2] Second author. Second paper. 2026. https://doi.org/10.1/second.\n",
        _pdf_styles("Helvetica"),
    )
    assert len(story) == 2
    assert all(item.style.name == "EnsembleReference" for item in story)
    assert all(item.style.firstLineIndent < 0 for item in story)


def test_publication_freezes_body_appendices_chair_check_and_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    repo, paths = _repo_with_publication_sources(tmp_path)
    engine = _Engine()
    runner = FinalPublicationRunner(
        repo=repo,
        engine=engine,
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )

    result = runner.run(
        certified_resolution_path=paths["certified"],
        chair_handoff_path=paths["handoff"],
        epistemic_reviews_path=paths["epistemic"],
        execution_reviews_path=paths["execution"],
        human_review_packet_path=paths["packet"],
    )

    assert result.readability_status == "READABLE"
    report = (repo.root / result.report_markdown_path).read_text(encoding="utf-8")
    assert "# 正文" in report
    assert "# 附录 A：独立知识完整性审查" in report
    assert "# 附录 B：独立执行移交审查" in report
    assert report.index("这是认证后的正文") < report.index("存在一项证据警示")
    pdf = (repo.root / result.report_pdf_path).read_bytes()
    manifest = json.loads(
        (repo.root / result.publication_manifest_path).read_text(encoding="utf-8")
    )
    assert manifest["report_pdf_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert (repo.root / "FINAL_REPORT.pdf").is_symlink()
    assert (repo.root / "FINAL_REPORT.pdf").read_bytes() == pdf
    assert (repo.root / "FINAL_REPORT.md").is_symlink()

    # Fully frozen publication resumes without another Chair call or PDF rewrite.
    second = runner.run(
        certified_resolution_path=paths["certified"],
        chair_handoff_path=paths["handoff"],
        epistemic_reviews_path=paths["epistemic"],
        execution_reviews_path=paths["execution"],
        human_review_packet_path=paths["packet"],
    )
    assert second.report_pdf_path == result.report_pdf_path
    assert engine.calls == 1


def test_publication_includes_validated_numeric_citations_and_references(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    repo, paths = _repo_with_publication_sources(tmp_path)
    _write_evidence_packet(repo)
    runner = FinalPublicationRunner(
        repo=repo,
        engine=_Engine(),
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )

    result = runner.run(
        certified_resolution_path=paths["certified"],
        chair_handoff_path=paths["handoff"],
        epistemic_reviews_path=paths["epistemic"],
        execution_reviews_path=paths["execution"],
        human_review_packet_path=paths["packet"],
    )

    report = (repo.root / result.report_markdown_path).read_text(encoding="utf-8")
    assert "# 附录 C：会议文献证据索引" in report
    assert "The bounded result holds under condition C." in report
    assert "支持 [1]" in report
    assert "[1] A. Researcher, B. Scholar." in report
    assert "https://doi.org/10.1000/bounded" in report
    assert "Screened but not used" not in report
    manifest = json.loads(
        (repo.root / result.publication_manifest_path).read_text(encoding="utf-8")
    )
    assert manifest["citation_policy"]["cited_evidence_packet_count"] == 1
    assert manifest["citation_policy"]["reference_count"] == 1
    assert manifest["citation_policy"]["style"] == "NUMBERED_HANGING_INDENT_V2"
    assert manifest["citation_policy"]["reference_trace"][0]["packet_ids"] == [
        "RP-CITATION001"
    ]
    references = report.split("# 参考文献 / References", 1)[1]
    assert "evidence packet：" not in references
    assert "会议归档：" not in references


def test_failed_readability_check_does_not_publish_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    repo, paths = _repo_with_publication_sources(tmp_path)
    runner = FinalPublicationRunner(
        repo=repo,
        engine=_Engine(status="REVISION_REQUIRED"),
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )

    result = runner.run(
        certified_resolution_path=paths["certified"],
        chair_handoff_path=paths["handoff"],
        epistemic_reviews_path=paths["epistemic"],
        execution_reviews_path=paths["execution"],
        human_review_packet_path=paths["packet"],
    )

    assert result.readability_status == "REVISION_REQUIRED"
    assert result.report_pdf_path is None
    assert not (repo.root / "public/final/final_report.pdf").exists()


def test_v1_pdf_is_preserved_and_root_link_moves_to_v2(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSEMBLE_CJK_FONT", str(_font_for_test()))
    repo, paths = _repo_with_publication_sources(tmp_path)
    runner = FinalPublicationRunner(
        repo=repo,
        engine=_Engine(),
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )
    first = runner.run(
        certified_resolution_path=paths["certified"],
        chair_handoff_path=paths["handoff"],
        epistemic_reviews_path=paths["epistemic"],
        execution_reviews_path=paths["execution"],
        human_review_packet_path=paths["packet"],
    )
    old_pdf = repo.root / first.report_pdf_path
    old_bytes = old_pdf.read_bytes()
    old_manifest_path = repo.root / "public/final/final_publication_manifest.json"
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    old_manifest["template_version"] = "FINAL_PUBLICATION_V1"
    old_manifest_path.write_text(json.dumps(old_manifest), encoding="utf-8")

    upgraded = runner.run(
        certified_resolution_path=paths["certified"],
        chair_handoff_path=paths["handoff"],
        epistemic_reviews_path=paths["epistemic"],
        execution_reviews_path=paths["execution"],
        human_review_packet_path=paths["packet"],
    )

    assert upgraded.report_pdf_path == "public/final/final_report_v2.pdf"
    assert upgraded.report_markdown_path == "public/final/final_report_v2.md"
    assert old_pdf.read_bytes() == old_bytes
    assert (repo.root / "FINAL_REPORT.pdf").resolve() == (
        repo.root / upgraded.report_pdf_path
    ).resolve()
    assert (repo.root / upgraded.publication_manifest_path).exists()
    assert (repo.root / "FINAL_REPORT.md").resolve() == (
        repo.root / upgraded.report_markdown_path
    ).resolve()
