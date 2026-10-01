"""Post-meeting presentation formats from an immutable complete Markdown source."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from project_ensemble.orchestration.academic_html import (
    HTML_RENDERING_PROFILE, render_academic_review_html,
)
from project_ensemble.orchestration.academic_pdf import render_academic_review_pdf
from project_ensemble.orchestration.final_publication import validate_pdf
from project_ensemble.orchestration.math_rendering import safe_pdf_font_grouping_repair
from project_ensemble.orchestration.report_palette import PALETTES, read_meeting_palette
from project_ensemble.storage.human_outputs import ensure_visible_link, titled_report_stem
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.storage.meeting_index import meeting_is_complete


_COMPLETE_DOCUMENTS = (
    "FINAL_REPORT_REVISED.md",
    "FINAL_REPORT.md",
    "public/final/literature_review_report_math_repaired.md",
    "public/final/literature_review_report.md",
    "public/final/scholarly_rendering/scholarly_review.md",
    "public/final/final_report_v2.md",
    "public/final/final_report.md",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _citation_links(root: Path) -> dict[str, dict[str, str]]:
    manifest_path = root / "public/final/literature_review_publication_manifest.json"
    manifest = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    trace_path = root / str(manifest.get("citation_trace_path", ""))
    if not trace_path.is_file() or root not in trace_path.resolve().parents:
        trace_path = next((root / relative for relative in (
            "public/literature_report/citation_trace_assembly_v4.json",
            "public/literature_report/citation_trace_assembly_v3.json",
            "public/literature_report/citation_trace_assembly_v2.json",
            "public/literature_report/citation_trace.json",
        ) if (root / relative).is_file()), root / "__missing_citation_trace__")
    try:
        references = json.loads(trace_path.read_text(encoding="utf-8")).get("references", [])
    except (OSError, ValueError):
        return {}
    if not isinstance(references, list):
        return {}
    pdf_by_source: dict[str, str] = {}
    packet_dir = root / "public/research/evidence_packets"
    if packet_dir.is_dir():
        for packet_path in packet_dir.glob("*.json"):
            try:
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for source in packet.get("sources", []):
                if isinstance(source, dict) and source.get("source_id") and source.get("original_document_url"):
                    pdf_by_source.setdefault(str(source["source_id"]), str(source["original_document_url"]))
    links: dict[str, dict[str, str]] = {}
    for item in references:
        if not isinstance(item, dict) or not item.get("reference_number"):
            continue
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(item.get("doi") or ""), flags=re.I)
        source = f"https://doi.org/{doi}" if doi else str(item.get("url") or "")
        entry = {"source": source}
        pdf = pdf_by_source.get(str(item.get("source_id") or ""))
        if pdf:
            entry["pdf"] = pdf
        links[str(item["reference_number"])] = entry
    return links


def _safe_source(root: Path, relative: str | Path) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.suffix.lower() != ".md":
        raise ValueError("补充排版的完整 Markdown 来源缺失或不在当前会议目录内")
    return path


def _verify_publication_hash(root: Path, source: Path) -> None:
    relative = str(source.relative_to(root))
    for manifest_relative in (
        "public/final/literature_review_publication_manifest.json",
        "public/final/final_publication_manifest.json",
    ):
        manifest_path = root / manifest_relative
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("report_markdown_path") != relative:
            continue
        expected = manifest.get("report_markdown_sha256")
        if expected and _sha(source.read_bytes()) != expected:
            raise ValueError("已发表 Markdown 与冻结出版哈希不一致；拒绝补充排版")


def complete_render_source(root: str | Path) -> Path | None:
    """Do not promote a module draft or a research-only JSON to a final report."""
    root = Path(root).resolve()
    if not meeting_is_complete(root):
        return None
    corrected = sorted(
        root.glob("public/corrigenda/revision_*.md"),
        key=lambda path: int(path.stem.rsplit("_", 1)[-1]) if path.stem.rsplit("_", 1)[-1].isdigit() else -1,
    )
    for path in reversed(corrected):
        if path.with_suffix(".pdf").is_file() and path.with_suffix(".json").is_file():
            return _safe_source(root, path.relative_to(root))
    archive = root / "public/archive_manifest.json"
    if archive.is_file():
        record = json.loads(archive.read_text(encoding="utf-8"))
        relative = record.get("retained_document_path")
        if not isinstance(relative, str) or not relative.endswith(".md"):
            return None
        source = _safe_source(root, relative)
        expected = record.get("retained_file_sha256", {}).get(relative)
        if expected and _sha(source.read_bytes()) != expected:
            raise ValueError("归档会议的保留文稿与归档哈希不一致；拒绝补充排版")
        return source
    for relative in _COMPLETE_DOCUMENTS:
        try:
            source = _safe_source(root, relative)
        except ValueError:
            continue
        _verify_publication_hash(root, source)
        return source
    return None


def render_additional_formats(
    repo: MeetingRepository, *, formats: tuple[Literal["html", "pdf"], ...],
    palette: str | None = None,
) -> dict[str, Path]:
    """Create only presentation derivatives; never rewrite source or governance files."""
    if not formats or any(item not in {"html", "pdf"} for item in formats):
        raise ValueError("请选择 HTML、PDF 或两种补充格式")
    with repo.exclusive_run_lock():
        source = complete_render_source(repo.root)
        if source is None:
            raise ValueError("该已完成会议没有可补充排版的完整 Markdown 文稿")
        root = repo.root.resolve()
        selected_palette = palette or read_meeting_palette(root)
        if selected_palette not in PALETTES:
            raise ValueError("unknown report palette")
        source_bytes = source.read_bytes()
        source_sha = _sha(source_bytes)
        markdown = source_bytes.decode("utf-8")
        source_relative = source.relative_to(root)
        target_directory = Path("public/supplementary_rendering") / source_sha[:16]
        result: dict[str, Path] = {}
        for format_name in dict.fromkeys(formats):
            profile = (
                HTML_RENDERING_PROFILE if format_name == "html"
                else "ACADEMIC_REVIEW_MATH_SAFE_V2"
            )
            relative = target_directory / profile / selected_palette / f"report.{format_name}"
            path = root / relative
            provenance_relative = target_directory / profile / selected_palette / f"report.{format_name}.provenance.json"
            provenance_path = root / provenance_relative
            if path.is_file():
                if not provenance_path.is_file():
                    raise ValueError(f"现有 {format_name.upper()} 衍生文件缺少来源记录；拒绝覆盖")
                frozen = json.loads(provenance_path.read_text(encoding="utf-8"))
                if (frozen.get("source_sha256") != source_sha
                        or frozen.get("output_sha256") != _sha(path.read_bytes())
                        or frozen.get("source_path") != str(source_relative)
                        or frozen.get("rendering_profile") != profile
                        or frozen.get("palette") != selected_palette):
                    raise ValueError(f"现有 {format_name.upper()} 衍生文件与来源记录不符；拒绝覆盖")
            else:
                if provenance_path.exists():
                    raise ValueError(f"现有 {format_name.upper()} 来源记录缺少对应文件；拒绝覆盖")
                if format_name == "html":
                    output = render_academic_review_html(
                        markdown, meeting_id=root.name, palette=selected_palette,
                        reference_links=_citation_links(root),
                    ).encode("utf-8")
                else:
                    output, font = render_academic_review_pdf(
                        markdown, meeting_id=root.name, palette=selected_palette,
                        repair_formula=lambda formula, _error, _display: safe_pdf_font_grouping_repair(formula),
                    )
                    validate_pdf(output)
                repo.docs.write_once(relative, output)
                repo.docs.write_once(provenance_relative, json.dumps({
                    "source_path": str(source_relative),
                    "source_sha256": source_sha,
                    "output_sha256": _sha(output),
                    "rendering_profile": profile,
                    "palette": selected_palette,
                    "embedded_font": str(font) if format_name == "pdf" else None,
                    "meeting_status": "COMPLETE_SOURCE_PRESENTATION_ONLY",
                }, ensure_ascii=False, indent=2))
            visible, _ = ensure_visible_link(
                root, link_name=f"SUPPLEMENTARY_REPORT.{format_name}",
                target_relative=relative, replace_symlink=True,
            )
            stem = titled_report_stem(markdown, kind="补充排版")
            ensure_visible_link(
                root, link_name=f"{stem}.{format_name}",
                target_relative=relative, replace_symlink=True,
            )
            result[format_name] = visible
        return result
