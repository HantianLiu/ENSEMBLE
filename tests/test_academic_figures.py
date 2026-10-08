"""Programmatic figures preserve scientific review, provenance and text fallback."""
import copy
import json
import re
from types import SimpleNamespace

import pytest

from project_ensemble.orchestration import academic_figures as figures
from project_ensemble.orchestration.academic_html import render_academic_review_html
from project_ensemble.orchestration.academic_pdf import render_academic_review_pdf
from project_ensemble.orchestration.literature_report_execution import ModuleDraft, LiteratureReportExecutionRunner
from project_ensemble.orchestration.literature_writing_v071 import WriterChapter
from project_ensemble.orchestration.literature_fast import FastLocalScienceRepair, _apply_local_science_items
from project_ensemble.storage.documents import ImmutableDocumentStore


def specification(kind="line"):
    spec = dict(id="comparison", kind=kind, title="条件比较", caption="同口径的数值比较。",
                alt_text="随控制参数增大，响应增大。", evidence_basis="extracted",
                data_note="来源表 1；同一人群、同一时间，未经插值。",
                source_citation_ids=["C1-1"], x_label="参数 (nm)", y_label="响应 (mN/m)")
    if kind in {"line", "scatter", "bar"}:
        spec["series"] = [dict(label="方案 A", y=[1, 3], y_error=[.1, .2])]
        if kind == "bar":
            spec["categories"] = ["条件一", "条件二"]
        else:
            spec["series"][0]["x"] = [1, 2]
    elif kind == "heatmap":
        spec.update(row_labels=["A", "B"], column_labels=["X", "Y"], matrix=[[1, 2], [3, 4]], value_label="响应 (mN/m)")
    else:
        spec.update(evidence_basis="schematic", nodes=[dict(id="a", label="输入"), dict(id="b", label="测量")],
                    edges=[dict(source="a", target="b", label="操作先后")])
    return spec


@pytest.mark.parametrize("kind", ["line", "scatter", "bar", "heatmap", "flowchart"])
def test_all_first_version_graphs_render_without_gui_or_external_code(kind):
    png, svg = figures.render_figure(figures.FigureSpec.model_validate(specification(kind)))
    assert png.startswith(b"\x89PNG") and b"<svg" in svg


@pytest.mark.parametrize("change", [
    {"series": [{"label": "A", "x": [1], "y": [2, 3]}]},
    {"series": [{"label": "A", "x": [2, 1], "y": [2, 3]}]},
    {"series": [{"label": "A", "x": [1, 2], "y": [float("nan"), 3]}]},
    {"source_citation_ids": ["C1-999"]},
    {"code": "raise RuntimeError('must never execute')"},
    {"path": "/tmp/image.png"},
])
def test_bad_picture_does_not_discard_other_figure_or_prose(change):
    bad = {**specification(), **change}
    good = {**specification("flowchart"), "id": "workflow"}
    body = "有效正文。\n\n[[FIGURE:comparison]]\n\n[[FIGURE:workflow]]\n"
    result, accepted, diagnostics = figures.prepare_figures(body, [bad, good], {"sources": [{"citation_id": "C1-1"}]})
    assert len(accepted) == 1 and accepted[0]["id"] == "workflow"
    assert "有效正文" in result and "FIGURE:workflow" in result and "FIGURE:comparison" not in result
    assert diagnostics[0]["status"] == "OMITTED" and diagnostics[0]["science_review_required"]


def test_sources_canonicalize_chinese_brackets_and_padding():
    spec = {**specification(), "source_citation_ids": ["【C1-1】"], "caption": "结论［C1-1］。"}
    body, accepted, diagnostics = figures.prepare_figures("[[FIGURE:comparison]]", [spec],
                                                         {"sources": [{"citation_id": "C00001-00001"}]})
    assert not diagnostics
    prose = figures.figure_citation_prose(accepted)
    assert "[C00001-00001]" in prose and "C1-1" not in prose
    assert "[C00001-00001]" in figures.expand_figure_markers(body, accepted)


