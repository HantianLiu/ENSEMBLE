"""Explicit Human-controlled compaction and deletion of one meeting workspace.

Compaction is a terminal state, not a way to resume a partially deleted vote.
The operation stages a verified minimal workspace before atomically replacing
the source directory. No caller may pass a workspace root or an inferred glob.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import argparse
import time
import errno
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from project_ensemble.storage.meeting import MeetingRepository, provisional_rendering_text
from project_ensemble.storage.meeting_index import (
    inspect_meeting, meeting_is_complete, unregister_meeting,
)
from project_ensemble.user_settings import settings_dir
from project_ensemble.storage.literature_zip_cache import preserve_zip_members
from project_ensemble.runtime.usage_summary import SUMMARY_PATH, build_usage_summary


@dataclass(frozen=True)
class ArchivePlan:
    root: Path
    meeting_id: str
    title: str
    document_source: Path
    document_target: Path
    document_inline_text: str | None
    original_completed: bool
    retained_files: tuple[Path, ...]
    html_aliases: tuple[tuple[str, Path], ...]
    removed_file_count: int
    removed_bytes: int


def _safe_meeting_root(root: str | Path) -> tuple[Path, object]:
    candidate = Path(root).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("会议目录必须是存在的普通目录，不能是符号链接")
    resolved = candidate.resolve()
    entry = inspect_meeting(resolved)
    if resolved.name != entry.meeting_id or not re.fullmatch(r"[A-Z]{1,3}-[A-F0-9]{8}", entry.meeting_id):
        raise ValueError("会议目录名与清单中的会议 ID 不一致；拒绝管理此目录")
    current = Path.cwd().resolve()
    if current == resolved or resolved in current.parents:
        raise ValueError("当前终端位于目标会议目录内；请先切换到会议目录之外")
    return resolved, entry


def _numbered_latest(root: Path, folder: str, prefix: str) -> Path | None:
    candidates = []
    for path in (root / folder).glob(f"{prefix}*.md"):
        match = re.fullmatch(rf"{re.escape(prefix)}(\d+)\.md", path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    return max(candidates, default=(0, None))[1]


def _complete_document(root: Path, deliverable: str) -> tuple[Path, Path, str | None]:
    if deliverable == "literature_review":
        target = Path("public/final/literature_review_report.md")
        if not (root / target).is_file():
            try:
                patched = provisional_rendering_text(root)
            except ValueError:
                patched = None
            if patched is not None:
                return (
                    Path("audit_private/literature_report/publication_patches.json"),
                    target,
                    patched,
                )
        candidates = (
            target,
            Path("public/literature_report/report_before_final_positions.md"),
        )
    elif deliverable == "scholarly_rendering":
        final_dir = root / "public/final/scholarly_rendering"
        if (
            any(final_dir.glob("scholarly_review*.tex"))
            and not any(final_dir.glob("scholarly_review*.md"))
        ):
            raise ValueError(
                "该重绘会议只落盘了 LaTeX 成稿，尚无可供后续会议继承的整篇 Markdown；"
                "请先导出 Markdown，再精简归档。"
            )
        target = Path("public/final/scholarly_rendering/scholarly_review.md")
        candidates = (
            Path("public/final/scholarly_rendering/scholarly_review_structured_v7.md"),
            target,
            Path("SOURCE_DRAFT.md"),
            Path("public/continuation/source_artifacts/final/literature_review_report.md"),
        )
    elif deliverable == "normative_instrument":
        target = Path("public/final/final_report.md")
        candidates = (
            Path("public/final/final_report_v2.md"),
            target,
            Path("public/final/procedurally_certified_resolution.md"),
            _numbered_latest(root, "public/detailed_clauses", "C"),
            _numbered_latest(root, "public/general_principle", "D"),
        )
    elif deliverable == "research_packet":
        target = Path("public/research/research_only_result.json")
        candidates = (target,)
    else:
        raise ValueError(f"此会议交付物类型尚无安全归档规则：{deliverable}")
    for relative in candidates:
        if relative is None:
            continue
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        if relative.suffix == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                if deliverable == "research_packet" and payload.get("status") not in {
                    "RESEARCH_COMPLETE", "RESEARCH_QC_FAILED"
                }:
                    continue
            except (OSError, UnicodeError, ValueError):
                continue
        elif not path.read_text(encoding="utf-8").strip():
            continue
        return relative, target, None
    raise ValueError(
        "找不到一份完整落盘的整篇文稿或最终结果；不能把单个模块/重绘章节冒充终稿。"
        "可保留原会继续运行，或先完成全文组装。"
    )


def _retained_source_files(
    root: Path, document_source: Path, document_target: Path, *, inline_source: bool
) -> tuple[Path, ...]:
    exact = {
        Path("public/meeting_manifest.json"),
        Path("public/task.json"),
        Path("identity_private/meeting_manifest.json"),
        Path("human_private/session_configuration.json"),
        SUMMARY_PATH,
        Path("original_prompt.txt"),
        Path("parent_prompt.txt"),
        Path("meeting_lineage.json"),
        Path("public/research/research_only_result.json"),
    }
    if not inline_source:
        exact.add(document_source)
    if document_target.suffix == ".md" and not inline_source:
        stem = document_source.with_suffix("")
        exact.update((
            stem.with_suffix(".pdf"),
            Path(str(stem) + ".pdf.provenance.json"),
            stem.with_suffix(".tex"),
            stem.with_suffix(".html"),
            Path(str(stem) + ".html.provenance.json"),
        ))
        if document_target == Path("public/final/scholarly_rendering/scholarly_review.md"):
            exact.add(Path("public/final/scholarly_rendering/rendering_appendix.md"))
    # Published HTML does not necessarily share the selected Markdown's stem:
    # math-repaired reports and scholarly renderings use independent names.
    # Keep every finished presentation edition and its provenance, not just
    # the HTML guessed from document_source.
    final_root = root / "public/final"
    if final_root.is_dir():
        for candidate in final_root.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink() and (
                candidate.suffix.lower() in {".html", ".pdf", ".tex"}
                or candidate.name.endswith((".html.provenance.json", ".pdf.provenance.json"))
            ):
                exact.add(candidate.relative_to(root))
    # Root-level title-named shortcuts are user-facing outputs. Preserve
    # regular HTML files and the in-workspace targets of HTML symlinks.
    for candidate in root.iterdir():
        if candidate.suffix.lower() != ".html":
            continue
        if candidate.is_symlink():
            resolved = candidate.resolve()
            if resolved.is_file() and resolved.is_relative_to(root):
                exact.add(resolved.relative_to(root))
        elif candidate.is_file():
            exact.add(candidate.relative_to(root))
    retained_dirs = (
        # Tiny append-only controls retain the latest search permission for
        # post-meeting Q&A instead of reverting to the initialization manifest.
        Path("human_private/runtime_controls"),
        Path("human_private/institutional_documents"),
        Path("public/research/evidence_packets"),
        Path("public/research/cache_invalidations"),
        Path("public/research/literature_bundle"),
        Path("public/human_references"),
        Path("public/corrigenda"),
        Path("public/supplementary_rendering"),
    )
    for directory in retained_dirs:
        base = root / directory
        if base.is_dir():
            exact.update(path.relative_to(root) for path in base.rglob("*") if path.is_file())
    # Temporary chapter IDs are meaningless without their frozen source map.
    # Retain this small provenance layer so archived reports remain auditable
    # and can receive a non-destructive citation correction later.
    report_base = root / "public/literature_report"
    if report_base.is_dir():
        exact.update(path.relative_to(root) for path in report_base.glob("citation_trace*.json"))
        exact.update(path.relative_to(root) for path in report_base.glob(
            "modules/*/research/chapter_citation_catalog.json"
        ))
    return tuple(sorted(path for path in exact
                        if (root / path).is_file() and not (root / path).is_symlink()))


def _retained_html_aliases(root: Path, retained: tuple[Path, ...]) -> tuple[tuple[str, Path], ...]:
    kept = set(retained)
    aliases = []
    for candidate in root.iterdir():
        if candidate.suffix.lower() != ".html" or not candidate.is_symlink():
            continue
        resolved = candidate.resolve()
        if resolved.is_file() and resolved.is_relative_to(root):
            target = resolved.relative_to(root)
            if target in kept:
                aliases.append((candidate.name, target))
    return tuple(sorted(aliases))


def plan_meeting_archive(root: str | Path) -> ArchivePlan:
    root, entry = _safe_meeting_root(root)
    if (root / "public/archive_manifest.json").is_file():
        raise ValueError("该会议已归档精简；无需重复执行")
    document_source, document_target, document_inline_text = _complete_document(
        root,
        "research_packet" if entry.meeting_type == "research" else entry.deliverable_type,
    )
    retained = _retained_source_files(
        root, document_source, document_target,
        inline_source=document_inline_text is not None,
    )
    html_aliases = _retained_html_aliases(root, retained)
    required = {Path("public/meeting_manifest.json"), Path("public/task.json")}
    if not required <= set(retained):
        raise ValueError("源会议缺少接续所需的清单或任务；拒绝精简")
    if Path("original_prompt.txt") not in retained:
        task = json.loads((root / "public/task.json").read_text(encoding="utf-8"))
        if not str(task.get("description", "")).strip():
            raise ValueError("源会议缺少原始提示和任务描述；拒绝精简")
    retained_alias_names = {name for name, _ in html_aliases}
    removed = [path for path in root.rglob("*")
               if path.is_file() and path.relative_to(root) not in retained
               and not (path.parent == root and path.name in retained_alias_names)]
    return ArchivePlan(
        root=root, meeting_id=entry.meeting_id, title=entry.title,
        document_source=document_source, document_target=document_target,
        document_inline_text=document_inline_text,
        original_completed=meeting_is_complete(root), retained_files=retained,
        html_aliases=html_aliases,
        removed_file_count=len(removed),
        removed_bytes=sum(path.lstat().st_size for path in removed),
    )


def _link_inside_root(root: Path, source_relative: Path, stage: Path, target_relative: Path) -> str:
    source = root / source_relative
    resolved = source.resolve()
    if source.is_symlink() or not resolved.is_file() or not resolved.is_relative_to(root):
        raise ValueError(f"保留文件不是会议内的普通文件：{source_relative}")
    target = stage / target_relative
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(resolved, target)
    except OSError:
        shutil.copy2(resolved, target)
    with resolved.open("rb") as handle:
        source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    with target.open("rb") as handle:
        target_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    if source_hash != target_hash:
        raise ValueError(f"暂存归档与源文件哈希不一致：{source_relative}")
    return source_hash


def compact_meeting(root: str | Path) -> dict:
    """Replace one idle meeting with continuation-ready evidence and final text."""
    root, _ = _safe_meeting_root(root)
    repo = MeetingRepository(root)
    with repo.exclusive_run_lock():
        plan = plan_meeting_archive(root)
        stage = Path(tempfile.mkdtemp(prefix=f".{plan.meeting_id}.archive-stage-", dir=root.parent))
        backup = root.parent / f".{plan.meeting_id}.archive-backup-{uuid4().hex[:12]}"
        swapped = False
        try:
            hashes = {}
            # ZIPs are exports, not the sole source of retained literature.
            # Normalize any legacy ZIP-only files before removing the cache.
            for relative in plan.retained_files:
                if relative == plan.document_target and plan.document_source != plan.document_target:
                    continue
                hashes[str(relative)] = _link_inside_root(root, relative, stage, relative)
            for member in preserve_zip_members(root, stage):
                hashes[member["path"]] = member["sha256"]
            # Keep a small private usage snapshot before raw workflow logs are
            # removed. An existing summary is copied verbatim (never overwritten).
            if SUMMARY_PATH not in plan.retained_files:
                summary = json.dumps(build_usage_summary(root), ensure_ascii=False, indent=2)
                summary_file = stage / SUMMARY_PATH
                summary_file.parent.mkdir(parents=True, exist_ok=True)
                summary_file.write_text(summary, encoding="utf-8")
                hashes[str(SUMMARY_PATH)] = hashlib.sha256(summary.encode("utf-8")).hexdigest()
            if Path("original_prompt.txt") not in plan.retained_files:
                task = json.loads((root / "public/task.json").read_text(encoding="utf-8"))
                prompt = str(task["description"]).strip() + "\n"
                (stage / "original_prompt.txt").write_text(prompt, encoding="utf-8")
                hashes["original_prompt.txt"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if plan.document_inline_text is not None:
                publication = stage / plan.document_target
                publication.parent.mkdir(parents=True, exist_ok=True)
                publication.write_text(plan.document_inline_text, encoding="utf-8")
                hashes[str(plan.document_target)] = hashlib.sha256(
                    plan.document_inline_text.encode("utf-8")
                ).hexdigest()
            elif plan.document_source != plan.document_target:
                hashes[str(plan.document_target)] = _link_inside_root(
                    root, plan.document_source, stage, plan.document_target
                )
            for alias, target in plan.html_aliases:
                destination = stage / alias
                if not os.path.lexists(destination):
                    os.symlink(os.path.relpath(stage / target, start=stage), destination)
            publication = stage / plan.document_target
            if plan.document_target.suffix == ".md":
                aliases = ["FINAL_REPORT.md"]
                if plan.document_target == Path("public/final/literature_review_report.md"):
                    aliases.append("LITERATURE_REVIEW.md")
                elif plan.document_target == Path("public/final/scholarly_rendering/scholarly_review.md"):
                    aliases.append("SCHOLARLY_REVIEW.md")
                for alias in aliases:
                    os.link(publication, stage / alias)
                pdf = stage / plan.document_source.with_suffix(".pdf")
                if pdf.is_file():
                    os.link(pdf, stage / "FINAL_REPORT.pdf")
                    if plan.document_target == Path("public/final/scholarly_rendering/scholarly_review.md"):
                        os.link(pdf, stage / "SCHOLARLY_REVIEW.pdf")
                html = stage / plan.document_source.with_suffix(".html")
                if not html.is_file() and plan.document_inline_text is None:
                    preferred_names = (
                        ("LITERATURE_REVIEW.html", "FINAL_REPORT.html")
                        if plan.document_target == Path("public/final/literature_review_report.md")
                        else ("SCHOLARLY_REVIEW.html", "FINAL_REPORT.html")
                    )
                    html = next(
                        (stage / name for name in preferred_names if (stage / name).is_file()),
                        html,
                    )
                if not html.is_file() and plan.document_inline_text is None:
                    html = next((stage / relative for relative in plan.retained_files
                                 if relative.is_relative_to(Path("public/final"))
                                 and relative.suffix.lower() == ".html"), html)
                if html.is_file():
                    if not os.path.lexists(stage / "FINAL_REPORT.html"):
                        os.link(html, stage / "FINAL_REPORT.html")
                    if plan.document_target == Path("public/final/literature_review_report.md"):
                        if not os.path.lexists(stage / "LITERATURE_REVIEW.html"):
                            os.link(html, stage / "LITERATURE_REVIEW.html")
                    elif plan.document_target == Path("public/final/scholarly_rendering/scholarly_review.md"):
                        if not os.path.lexists(stage / "SCHOLARLY_REVIEW.html"):
                            os.link(html, stage / "SCHOLARLY_REVIEW.html")
            elif plan.document_target == Path("public/research/research_only_result.json"):
                os.link(publication, stage / "RESEARCH_RESULT.json")
            status = (
                "ORIGINAL_COMPLETED" if plan.original_completed
                else "PROMOTED_COMPLETE_DRAFT_NOT_PROCEDURALLY_CERTIFIED"
            )
            record = {
                "meeting_id": plan.meeting_id,
                "status": "ARCHIVED",
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "source_certification_status": status,
                "original_document_path": (
                    str(plan.document_source) + "#final_text"
                    if plan.document_inline_text is not None
                    else str(plan.document_source)
                ),
                "retained_document_path": str(plan.document_target),
                "retained_html_aliases": {name: str(target) for name, target in plan.html_aliases},
                "retained_file_sha256": hashes,
                "literature_zip_policy": "ON_DEMAND_EXPORT_ONLY",
                "private_usage_summary_path": str(SUMMARY_PATH),
                "removed_file_count": plan.removed_file_count,
                "removed_bytes": plan.removed_bytes,
                "notice": "Workflow and audit records were removed; this meeting cannot resume its original procedure.",
            }
            (stage / "public/archive_manifest.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            visible_html = next(
                (name for name in (
                    "LITERATURE_REVIEW.html", "SCHOLARLY_REVIEW.html",
                    "FINAL_REPORT.html", "SUPPLEMENTARY_REPORT.html",
                ) if (stage / name).is_file()), None,
            )
            html_notice = (f"保留 HTML：`{visible_html}`。\n\n" if visible_html else "")
            (stage / "MEETING_RESULTS.md").write_text(
                f"# {plan.title}\n\n会议 ID：`{plan.meeting_id}`。\n\n"
                f"归档状态：{status}。此文稿是后续会议可继承的完整文本；"
                "如原会议未完成，它不代表获得了原程序认证。\n\n"
                f"保留文稿：`{plan.document_target}`。\n\n"
                f"{html_notice}"
                "证据包：`public/research/evidence_packets/`；文献原文件："
                "`public/research/literature_bundle/`。ZIP 不长期保存；"
                "可运行 `ensemble gather` 按需打包导出。\n\n"
                "原会议流程和审计原始记录已删除，不能继续原会议。"
                "详情及文件哈希见 `public/archive_manifest.json`。\n",
                encoding="utf-8",
            )
            inspect_meeting(stage)
            if not meeting_is_complete(stage):
                raise ValueError("归档暂存目录未通过最低接续完整性检查")
            root.rename(backup)
            try:
                stage.rename(root)
                swapped = True
            except BaseException:
                backup.rename(root)
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if not swapped and backup.exists() and not root.exists():
                backup.rename(root)
    # The old directory contains the lock file. Wait for the context manager
    # to close its descriptor before deleting it on NFS.
    _remove_renamed_tree(backup, label="归档已完成，但旧流程备份清理未完成")
    return record


def _remove_renamed_tree(path: Path, *, label: str) -> None:
    for attempt in range(8):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            if not path.exists():
                return
            if exc.errno not in {errno.ENOTEMPTY, errno.EBUSY, errno.EACCES} or attempt == 7:
                raise RuntimeError(f"{label}；尚存文件位于 {path}，请人工检查后处理") from exc
            time.sleep(min(0.25 * 2 ** attempt, 8.0))


def delete_meeting(root: str | Path) -> str:
    """Permanently remove one exact, idle meeting workspace after UI confirmation."""
    root, entry = _safe_meeting_root(root)
    repo = MeetingRepository(root)
    hidden = root.parent / f".{entry.meeting_id}.deleting-{uuid4().hex[:12]}"
    with repo.exclusive_run_lock():
        _safe_meeting_root(root)
        root.rename(hidden)
    # On NFS, removing a directory while its meeting.run.lock is still open
    # creates a temporary .nfs file and makes rmtree fail with ENOTEMPTY.
    # The lock must be closed before deleting the renamed directory.
    _remove_renamed_tree(hidden, label="删除中断")
    return entry.meeting_id


def _write_deletion_receipt(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".deletion-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def launch_background_deletion(root: str | Path) -> tuple[Path, Path, int]:
    """Queue an explicitly confirmed deletion and immediately return its receipt."""
    root, entry = _safe_meeting_root(root)
    # Fail immediately if an ENSEMBLE run still owns the meeting lock. The
    # worker reacquires it, so a run started in the gap is still protected.
    with MeetingRepository(root).exclusive_run_lock():
        checked_root, checked_entry = _safe_meeting_root(root)
        if checked_root != root or checked_entry.meeting_id != entry.meeting_id:
            raise ValueError("目标会议在确认后发生变化；拒绝启动删除")
        target_stat = root.stat()
    receipt_dir = settings_dir() / "deletions"
    receipt = receipt_dir / f"{entry.meeting_id}-{uuid4().hex[:12]}.json"
    log_path = receipt.with_suffix(".log")
    token = uuid4().hex
    data = {
        "meeting_id": entry.meeting_id,
        "title": entry.title,
        "meeting_path": str(root),
        "target_device": target_stat.st_dev,
        "target_inode": target_stat.st_ino,
        "status": "QUEUED",
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "log_path": str(log_path),
        "launch_token": token,
    }
    _write_deletion_receipt(receipt, data)
    module_source = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(module_source), environment.get("PYTHONPATH", "")) if part
    )
    command = [
        sys.executable, "-m", "project_ensemble.storage.meeting_management",
        "--delete-worker", str(root), "--receipt", str(receipt), "--token", token,
    ]
    try:
        with log_path.open("ab") as output:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, cwd=root.parent,
                env=environment, start_new_session=(os.name != "nt"),
                close_fds=True,
            )
    except OSError as exc:
        data.update(status="FAILED", finished_at=datetime.now(timezone.utc).isoformat(),
                    error=f"后台进程未能启动：{exc}")
        _write_deletion_receipt(receipt, data)
        raise
    return receipt, log_path, process.pid


def _run_delete_worker(root: Path, receipt: Path, token: str) -> int:
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if (
        data.get("status") != "QUEUED"
        or data.get("meeting_path") != str(root.resolve())
        or data.get("launch_token") != token
    ):
        raise ValueError("后台删除凭据与目标会议不匹配；拒绝删除")
    data.update(status="RUNNING", started_at=datetime.now(timezone.utc).isoformat(), pid=os.getpid())
    _write_deletion_receipt(receipt, data)
    try:
        target_stat = root.stat()
        if (target_stat.st_dev, target_stat.st_ino) != (
            data["target_device"], data["target_inode"]
        ):
            raise ValueError("待删除会议目录已被替换；拒绝删除新的目录")
        delete_meeting(root)
    except Exception as exc:
        data.update(status="FAILED", finished_at=datetime.now(timezone.utc).isoformat(),
                    error=f"{type(exc).__name__}: {exc}")
        _write_deletion_receipt(receipt, data)
        return 1
    try:
        unregister_meeting(root, "")
    except Exception as exc:
        # A stale index entry is filtered by indexed_meetings because the
        # workspace is gone; do not misreport the data deletion as failed.
        data["index_warning"] = f"{type(exc).__name__}: {exc}"
    data.update(status="DONE", finished_at=datetime.now(timezone.utc).isoformat())
    data.pop("launch_token", None)
    _write_deletion_receipt(receipt, data)
    return 0


def _worker_main() -> int:
    parser = argparse.ArgumentParser(description="Internal detached meeting deletion worker")
    parser.add_argument("--delete-worker", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    return _run_delete_worker(Path(args.delete_worker), Path(args.receipt), args.token)


if __name__ == "__main__":
    raise SystemExit(_worker_main())
