import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from project_ensemble.storage.literature_zip_cache import (
    BUNDLE_PATH, ZIP_PATH, RETIREMENT_PATH, retire_archived_literature_zip,
)
from project_ensemble.storage.meeting import _verify_archived_parent_integrity
from project_ensemble.storage.meeting_management import compact_meeting
from project_ensemble.storage.human_outputs import ensure_visible_link
from project_ensemble.orchestration.meeting_gather import gather_meeting


def _archive(tmp_path, *, archived=True, extras=None):
    root = tmp_path / "LR-A1B2C3D4"
    (root / "public/final").mkdir(parents=True)
    (root / "public/meeting_manifest.json").write_text(json.dumps({
        "meeting_id": root.name, "title": "测试", "deliverable_type": "literature_review",
    }))
    (root / "public/task.json").write_text(json.dumps({"description": "测试"}))
    source = root / "public/final/literature_review_report.md"
    source.write_text("# 测试\n\n完整正文。", encoding="utf-8")
    (root / BUNDLE_PATH / "documents").mkdir(parents=True)
    paper = root / BUNDLE_PATH / "documents/paper.pdf"
    paper.write_bytes(b"%PDF-original")
    catalog = root / BUNDLE_PATH / "manifest.json"
    catalog.write_text('{"version": 2}')
    with zipfile.ZipFile(root / ZIP_PATH, "w") as bundle:
        bundle.writestr("documents/paper.pdf", paper.read_bytes())
        bundle.writestr("manifest.json", '{"version": 1}')
        for name, content in (extras or {}).items():
            bundle.writestr(name, content)
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (source, paper, catalog, root / ZIP_PATH)}
    if archived:
        (root / "public/archive_manifest.json").write_text(json.dumps({
            "status": "ARCHIVED", "meeting_id": root.name,
            "retained_document_path": source.relative_to(root).as_posix(),
            "retained_file_sha256": hashes,
        }))
    return root


def test_retirement_preserves_zip_only_and_older_catalog_without_rewriting_archive(tmp_path):
    root = _archive(tmp_path, extras={"documents/only.pdf": b"%PDF-only"})
    manifest_bytes = (root / "public/archive_manifest.json").read_bytes()
    catalog_bytes = (root / BUNDLE_PATH / "manifest.json").read_bytes()
    alias = root / "LITERATURE_BUNDLE.zip"
    alias.symlink_to(ZIP_PATH)
    result = retire_archived_literature_zip(root)
    assert result["removed_bytes"] > 0
    assert result["status"] == "RETIRED"
    assert not (root / ZIP_PATH).exists()
    assert not alias.is_symlink()
    assert (root / "public/archive_manifest.json").read_bytes() == manifest_bytes
    assert (root / BUNDLE_PATH / "manifest.json").read_bytes() == catalog_bytes
    assert (root / BUNDLE_PATH / "documents/only.pdf").read_bytes() == b"%PDF-only"
    proof = json.loads((root / RETIREMENT_PATH).read_text())
    assert len(proof["members"]) == 3
    old_catalog = next(member for member in proof["members"] if member["zip_member"] == "manifest.json")
    assert (root / old_catalog["path"]).read_text() == '{"version": 1}'
    effective = _verify_archived_parent_integrity(root, root.name)
    assert str(ZIP_PATH) not in effective["retained_file_sha256"]
    assert retire_archived_literature_zip(root)["status"] == "ALREADY_ABSENT"
    export = gather_meeting(root, tmp_path / "exports", formats=("md",), rerender=False, include_literature=True)
    assert export["status"] == "COMPLETE"
    assert export["pdf_count"] == 2
    assert not (root / ZIP_PATH).exists()


def test_retirement_does_not_hide_missing_or_modified_preserved_originals(tmp_path):
    root = _archive(tmp_path, extras={"documents/only.pdf": b"%PDF-only"})
    retire_archived_literature_zip(root)
    (root / BUNDLE_PATH / "documents/only.pdf").unlink()
    with pytest.raises(ValueError, match="原文件校验失败"):
        _verify_archived_parent_integrity(root, root.name)


def test_missing_zip_without_maintenance_record_is_not_silently_accepted(tmp_path):
    root = _archive(tmp_path)
    (root / ZIP_PATH).unlink()
    with pytest.raises(ValueError, match="缺少保留文件"):
        _verify_archived_parent_integrity(root, root.name)


def test_retirement_record_cannot_override_frozen_original_file_hashes(tmp_path):
    root = _archive(tmp_path)
    retire_archived_literature_zip(root)
    paper = root / BUNDLE_PATH / "documents/paper.pdf"
    paper.write_bytes(b"changed original")
    proof_path = root / RETIREMENT_PATH
    proof = json.loads(proof_path.read_text())
    member = next(item for item in proof["members"] if item["zip_member"] == "documents/paper.pdf")
    member["sha256"] = hashlib.sha256(paper.read_bytes()).hexdigest()
    proof_path.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match="不得改写"):
        _verify_archived_parent_integrity(root, root.name)


def test_modified_zip_is_not_deleted(tmp_path):
    root = _archive(tmp_path)
    with (root / ZIP_PATH).open("ab") as stream:
        stream.write(b"modified")
    with pytest.raises(ValueError, match="文件校验失败"):
        retire_archived_literature_zip(root)
    assert (root / ZIP_PATH).is_file()
    assert not (root / RETIREMENT_PATH).exists()


def test_unsafe_zip_is_not_deleted(tmp_path):
    root = _archive(tmp_path, extras={"../escape.pdf": b"bad"})
    with pytest.raises(ValueError, match="不安全路径"):
        retire_archived_literature_zip(root)
    assert (root / ZIP_PATH).is_file()
    assert not (tmp_path / "escape.pdf").exists()


def test_new_archive_retains_zip_only_contents_not_zip_cache(tmp_path, monkeypatch):
    root = _archive(tmp_path, archived=False, extras={"documents/only.pdf": b"%PDF-only"})
    monkeypatch.chdir(tmp_path)
    compact_meeting(root)
    assert not (root / ZIP_PATH).exists()
    assert (root / BUNDLE_PATH / "documents/only.pdf").read_bytes() == b"%PDF-only"
    record = _verify_archived_parent_integrity(root, root.name)
    assert record["literature_zip_policy"] == "ON_DEMAND_EXPORT_ONLY"
    assert str(ZIP_PATH) not in record["retained_file_sha256"]


def test_visible_literature_directory_link_is_safe(tmp_path):
    root = _archive(tmp_path)
    link, created = ensure_visible_link(root, link_name="LITERATURE_BUNDLE", target_relative=BUNDLE_PATH)
    assert created and link.is_dir()
    outside = root / "outside"
    outside.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        ensure_visible_link(root, link_name="BAD", target_relative="outside")
