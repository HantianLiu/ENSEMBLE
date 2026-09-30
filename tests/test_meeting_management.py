import json
import hashlib
import errno
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_ensemble.domain import DeliverableType, InheritanceMode
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.storage.meeting_index import inspect_meeting, meeting_is_complete
from project_ensemble.storage.meeting_management import (
    compact_meeting,
    delete_meeting,
    launch_background_deletion,
    plan_meeting_archive,
)
from project_ensemble.cli import _assert_meeting_runtime_compatible
from project_ensemble import cli
from project_ensemble.errors import PolicyNotConfiguredError


def _meeting(tmp_path: Path, meeting_id: str = "LR-A1B2C3D4") -> MeetingRepository:
    governance = tmp_path / "governance"
    governance.mkdir(exist_ok=True)
    (governance / "rule.md").write_text("rule", encoding="utf-8")
    return MeetingRepository.create(
        tmp_path / "workspaces",
        selected_models=[("fake", "writer")],
        chair_model=("fake", "chair"),
        governance_docs=governance,
        task_description="比较两条研究路线。",
        meeting_title="测试文献会议",
        forced_meeting_id=meeting_id,
    )


def test_archive_promotes_only_complete_draft_and_keeps_successor_assets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parent = _meeting(tmp_path)
    parent.docs.write_once(
        "public/literature_report/report_before_final_positions.md",
        "# 完整报告\n\n正文和引文。\n",
    )
    parent.docs.write_once("public/research/evidence_packets/RP-1.json", "{\"claim\": \"test\"}")
    parent.docs.write_once("public/literature_report/citation_trace_assembly_v3.json", "{}")
    parent.docs.write_once(
        "public/literature_report/modules/RM-01/research/chapter_citation_catalog.json", "{}"
    )
    parent.docs.write_once("public/human_references/paper.pdf", b"%PDF-1.4 fake")
    parent.docs.write_once("governance_private/transcript.txt", "过程记录")
    manifest = json.loads((parent.root / "public/meeting_manifest.json").read_text(encoding="utf-8"))
    manifest["deliverable_type"] = DeliverableType.LITERATURE_REVIEW.value
    (parent.root / "public/meeting_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    plan = plan_meeting_archive(parent.root)
    assert plan.document_source == Path("public/literature_report/report_before_final_positions.md")
    assert plan.original_completed is False
    result = compact_meeting(parent.root)
    assert result["source_certification_status"] == "PROMOTED_COMPLETE_DRAFT_NOT_PROCEDURALLY_CERTIFIED"
    assert meeting_is_complete(parent.root)
    assert not (parent.root / "governance_private/transcript.txt").exists()
    assert (parent.root / "public/final/literature_review_report.md").read_text(encoding="utf-8").startswith("# 完整报告")
    assert (parent.root / "public/research/evidence_packets/RP-1.json").is_file()
    assert (parent.root / "public/literature_report/citation_trace_assembly_v3.json").is_file()
    assert (parent.root / "public/literature_report/modules/RM-01/research/chapter_citation_catalog.json").is_file()
    assert (parent.root / "public/human_references/paper.pdf").is_file()
    assert (parent.root / "original_prompt.txt").is_file()
    with pytest.raises(PolicyNotConfiguredError, match="ARCHIVED_MEETING_NOT_RESUMABLE"):
        _assert_meeting_runtime_compatible(parent, governance_docs=tmp_path / "governance")

    child = MeetingRepository.create(
        tmp_path / "children",
        selected_models=[("fake", "writer")],
        chair_model=("fake", "chair"),
        governance_docs=tmp_path / "governance",
        task_description="继续研究。",
        meeting_title="后续会议",
        parent_meeting_id=parent.meeting_id,
        parent_meeting_path=parent.root,
        inheritance_mode=InheritanceMode.BOTH,
        forced_meeting_id="DL-B2C3D4E5",
    )
    lineage = json.loads(
        (child.root / "public/continuation/lineage.json").read_text(encoding="utf-8")
    )
    assert lineage["source_status"] == "ARCHIVED_UNCERTIFIED_DRAFT"
    assert (child.root / "public/research/evidence_packets/RP-1.json").is_file()
    assert (child.root / "public/human_references/paper.pdf").is_file()

    (parent.root / "public/research/evidence_packets/RP-1.json").write_text(
        "{\"claim\": \"changed\"}", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="文件校验失败"):
        MeetingRepository.create(
            tmp_path / "children",
            selected_models=[("fake", "writer")],
            chair_model=("fake", "chair"),
            governance_docs=tmp_path / "governance",
            task_description="另一次接续。",
            meeting_title="后续会议二",
            parent_meeting_id=parent.meeting_id,
            parent_meeting_path=parent.root,
            inheritance_mode=InheritanceMode.BOTH,
            forced_meeting_id="DL-E5F6A7B8",
        )


def test_archive_keeps_html_and_its_provenance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path)
    manifest_path = repo.root / "public/meeting_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["deliverable_type"] = DeliverableType.LITERATURE_REVIEW.value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    base = "public/final/literature_review_report"
    repo.docs.write_once(base + ".md", "# 完整报告\n\n正文。\n")
    repo.docs.write_once(base + ".html", "<!doctype html><title>完整报告</title>")
    repo.docs.write_once(base + ".html.provenance.json", '{"profile":"test"}')
    repo.docs.write_once("public/supplementary_rendering/old/report.html", "旧补充排版")

    plan = plan_meeting_archive(repo.root)
    assert Path(base + ".html") in plan.retained_files
    compact_meeting(repo.root)
    assert (repo.root / (base + ".html")).read_text(encoding="utf-8").startswith("<!doctype")
    assert (repo.root / (base + ".html.provenance.json")).is_file()
    assert (repo.root / "LITERATURE_REVIEW.html").is_file()
    assert (repo.root / "public/supplementary_rendering/old/report.html").is_file()


