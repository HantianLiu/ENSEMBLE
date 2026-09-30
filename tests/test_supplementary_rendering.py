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
