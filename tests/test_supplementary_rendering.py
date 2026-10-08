import hashlib
import json

import pytest

from project_ensemble.orchestration import supplementary_rendering as rendering
from project_ensemble.storage.meeting import MeetingRepository


def archived_report(tmp_path):
    root = tmp_path / "LR-COMPLETE"
    source = root / "public/final/literature_review_report.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 完成的报告\n\n## 正文\n\n可读的结论 [1]。\n\n## 参考文献\n\n[1] Example.\n", encoding="utf-8")
    relative = str(source.relative_to(root))
    archive = root / "public/archive_manifest.json"
    archive.write_text(json.dumps({
        "status": "ARCHIVED", "retained_document_path": relative,
        "retained_file_sha256": {relative: hashlib.sha256(source.read_bytes()).hexdigest()},
    }), encoding="utf-8")
    return MeetingRepository(root), source


def test_completed_archived_meeting_can_add_html_without_changing_source(tmp_path):
    repo, source = archived_report(tmp_path)
    original = source.read_bytes()
    first = rendering.render_additional_formats(repo, formats=("html",))
    second = rendering.render_additional_formats(repo, formats=("html",))
    assert first == second
    assert first["html"].parent == repo.root
    assert first["html"].read_text(encoding="utf-8").startswith("<!doctype html>")
    assert (repo.root / "SUPPLEMENTARY_REPORT.html").resolve() == first["html"].resolve()
    assert source.read_bytes() == original
    provenance = json.loads(first["html"].resolve().with_name("report.html.provenance.json").read_text())
    assert provenance["source_sha256"] == hashlib.sha256(original).hexdigest()


def test_supplementary_rendering_accepts_an_alias_path_to_the_same_meeting(tmp_path):
    repo, _ = archived_report(tmp_path)
    alias = tmp_path / "meeting-alias"
    alias.symlink_to(repo.root, target_is_directory=True)
    output = rendering.render_additional_formats(MeetingRepository(alias), formats=("html",))
    assert output["html"].parent == repo.root
    assert output["html"].is_file()


def test_pdf_failure_does_not_remove_supplementary_html(tmp_path, monkeypatch):
    repo, _ = archived_report(tmp_path)
    def broken_pdf(*_args, **_kwargs):
        raise ValueError("PDF_MATH_RENDERING_FAILED")
    monkeypatch.setattr(rendering, "render_academic_review_pdf", broken_pdf)
    with pytest.raises(ValueError, match="PDF_MATH_RENDERING_FAILED"):
        rendering.render_additional_formats(repo, formats=("html", "pdf"))
    assert (repo.root / "SUPPLEMENTARY_REPORT.html").is_file()
    assert not (repo.root / "SUPPLEMENTARY_REPORT.pdf").exists()


def test_archived_source_hash_mismatch_blocks_supplementary_rendering(tmp_path):
    repo, source = archived_report(tmp_path)
    source.write_text("# 被篡改的文稿", encoding="utf-8")
    with pytest.raises(ValueError, match="归档哈希不一致"):
        rendering.render_additional_formats(repo, formats=("html",))


def test_supplementary_html_recovers_original_pdf_url_from_retained_evidence(tmp_path):
    repo, _ = archived_report(tmp_path)
    (repo.root / "public/final").mkdir(parents=True, exist_ok=True)
    (repo.root / "public/final/literature_review_publication_manifest.json").write_text(
        json.dumps({"citation_trace_path": "public/literature_report/citation_trace.json"}),
        encoding="utf-8",
    )
    trace = {"references": [{
        "reference_number": 1,
        "source_id": "S-1",
        "doi": "10.1234/example",
        "url": "https://publisher.example/article",
    }]}
    trace_path = repo.root / "public/literature_report/citation_trace.json"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    packets = repo.root / "public/research/evidence_packets"
    packets.mkdir(parents=True)
    (packets / "C1.json").write_text(json.dumps({"sources": [{
        "source_id": "S-1",
        "original_document_url": "https://publisher.example/article.pdf",
    }]}), encoding="utf-8")

    links = rendering._citation_links(repo.root)
    assert links == {
        "1": {
            "source": "https://doi.org/10.1234/example",
            "pdf": "https://publisher.example/article.pdf",
        }
    }


def test_old_html_derivative_is_preserved_and_shortcut_points_to_v2(tmp_path):
    repo, source = archived_report(tmp_path)
    old = repo.root / "public/supplementary_rendering" / hashlib.sha256(source.read_bytes()).hexdigest()[:16] / "report.html"
    old.parent.mkdir(parents=True)
    old.write_text("<!doctype html><p>old formula $x$</p>", encoding="utf-8")
    (repo.root / "SUPPLEMENTARY_REPORT.html").symlink_to(old.relative_to(repo.root))
    output = rendering.render_additional_formats(repo, formats=("html",))
    assert old.read_text(encoding="utf-8") == "<!doctype html><p>old formula $x$</p>"
    assert output["html"].resolve() != old.resolve()
    from project_ensemble.orchestration.academic_html import HTML_RENDERING_PROFILE
    assert HTML_RENDERING_PROFILE in str(output["html"].resolve())


def test_renderer_change_generates_a_new_derivative_without_overwriting_old(tmp_path, monkeypatch):
    repo, source = archived_report(tmp_path)
    source_bytes = source.read_bytes()
    monkeypatch.setattr(rendering, "_renderer_fingerprint", lambda _: "a" * 64)
    first = rendering.render_additional_formats(repo, formats=("html",))["html"].resolve()
    first_bytes, first_mtime = first.read_bytes(), first.stat().st_mtime_ns
    monkeypatch.setattr(rendering, "_renderer_fingerprint", lambda _: "b" * 64)
    second = rendering.render_additional_formats(repo, formats=("html",))["html"].resolve()
    assert second != first
    assert first.read_bytes() == first_bytes
    assert first.stat().st_mtime_ns == first_mtime
    assert source.read_bytes() == source_bytes
    assert rendering.render_additional_formats(repo, formats=("html",))["html"].resolve() == second
    assert json.loads(second.with_name("report.html.provenance.json").read_text())["renderer_sha256"] == "b" * 64


def test_changed_citation_links_or_mathjax_settings_invalidate_html_cache(tmp_path, monkeypatch):
    repo, _ = archived_report(tmp_path)
    first = rendering.render_additional_formats(repo, formats=("html",))["html"].resolve()
    monkeypatch.setattr(rendering, "_citation_links", lambda _: {"1": {"source": "https://example.org/new"}})
    second = rendering.render_additional_formats(repo, formats=("html",))["html"].resolve()
    monkeypatch.setenv("ENSEMBLE_MATHJAX_URL", "./mathjax.js")
    third = rendering.render_additional_formats(repo, formats=("html",))["html"].resolve()
    assert len({first, second, third}) == 3
    assert 'src="./mathjax.js"' in third.read_text()


def test_tampered_cached_derivative_is_not_overwritten(tmp_path):
    repo, _ = archived_report(tmp_path)
    output = rendering.render_additional_formats(repo, formats=("html",))["html"].resolve()
    output.write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="与来源记录不符"):
        rendering.render_additional_formats(repo, formats=("html",))
    assert output.read_text() == "modified"