def test_archived_meeting_list_uses_report_title_and_original_creation_date(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="LR-C0FFEE10")
    manifest_path = repo.root / "public/meeting_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["deliverable_type"] = DeliverableType.LITERATURE_REVIEW.value
    manifest["created_at"] = "2025-03-04T23:30:00+00:00"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    repo.docs.write_once(
        "public/final/literature_review_report.md",
        "# 正文确定的正式标题\n\n## 摘要\n\n正文。\n",
    )
    compact_meeting(repo.root)

    entry = inspect_meeting(repo.root)
    assert entry.title == "正文确定的正式标题"
    assert entry.created_at == "2025-03-04T23:30:00+00:00"

    class CapturingWizard:
        def _choose_one(self, _title, options):
            label = options[0][1]
            assert "正文确定的正式标题" in label
            assert "2025-03-04 UTC" in label
            assert "测试文献会议" not in label
            return options[0][0]

    monkeypatch.setattr(cli, "_available_meeting_paths", lambda _config: [repo.root])
    assert cli._select_existing_meeting(CapturingWizard(), tmp_path / "ensemble.toml") == repo.root


def test_archive_keeps_math_repaired_html_and_visible_shortcuts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="LR-C0FFEE11")
    manifest_path = repo.root / "public/meeting_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["deliverable_type"] = DeliverableType.LITERATURE_REVIEW.value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    repo.docs.write_once("public/final/literature_review_report.md", "# 完整报告\n\n正文。\n")
    html_path = Path("public/final/literature_review_report_math_repaired.html")
    repo.docs.write_once(html_path, "<!doctype html><title>修复版</title>")
    repo.docs.write_once(str(html_path) + ".provenance.json", '{"profile":"math-repaired"}')
    (repo.root / "LITERATURE_REVIEW.html").symlink_to(html_path)
    (repo.root / "线张力调研（报告）.html").symlink_to(html_path)

    plan = plan_meeting_archive(repo.root)
    assert html_path in plan.retained_files
    assert ("线张力调研（报告）.html", html_path) in plan.html_aliases
    record = compact_meeting(repo.root)
    assert (repo.root / html_path).read_text(encoding="utf-8").startswith("<!doctype")
    assert (repo.root / (str(html_path) + ".provenance.json")).is_file()
    assert (repo.root / "LITERATURE_REVIEW.html").resolve() == (repo.root / html_path).resolve()
    assert (repo.root / "线张力调研（报告）.html").resolve() == (repo.root / html_path).resolve()
    assert (repo.root / "FINAL_REPORT.html").is_file()
    assert record["retained_html_aliases"]["线张力调研（报告）.html"] == str(html_path)


