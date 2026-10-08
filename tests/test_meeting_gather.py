import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

from project_ensemble import cli
from project_ensemble.orchestration import supplementary_rendering
from project_ensemble.orchestration.meeting_gather import gather_collection, gather_meeting
from project_ensemble.startup import TerminalWizard


def _meeting(root, meeting_id="LR-1234ABCD", title="测试会议"):
    source = root / "public/final/literature_review_report.md"
    source.parent.mkdir(parents=True)
    source.write_text(f"# {title}\n\n已完成的正文。\n", encoding="utf-8")
    (root / "public/meeting_manifest.json").write_text(json.dumps({
        "meeting_id": meeting_id, "title": title, "deliverable_type": "literature_review",
    }), encoding="utf-8")
    relative = source.relative_to(root).as_posix()
    (root / "public/archive_manifest.json").write_text(json.dumps({
        "status": "ARCHIVED", "retained_document_path": relative,
        "retained_file_sha256": {relative: hashlib.sha256(source.read_bytes()).hexdigest()},
    }), encoding="utf-8")
    return source


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_text_only_gather_collects_latest_complete_source_without_literature(tmp_path):
    root = tmp_path / "renamed-task"
    source = _meeting(root)
    revision = _write(root / "public/corrigenda/revision_1.md", b"# New complete revision")
    _write(revision.with_suffix(".pdf"), b"%PDF-corrected")
    revision.with_suffix(".json").write_text("{}", encoding="utf-8")
    original = source.read_bytes()
    result = gather_meeting(root, tmp_path / "exports", formats=("md",), rerender=False, include_literature=False)
    assert result["status"] == "COMPLETE"
    assert Path(result["files"][0]["path"]).read_bytes() == revision.read_bytes()
    assert not list(Path(result["folder"]).glob("*.zip"))
    assert source.read_bytes() == original


def test_pdf_bundle_collects_all_local_sources_and_zip_only_documents_with_deduplication(tmp_path):
    root = tmp_path / "renamed-task"
    _meeting(root, title="有意义的会议名称")
    docs = root / "public/research/literature_bundle/documents"
    _write(docs / "paper.pdf", b"%PDF-paper")
    _write(root / "public/human_references/files/duplicate.PDF", b"%PDF-paper")
    _write(root / "public/human_references/files/human.pdf", b"%PDF-human")
    _write(root / "audit_private/private.pdf", b"%PDF-private")
    _write(root / "public/final/report.pdf", b"%PDF-final")
    _write(docs / "web.html", b"<html>web</html>")
    with zipfile.ZipFile(root / "public/research/literature_bundle.zip", "w") as bundle:
        bundle.writestr("documents/duplicate.pdf", b"%PDF-paper")
        bundle.writestr("documents/zip-only.pdf", b"%PDF-zip-only")
    result = gather_meeting(root, tmp_path / "exports", formats=("md",), rerender=False, include_literature=True)
    assert result["status"] == "COMPLETE"
    assert result["pdf_count"] == 3
    package = Path(result["literature_zip"])
    assert "有意义的会议名称" in package.name
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        pdfs = [archive.read(name) for name in archive.namelist() if name.lower().endswith(".pdf")]
        assert set(pdfs) == {b"%PDF-paper", b"%PDF-human", b"%PDF-zip-only"}
        assert set(archive.namelist()) == {"paper.pdf", "human.pdf", "zip-only.pdf"}
        assert all("/" not in name for name in archive.namelist())


def test_pdf_render_failure_preserves_html_and_markdown_and_uses_actual_meeting_id(tmp_path, monkeypatch):
    root = tmp_path / "renamed-folder"
    source = _meeting(root)
    original = source.read_bytes()
    def broken_pdf(*args, **kwargs):
        raise ValueError("PDF renderer unavailable")
    monkeypatch.setattr(supplementary_rendering, "render_academic_review_pdf", broken_pdf)
    result = gather_meeting(root, tmp_path / "exports", formats=("md", "html", "pdf"), rerender=True, include_literature=False)
    assert result["status"] == "PARTIAL"
    assert {item["format"] for item in result["files"]} == {"md", "html"}
    html = Path(next(item["path"] for item in result["files"] if item["format"] == "html")).read_text(encoding="utf-8")
    assert 'data-meeting-id="LR-1234ABCD"' in html
    assert any("PDF renderer unavailable" in issue for issue in result["issues"])
    assert source.read_bytes() == original