def test_legacy_draft_and_local_patch_serialized_shape_are_unchanged():
    draft = ModuleDraft(title="标题", body_markdown="正文", short_summary="摘要")
    assert draft.figures == [] and "figures" not in draft.model_dump(mode="json")
    assert "figure_diagnostics" not in draft.model_dump(mode="json")
    assert "figure_edits" not in FastLocalScienceRepair().model_dump(mode="json")


def test_figure_only_citations_reach_evidence_review(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"sources": [{"citation_id": "C1-1", "packet_ids": ["RP-ONE"]}]}))
    runner = object.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(meeting_id="LR-TEST", events=SimpleNamespace(append=lambda *_a, **_kw: None))
    draft = ModuleDraft(title="研究", body_markdown="[[FIGURE:comparison]]", short_summary="摘要", figures=[specification()])
    validated = runner._validate_chapter_source_citations(SimpleNamespace(module_id="RM-01"), draft, catalog)
    assert validated.cited_packet_ids == ["RP-ONE"]
    assert validated.figures and not validated.figure_diagnostics


def test_freeze_reuses_existing_picture_and_verifies_provenance(tmp_path, monkeypatch):
    repo = SimpleNamespace(root=tmp_path, docs=ImmutableDocumentStore(tmp_path))
    spec = figures.FigureSpec.model_validate(specification())
    assets, records = figures.freeze_figure_assets(repo, [spec.model_dump()])
    image_path = figures.figure_image_path(spec)
    original = (tmp_path / "public/final" / image_path).read_bytes()
    metadata = json.loads((tmp_path / "public/final" / image_path).with_suffix(".json").read_text())
    assert metadata["spec"]["data_note"] == spec.data_note
    monkeypatch.setattr(figures, "render_figure", lambda *_a: pytest.fail("frozen figure rendered again"))
    reused, statuses = figures.freeze_figure_assets(repo, [spec.model_dump()])
    assert reused == assets and statuses == records
    assert original == reused[image_path]


def test_graphics_failure_keeps_spec_for_text_fallback_without_retry(tmp_path, monkeypatch):
    repo = SimpleNamespace(root=tmp_path, docs=ImmutableDocumentStore(tmp_path))
    def fail(_spec):
        raise RuntimeError("render unavailable")
    monkeypatch.setattr(figures, "render_figure", fail)
    assets, records = figures.freeze_figure_assets(repo, [specification()])
    assert not assets and records[0]["status"] == "TEXT_FALLBACK"
    monkeypatch.setattr(figures, "render_figure", lambda *_a: pytest.fail("failed graphics replayed on resume"))
    assert figures.freeze_figure_assets(repo, [specification()]) == (assets, records)
    fallback = figures.figure_text_fallback(figures.FigureSpec.model_validate(records[0]["spec"]))
    assert "1.0" in fallback and "3.0" in fallback and "mN/m" in fallback


def test_html_is_single_file_pdf_contains_the_same_png():
    spec = figures.FigureSpec.model_validate(specification())
    png, _ = figures.render_figure(spec)
    path = figures.figure_image_path(spec)
    body = figures.expand_figure_markers("[[FIGURE:comparison]]", [spec.model_dump()], first_number=3)
    markdown = "# 报告\n\n## 方法\n\n" + body.replace("[C1-1]", "[1]") + "\n\n## 参考文献\n\n[1] 文献。\n"
    rendered = render_academic_review_html(markdown, meeting_id="LR-TEST", figure_assets={path: png})
    assert 'src="data:image/png;base64,' in rendered and 'class="ensemble-figure"' in rendered
    assert 'data-references="1"' in rendered and "图 3." in rendered
    pdf, _ = render_academic_review_pdf(markdown, meeting_id="LR-TEST", figure_assets={path: png})
    assert pdf.startswith(b"%PDF") and b"/Subtype /Image" in pdf
    no_asset = render_academic_review_html(markdown, meeting_id="LR-TEST")
    assert "figure-fallback" in no_asset


