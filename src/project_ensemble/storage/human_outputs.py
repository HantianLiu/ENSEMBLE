from __future__ import annotations

import os
import re
from pathlib import Path


def titled_report_stem(markdown: str, *, kind: str, revised: bool = False) -> str:
    """Make a portable reader-facing name from the report's actual title."""
    match = re.search(r"^#\s+(.+?)\s*$", markdown, re.MULTILINE)
    title = match.group(1) if match else "会议成果"
    title = re.sub(r"[`*_]", "", title)
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", title)
    title = re.sub(r"\s+", " ", title).strip(" .-") or "会议成果"
    suffix = f"（{kind}{'·修订版' if revised else ''}）"
    budget = 200 - len(suffix.encode("utf-8"))
    shortened = ""
    for character in title:
        if len((shortened + character).encode("utf-8")) > budget:
            break
        shortened += character
    return (shortened.rstrip(" .-") or "会议成果") + suffix


def ensure_titled_report_links(
    meeting_root: str | Path, *, markdown_target: str | Path,
    pdf_target: str | Path | None, kind: str, revised: bool = False,
    html_target: str | Path | None = None,
) -> dict[str, Path]:
    """Expose stable title-named publication links to immutable targets."""
    root = Path(meeting_root).resolve()
    source = (root / markdown_target).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("reader-facing report Markdown is missing or escapes the meeting")
    stem = titled_report_stem(source.read_text(encoding="utf-8"), kind=kind, revised=revised)
    result: dict[str, Path] = {}
    targets = [("md", markdown_target)]
    if pdf_target is not None:
        targets.append(("pdf", pdf_target))
    if html_target is not None:
        targets.append(("html", html_target))
    for extension, target in targets:
        link, _ = ensure_visible_link(
            root, link_name=f"{stem}.{extension}", target_relative=target,
            replace_symlink=True,
        )
        result[extension] = link
    return result


def ensure_visible_link(
    meeting_root: str | Path,
    *,
    link_name: str,
    target_relative: str | Path,
    replace_symlink: bool = False,
) -> tuple[Path, bool]:
    """Create a stable, root-level Human entry point without duplicating frozen data."""

    root = Path(meeting_root).resolve()
    if Path(link_name).name != link_name:
        raise ValueError("visible output link_name must be one root-level filename")
    target_relative = Path(target_relative)
    target = (root / target_relative).resolve()
    if not target.is_relative_to(root) or not (target.is_file() or target.is_dir()):
        raise ValueError(f"visible output target is missing or escapes the meeting: {target_relative}")
    link = root / link_name
    expected = os.path.relpath(target, start=link.parent)
    if os.path.lexists(link):
        if link.is_symlink() and os.readlink(link) == expected:
            return link, False
        if not link.is_symlink() or not replace_symlink:
            raise ValueError(f"visible output entry point conflicts with frozen path: {link_name}")
        temporary = root / f".{link_name}.updating-{os.getpid()}"
        if os.path.lexists(temporary):
            raise ValueError(f"temporary visible output path already exists: {temporary.name}")
        os.symlink(expected, temporary)
        os.replace(temporary, link)
        return link, True
    os.symlink(expected, link)
    return link, True