def test_archive_keeps_scholarly_html_with_different_markdown_stem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="SR-C0FFEE12")
    manifest_path = repo.root / "public/meeting_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["deliverable_type"] = DeliverableType.SCHOLARLY_RENDERING.value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    repo.docs.write_once("public/final/scholarly_rendering/scholarly_review_structured_v7.md", "# 重绘终稿\n")
    html_path = Path("public/final/scholarly_rendering/scholarly_review.html")
    repo.docs.write_once(html_path, "<!doctype html><title>重绘</title>")
    (repo.root / "SCHOLARLY_REVIEW.html").symlink_to(html_path)

    compact_meeting(repo.root)
    assert (repo.root / html_path).is_file()
    assert (repo.root / "SCHOLARLY_REVIEW.html").resolve() == (repo.root / html_path).resolve()
    assert (repo.root / "FINAL_REPORT.html").is_file()


def test_archive_refuses_partial_module_and_leaves_meeting_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path)
    repo.docs.write_once("public/literature_report/modules/RM-01/draft.md", "# 部分模块")
    manifest = json.loads((repo.root / "public/meeting_manifest.json").read_text(encoding="utf-8"))
    manifest["deliverable_type"] = DeliverableType.LITERATURE_REVIEW.value
    (repo.root / "public/meeting_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="完整落盘"):
        compact_meeting(repo.root)
    assert (repo.root / "public/literature_report/modules/RM-01/draft.md").is_file()
    assert not (repo.root / "public/archive_manifest.json").exists()


def test_delete_only_exact_temp_meeting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path)
    other = tmp_path / "workspaces" / "keep.txt"
    other.write_text("keep", encoding="utf-8")
    meeting_id = repo.meeting_id
    assert delete_meeting(repo.root) == meeting_id
    assert not repo.root.exists()
    assert other.read_text(encoding="utf-8") == "keep"


def test_archive_uses_latest_normative_version_not_older_final(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="DL-C3D4E5F6")
    repo.docs.write_once("public/final/final_report.md", "# 旧版\n")
    repo.docs.write_once("public/final/final_report_v2.md", "# 新版\n")
    repo.docs.write_once("public/final/think_tank_epistemic_reviews.json", "{\"internal\": true}")
    result = compact_meeting(repo.root)
    assert result["original_document_path"] == "public/final/final_report_v2.md"
    assert (repo.root / "public/final/final_report.md").read_text(encoding="utf-8") == "# 新版\n"
    assert not (repo.root / "public/final/think_tank_epistemic_reviews.json").exists()


def test_archive_recovers_original_prompt_from_task_only_for_legacy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="DL-D4E5F6A7")
    repo.docs.write_once("public/final/final_report.md", "# 成果\n")
    (repo.root / "original_prompt.txt").unlink()
    compact_meeting(repo.root)
    assert (repo.root / "original_prompt.txt").read_text(encoding="utf-8").strip() == "比较两条研究路线。"


def test_running_meeting_cannot_be_compacted_or_deleted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="DL-E5F6A7B8")
    repo.docs.write_once("public/final/final_report.md", "# 成果\n")
    with repo.exclusive_run_lock():
        with pytest.raises(ValueError, match="正在另一个 ensemble 进程中运行"):
            compact_meeting(repo.root)
        with pytest.raises(ValueError, match="正在另一个 ensemble 进程中运行"):
            delete_meeting(repo.root)
    assert (repo.root / "public/final/final_report.md").is_file()