def test_existing_old_html_is_not_mixed_with_latest_corrigendum(tmp_path):
    root = tmp_path / "meeting"
    source = _meeting(root)
    (root / "LITERATURE_REVIEW.md").symlink_to(source.relative_to(root))
    _write(root / "LITERATURE_REVIEW.html", b"<html>old edition</html>")
    revision = _write(root / "public/corrigenda/revision_2.md", b"# Latest corrected report")
    _write(revision.with_suffix(".pdf"), b"%PDF-new")
    revision.with_suffix(".json").write_text("{}", encoding="utf-8")
    result = gather_meeting(root, tmp_path / "exports", formats=("md", "html"), rerender=False, include_literature=False)
    assert result["status"] == "PARTIAL"
    assert [item["format"] for item in result["files"]] == ["md"]


def test_incomplete_meeting_can_export_literature_without_promoting_module_draft(tmp_path):
    root = tmp_path / "meeting"
    (root / "public").mkdir(parents=True)
    (root / "public/meeting_manifest.json").write_text(json.dumps({
        "meeting_id": "LR-11223344", "title": "未完成会议",
    }), encoding="utf-8")
    _write(root / "public/literature_report/modules/RM-01/draft.md", b"# Only one module")
    _write(root / "public/research/literature_bundle/documents/paper.pdf", b"%PDF-paper")
    result = gather_meeting(root, tmp_path / "exports", formats=("md",), rerender=False, include_literature=True)
    assert result["status"] == "PARTIAL"
    assert result["files"] == []
    assert result["pdf_count"] == 1
    assert Path(result["literature_zip"]).is_file()


def test_gather_rejects_modified_frozen_complete_text(tmp_path):
    root = tmp_path / "meeting"
    source = _meeting(root)
    source.write_text("# Changed text", encoding="utf-8")
    result = gather_meeting(root, tmp_path / "exports", formats=("md",), rerender=False, include_literature=False)
    assert result["status"] == "SKIPPED"
    assert result["files"] == []
    assert any("哈希" in issue for issue in result["issues"])


def test_gather_command_selects_only_immediate_children_and_supports_ranges(tmp_path, monkeypatch):
    parent = tmp_path / "tasks"
    for number in range(3):
        _meeting(parent / f"task-{number}", meeting_id=f"LR-1234ABC{number}", title=f"会议{number}")
    _meeting(parent / "nested" / "deeper-meeting", meeting_id="LR-9999ABCD")
    monkeypatch.chdir(parent)
    answers = iter(["1，2-3", "1"])
    output = io.StringIO()
    wizard = TerminalWizard(input_fn=lambda _: next(answers), output=output, color=False)
    monkeypatch.setattr(cli, "TerminalWizard", lambda: wizard)
    monkeypatch.setattr(cli, "enable_utf8_terminal_erase", lambda: None)
    monkeypatch.setattr(sys, "argv", ["ensemble", "gather", "--output", str(tmp_path / "exports")])
    assert cli._main_impl() == 0
    destination = tmp_path / "exports/会议资料.zip"
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {f"会议{number}/会议{number}.md" for number in range(3)}
    assert "deeper-meeting" not in output.getvalue()


def test_gather_cancel_does_not_create_output(tmp_path, monkeypatch):
    _meeting(tmp_path / "meeting")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "TerminalWizard", lambda: TerminalWizard(input_fn=lambda _: "", output=io.StringIO(), color=False))
    assert cli.cmd_gather(SimpleNamespace(output=None)) == 0
    assert not list(tmp_path.glob("*.zip"))