def test_generated_figure_alt_math_does_not_inject_html_inside_attribute():
    spec = figures.FigureSpec.model_validate({**specification(), "alt_text": r'趋势由 \(x\) 控制，"<标签>"。'})
    markdown = figures.expand_figure_markers("[[FIGURE:comparison]]", [spec.model_dump()])
    png, _ = figures.render_figure(spec)
    rendered = render_academic_review_html(markdown, meeting_id="LR-TEST", figure_assets={figures.figure_image_path(spec): png})
    tag = re.search(r'<img class="ensemble-figure"[^>]+>', rendered)[0]
    assert "ENSEMBLEMATH" not in tag and "<span" not in tag
    assert r"\(x\)" in tag and "&lt;标签&gt;" in tag and "&quot;" in tag


def test_local_figure_science_revision_applies_and_bad_item_is_independent():
    original = figures.FigureSpec.model_validate(specification()).model_dump(mode="json")
    revised = copy.deepcopy(original)
    revised["caption"] = "修正后的图注，保留适用边界。"
    chapter = WriterChapter(draft=ModuleDraft(title="研究", short_summary="摘要", body_markdown="[[FIGURE:comparison]]", figures=[original]))
    proposal = FastLocalScienceRepair(figure_edits=[
        dict(figure_id="missing", expected_figure={}, replacement=revised, objection_numbers=[1]),
        dict(figure_id="comparison", expected_figure=original, replacement=revised, objection_numbers=[2]),
    ])
    result, audit = _apply_local_science_items(chapter, proposal, {}, 2, lambda new, *_a: new, lambda *_a: None)
    assert result.draft.figures[0]["caption"] == revised["caption"]
    assert chapter.draft.figures[0]["caption"] == original["caption"]
    assert audit["objections_without_applied_edit"] == [1] and audit["science_review_required"]


def test_skill_is_packaged_and_teaches_evidence_not_image_upload():
    skill = figures.writer_figure_skill()
    assert "draft.figures" in skill and "[[FIGURE:" in skill
    assert "不接受本地图片" in skill and "不能把关键判断仅放在图中" in skill


def test_figure_only_revision_still_gets_local_scientific_review(tmp_path, monkeypatch):
    from project_ensemble.orchestration import literature_writing_v071 as writing
    from project_ensemble.orchestration.literature_report import OutlineModule
    from project_ensemble.runtime.progress import NullProgressReporter
    repo = SimpleNamespace(root=tmp_path, docs=ImmutableDocumentStore(tmp_path))
    runner = SimpleNamespace(repo=repo, engine=SimpleNamespace(status=SimpleNamespace(phase=None),
                                                             progress=NullProgressReporter()))
    module = OutlineModule(module_id="RM-01", title="研究", research_questions=["问题？"],
                           required_evidence=["证据"], source_submission_refs=["S-1"])
    original = WriterChapter(draft=ModuleDraft(title="研究", body_markdown="[[FIGURE:comparison]]",
                                               short_summary="摘要", figures=[specification()]))
    revised = original.model_copy(deep=True)
    revised.draft.figures[0]["caption"] = "修正后的图注。"
    dossier = repo.docs.write_once("dossier.json", json.dumps({"module_id": "RM-01", "packets": []}))
    monkeypatch.setattr(writing, "_literature_step", lambda *_a, **_kw: None)
    monkeypatch.setattr(writing, "_science_librarians", lambda *_a, **_kw: [{"representative_id": "R-1"}])
    calls = []
    def review(*args):
        user = args[-1]
        assert user["changed_paragraphs"] == []
        assert user["final_figures"][0]["caption"] == "修正后的图注。"
        calls.append(user)
        return writing.LocalCheck(status="PASS")
    monkeypatch.setattr(writing, "_frozen_or_call", review)
    path = writing._local_science_check(runner, module, original, revised, dossier)
    assert calls and json.loads(path.read_text())["status"] == "PASS"