def test_archive_prefers_verified_chair_patched_whole_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="LR-F6A7B8C9")
    manifest = json.loads((repo.root / "public/meeting_manifest.json").read_text(encoding="utf-8"))
    manifest["deliverable_type"] = DeliverableType.LITERATURE_REVIEW.value
    (repo.root / "public/meeting_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    repo.docs.write_once(
        "public/literature_report/report_before_final_positions.md", "# 较早的完整草稿\n"
    )
    latest = "# 主席修订后的整篇草稿\n\n最新正文。\n"
    repo.docs.write_once(
        "audit_private/literature_report/publication_patches.json",
        json.dumps({"final_text": latest, "decisions": []}, ensure_ascii=False),
    )
    repo.docs.write_once(
        "chair_private/literature_report/readability_certification.json",
        json.dumps({
            "status": "REVISION_REQUIRED",
            "source_sha256": hashlib.sha256(latest.encode("utf-8")).hexdigest(),
        }),
    )
    plan = plan_meeting_archive(repo.root)
    assert plan.document_inline_text == latest
    compact_meeting(repo.root)
    assert (repo.root / "public/final/literature_review_report.md").read_text(encoding="utf-8") == latest
    assert not (repo.root / "audit_private/literature_report/publication_patches.json").exists()


def test_delete_retries_transient_nonempty_after_lock_is_released(tmp_path, monkeypatch):
    import project_ensemble.storage.meeting_management as management

    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="DL-A7B8C9D0")
    meeting_root = repo.root
    real_rmtree = management.shutil.rmtree
    attempts = []

    def transient_nfs_error(path, *args, **kwargs):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError(errno.ENOTEMPTY, "simulated NFS temporary file")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(management.shutil, "rmtree", transient_nfs_error)
    monkeypatch.setattr(management.time, "sleep", lambda delay: None)
    assert delete_meeting(meeting_root) == "DL-A7B8C9D0"
    assert len(attempts) == 2
    assert not meeting_root.exists()


def test_archive_retries_nfs_cleanup_after_releasing_lock(tmp_path, monkeypatch):
    import project_ensemble.storage.meeting_management as management

    monkeypatch.chdir(tmp_path)
    repo = _meeting(tmp_path, meeting_id="DL-D0E1F2A3")
    repo.docs.write_once("public/final/final_report.md", "# 成果\n")
    real_rmtree = management.shutil.rmtree
    attempts = []

    def transient_nfs_error(path, *args, **kwargs):
        if ".archive-backup-" in str(path):
            attempts.append(path)
            if len(attempts) == 1:
                raise OSError(errno.ENOTEMPTY, "simulated NFS temporary file")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(management.shutil, "rmtree", transient_nfs_error)
    monkeypatch.setattr(management.time, "sleep", lambda delay: None)
    compact_meeting(repo.root)
    assert len(attempts) == 2
    assert (repo.root / "public/archive_manifest.json").is_file()


def test_background_delete_reports_completion_outside_meeting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
    repo = _meeting(tmp_path, meeting_id="DL-B8C9D0E1")
    meeting_root = repo.root
    receipt, log_path, pid = launch_background_deletion(meeting_root)
    assert pid > 0
    assert receipt.parent == tmp_path / "settings" / "ensemble" / "deletions"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = json.loads(receipt.read_text(encoding="utf-8"))["status"]
        if status in {"DONE", "FAILED"}:
            break
        time.sleep(0.05)
    assert status == "DONE", log_path.read_text(encoding="utf-8")
    assert not meeting_root.exists()


def test_finish_offer_only_appears_for_interactive_completed_meeting(tmp_path, monkeypatch):
    import project_ensemble.cli as cli

    root = tmp_path / "DL-C9D0E1F2"
    root.mkdir()
    repo = SimpleNamespace(root=root, meeting_id="DL-C9D0E1F2")
    selections = []
    compacted = []
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "TerminalWizard", lambda: SimpleNamespace(
        _choose_one=lambda title, options: selections.append(title) or "compact"
    ))
    monkeypatch.setattr(cli, "_confirm_and_compact_meeting", lambda *args: compacted.append(args))
    monkeypatch.setattr(cli, "meeting_is_complete", lambda root: False)
    cli._offer_compaction_after_completion(repo, tmp_path / "config.toml")
    assert not selections
    monkeypatch.setattr(cli, "meeting_is_complete", lambda root: True)
    cli._offer_compaction_after_completion(repo, tmp_path / "config.toml")
    assert len(selections) == 1
    assert compacted == [(root, tmp_path / "config.toml")]
