"""Collect selected final reports and meeting-local literature PDFs."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from project_ensemble.orchestration.supplementary_rendering import (
    complete_render_source, render_additional_formats,
)
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.storage.meeting_index import inspect_meeting


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _safe_name(value: str, fallback: str = "会议") -> str:
    """Use readable titles/basenames, not internal IDs or directory trees."""
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    # Leave room for extension and a human-readable collision suffix.
    value = value.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    return value if value and value not in {".", ".."} else fallback


def _unique_name(value: str, used: set[str], *, file: bool = False) -> str:
    stem, suffix = (Path(value).stem, Path(value).suffix) if file else (value, "")
    candidate = value
    number = 1
    while candidate.casefold() in used:
        number += 1
        candidate = f"{stem}（{number}）{suffix}"
    used.add(candidate.casefold())
    return candidate


def _inside_file(root: Path, path: Path) -> Path | None:
    resolved = path.resolve()
    return resolved if resolved.is_relative_to(root) and resolved.is_file() else None


def _existing_format(root: Path, source: Path, format_name: str) -> Path | None:
    source_sha = _digest(source)
    # A supplementary edition explicitly records which complete text it renders.
    for provenance in sorted((root / "public/supplementary_rendering").rglob(
        f"report.{format_name}.provenance.json"
    )):
        try:
            record = json.loads(provenance.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        output = _inside_file(root, provenance.with_name(f"report.{format_name}"))
        if (output is not None and record.get("source_sha256") == source_sha
                and record.get("output_sha256") == _digest(output)):
            return output
    for name in ("literature_review_publication_manifest.json", "final_publication_manifest.json"):
        manifest = root / "public/final" / name
        if not manifest.is_file():
            continue
        record = json.loads(manifest.read_text(encoding="utf-8"))
        markdown_name = record.get("report_markdown_path")
        if not isinstance(markdown_name, str):
            continue
        markdown = _inside_file(root, root / markdown_name)
        if markdown is None or _digest(markdown) != source_sha:
            continue
        output_name = record.get(f"report_{format_name}_path")
        if isinstance(output_name, str):
            output = _inside_file(root, root / output_name)
            if output is not None:
                expected = record.get(f"report_{format_name}_sha256")
                if expected and _digest(output) != expected:
                    raise ValueError(f"{format_name.upper()} 与出版记录中的哈希不一致")
                return output
    sibling = _inside_file(root, source.with_suffix("." + format_name))
    if sibling is not None:
        archive_manifest = root / "public/archive_manifest.json"
        if archive_manifest.is_file():
            record = json.loads(archive_manifest.read_text(encoding="utf-8"))
            expected = record.get("retained_file_sha256", {}).get(str(sibling.relative_to(root)))
            if expected and _digest(sibling) != expected:
                raise ValueError(f"{format_name.upper()} 与归档哈希不一致")
        return sibling
    # Root aliases can be symlinks or the hard links made by compaction.
    for name in ("FINAL_REPORT_REVISED", "FINAL_REPORT", "LITERATURE_REVIEW", "SCHOLARLY_REVIEW"):
        markdown = _inside_file(root, root / f"{name}.md")
        output = _inside_file(root, root / f"{name}.{format_name}")
        if markdown is not None and output is not None and _digest(markdown) == source_sha:
            return output
    return None


def _stream_digest(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _literature_zip(root: Path, target: Path, entry) -> tuple[int, list[str]]:
    records: dict[str, dict] = {}
    used_names: set[str] = set()
    issues: list[str] = []
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for base_name in ("public/research", "public/human_references"):
            base = root / base_name
            for path in sorted(base.rglob("*")):
                if path.suffix.lower() != ".pdf":
                    continue
                source = _inside_file(root, path)
                if source is None:
                    continue
                try:
                    digest = _digest(source)
                    origin = path.relative_to(root).as_posix()
                    if digest in records:
                        records[digest]["original_paths"].append(origin)
                        continue
                    archive_name = _unique_name(_safe_name(path.stem, "文献") + ".pdf", used_names, file=True)
                    archive.write(source, archive_name)
                    records[digest] = {
                        "archive_path": archive_name, "sha256": digest,
                        "size_bytes": source.stat().st_size, "original_paths": [origin],
                    }
                except OSError as exc:
                    issues.append(f"文献 PDF {path.name} 未收集：{exc}")
        # Some older workspaces retain only the downloadable ZIP.
        source_zip = _inside_file(root, root / "public/research/literature_bundle.zip")
        if source_zip is not None:
            try:
                with zipfile.ZipFile(source_zip) as bundle:
                    for item in bundle.infolist():
                        relative = PurePosixPath(item.filename)
                        if item.is_dir() or relative.suffix.lower() != ".pdf":
                            continue
                        if relative.is_absolute() or ".." in relative.parts or "\\" in item.filename:
                            issues.append(f"跳过文献包中的无效路径：{item.filename}")
                            continue
                        with bundle.open(item) as original:
                            digest = _stream_digest(original)
                        origin = "public/research/literature_bundle.zip:" + item.filename
                        if digest in records:
                            records[digest]["original_paths"].append(origin)
                            continue
                        archive_name = _unique_name(
                            _safe_name(relative.stem, "文献") + ".pdf", used_names, file=True,
                        )
                        with bundle.open(item) as original, archive.open(archive_name, "w") as output:
                            shutil.copyfileobj(original, output, length=1024 * 1024)
                        records[digest] = {
                            "archive_path": archive_name, "sha256": digest,
                            "size_bytes": item.file_size, "original_paths": [origin],
                        }
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                issues.append(f"原文献 ZIP 未完整读取：{exc}")
    return len(records), issues


def gather_meeting(
    root: str | Path, destination: str | Path, *,
    formats: tuple[str, ...], rerender: bool, include_literature: bool,
) -> dict:
    """Collect one meeting; failures in one format preserve other exports."""
    if (not formats and not include_literature) or any(name not in {"md", "html", "pdf"} for name in formats):
        raise ValueError("请选择 Markdown、HTML 或 PDF 格式")
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if destination.is_relative_to(root):
        raise ValueError("收集目录应位于会议目录之外")
    entry = inspect_meeting(root)
    destination.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(entry.title)
    folder = destination / stem
    number = 1
    while True:
        try:
            folder.mkdir()
            break
        except FileExistsError:
            number += 1
            folder = destination / f"{stem}（{number}）"
    result = {
        "meeting_id": entry.meeting_id, "title": entry.title,
        "meeting_path": str(root), "folder": str(folder), "files": [],
        "literature_zip": None, "pdf_count": 0, "issues": [], "status": "COMPLETE",
    }
    try:
        source = complete_render_source(root) if formats else None
        if formats and source is None:
            raise ValueError("会议尚无已完成的完整最终文本")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["issues"].append(f"最终文本未收集：{exc}")
        source = None
    if source is not None:
        result["source_path"] = str(source.relative_to(root))
        result["source_sha256"] = _digest(source)
        for name in dict.fromkeys(formats):
            try:
                if name == "md":
                    original = source
                elif rerender:
                    original = render_additional_formats(MeetingRepository(root), formats=(name,))[name]
                    provenance = original.resolve().with_name(f"report.{name}.provenance.json")
                    record = json.loads(provenance.read_text(encoding="utf-8"))
                    if record.get("source_sha256") != result["source_sha256"]:
                        raise ValueError("完整稿在收集期间发生变化；请再次收集")
                else:
                    original = _existing_format(root, source, name)
                    if original is None:
                        raise ValueError("当前完整稿没有此格式；可选择重新渲染后收集")
                target = folder / f"{stem}.{name}"
                shutil.copy2(original, target)
                result["files"].append({
                    "format": name, "path": str(target), "sha256": _digest(target),
                })
            except Exception as exc:
                result["issues"].append(f"{name.upper()} 未收集：{exc}")
    if include_literature:
        target = folder / f"{stem}（文献PDF）.zip"
        try:
            count, issues = _literature_zip(root, target, entry)
            result.update(literature_zip=str(target), pdf_count=count)
            result["issues"].extend(issues)
        except Exception as exc:
            # Remove only the incomplete new ZIP created by this operation.
            target.unlink(missing_ok=True)
            result["issues"].append(f"文献包未完成：{exc}")
    if result["issues"]:
        result["status"] = "PARTIAL" if result["files"] or result["literature_zip"] else "SKIPPED"
    return result


def gather_collection(
    roots: list[str | Path], destination: str | Path, *, formats: tuple[str, ...],
    include_literature: bool, progress: Callable[[str], None] | None = None,
) -> dict:
    """One outer ZIP, one title-named folder per meeting, no internal metadata.

    Staging is temporary and bounded to one meeting. Original reports, source
    PDFs and frozen records stay intact; errors in a format do not discard
    successful exports. Existing ZIPs are never overwritten.
    """
    if (not formats and not include_literature) or any(name not in {"md", "html", "pdf"} for name in formats):
        raise ValueError("请选择最终文本格式或文献 PDF")
    selected = list(dict.fromkeys(Path(root).resolve() for root in roots))
    if not selected:
        raise ValueError("请选择要收集的会议")
    destination = Path(destination).expanduser().resolve()
    if destination.is_dir() or destination.suffix.lower() != ".zip":
        destination = destination / "会议资料.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    stem = destination.stem
    number = 1
    while True:
        try:
            archive = zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1)
            break
        except FileExistsError:
            number += 1
            destination = destination.with_name(f"{stem}（{number}）.zip")
    results: list[dict] = []
    used_folders: set[str] = set()
    with archive:
        for root in selected:
            try:
                entry = inspect_meeting(root)
                if progress:
                    progress(entry.title)
                folder_name = _unique_name(_safe_name(entry.title), used_folders)
                with tempfile.TemporaryDirectory(prefix="ensemble-gather-") as temporary:
                    result = gather_meeting(
                        root, temporary, formats=formats, rerender=True,
                        include_literature=include_literature,
                    )
                    for item in result["files"]:
                        path = Path(item["path"])
                        member = f"{folder_name}/{path.name}"
                        archive.write(path, member)
                        item["path"] = member
                    if result["literature_zip"]:
                        path = Path(result["literature_zip"])
                        member = f"{folder_name}/{path.name}"
                        archive.write(path, member, compress_type=zipfile.ZIP_STORED)
                        result["literature_zip"] = member
                    result["folder"] = folder_name
                    results.append(result)
            except Exception as exc:
                results.append({
                    "meeting_path": str(root), "title": root.name, "status": "SKIPPED",
                    "files": [], "literature_zip": None, "pdf_count": 0, "issues": [str(exc)],
                })
    return {"path": str(destination), "meetings": results}