def test_outer_zip_contains_only_title_folders_final_text_and_flat_pdf_zip(tmp_path):
    root = tmp_path / "meeting"
    source = _meeting(root, title="会议 / 测试")
    _write(root / "public/research/one/paper.pdf", b"%PDF-one")
    _write(root / "public/research/two/paper.pdf", b"%PDF-two")
    _write(root / "public/human_references/paper.PDF", b"%PDF-two")
    source_bytes = source.read_bytes()
    result = gather_collection([root], tmp_path / "exports", formats=("md", "html"), include_literature=True)
    assert result["meetings"][0]["status"] == "COMPLETE"
    with zipfile.ZipFile(result["path"]) as outer:
        assert set(outer.namelist()) == {
            "会议 - 测试/会议 - 测试.md", "会议 - 测试/会议 - 测试.html",
            "会议 - 测试/会议 - 测试（文献PDF）.zip",
        }
        inner_bytes = outer.read(result["meetings"][0]["literature_zip"])
        with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
            assert set(inner.namelist()) == {"paper.pdf", "paper（2）.pdf"}
            assert {inner.read(name) for name in inner.namelist()} == {b"%PDF-one", b"%PDF-two"}
    assert list((tmp_path / "exports").iterdir()) == [Path(result["path"])]
    assert source.read_bytes() == source_bytes


def test_duplicate_titles_and_repeated_export_never_overwrite(tmp_path):
    roots = [tmp_path / "one", tmp_path / "two"]
    for index, root in enumerate(roots):
        _meeting(root, meeting_id=f"LR-1234ABC{index}", title="同名会议")
    first = gather_collection(roots, tmp_path / "export.zip", formats=("md",), include_literature=False)
    first_bytes = Path(first["path"]).read_bytes()
    second = gather_collection(roots, tmp_path / "export.zip", formats=("md",), include_literature=False)
    assert Path(second["path"]).name == "export（2）.zip"
    assert Path(first["path"]).read_bytes() == first_bytes
    with zipfile.ZipFile(first["path"]) as outer:
        assert set(outer.namelist()) == {"同名会议/同名会议.md", "同名会议（2）/同名会议.md"}


def test_current_meeting_is_discovered_and_only_two_menus_are_needed(tmp_path, monkeypatch):
    _meeting(tmp_path)
    monkeypatch.chdir(tmp_path)
    answers = iter(["1", "1，4"])
    wizard = TerminalWizard(input_fn=lambda _: next(answers), output=io.StringIO(), color=False)
    monkeypatch.setattr(cli, "TerminalWizard", lambda: wizard)
    assert cli.cmd_gather(SimpleNamespace(output=None)) == 0
    with zipfile.ZipFile(tmp_path / "会议资料.zip") as archive:
        assert set(archive.namelist()) == {"测试会议/测试会议.md", "测试会议/测试会议（文献PDF）.zip"}


def test_cancel_content_menu_creates_no_archive(tmp_path, monkeypatch):
    _meeting(tmp_path / "meeting")
    monkeypatch.chdir(tmp_path)
    answers = iter(["1", ""])
    monkeypatch.setattr(cli, "TerminalWizard", lambda: TerminalWizard(
        input_fn=lambda _: next(answers), output=io.StringIO(), color=False,
    ))
    assert cli.cmd_gather(SimpleNamespace(output=None)) == 0
    assert not list(tmp_path.glob("*.zip"))


def test_literature_only_does_not_require_a_final_report_and_unsafe_members_are_skipped(tmp_path):
    root = tmp_path / "meeting"
    source = _meeting(root)
    source.unlink()
    base = root / "public/research"
    base.mkdir(parents=True)
    with zipfile.ZipFile(base / "literature_bundle.zip", "w") as archive:
        archive.writestr("../escape.pdf", b"%PDF-bad")
        archive.writestr("deep/tree/original.pdf", b"%PDF-good")
    result = gather_collection([root], tmp_path / "exports", formats=(), include_literature=True)
    meeting = result["meetings"][0]
    assert meeting["status"] == "PARTIAL"
    assert meeting["files"] == []
    assert meeting["pdf_count"] == 1
    with zipfile.ZipFile(result["path"]) as outer:
        with zipfile.ZipFile(io.BytesIO(outer.read(meeting["literature_zip"]))) as inner:
            assert inner.namelist() == ["original.pdf"]


def test_one_broken_meeting_does_not_discard_other_exports(tmp_path):
    good, bad = tmp_path / "good", tmp_path / "bad"
    _meeting(good, title="成功会议")
    source = _meeting(bad, title="异常会议")
    source.write_text("modified", encoding="utf-8")
    result = gather_collection([good, bad], tmp_path / "exports", formats=("md",), include_literature=False)
    assert [item["status"] for item in result["meetings"]] == ["COMPLETE", "SKIPPED"]
    with zipfile.ZipFile(result["path"]) as archive:
        assert archive.namelist() == ["成功会议/成功会议.md"]
