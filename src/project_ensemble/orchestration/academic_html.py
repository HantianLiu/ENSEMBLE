"""Reader-facing HTML from frozen Markdown; never changes substantive prose."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from pathlib import Path

from markdown_it import MarkdownIt

from project_ensemble.orchestration.math_rendering import (
    display_math_continue, display_math_start, normalize_fragmented_inline_math,
    normalize_math_operator_commands,
    repair_json_decoded_math_commands, repair_nested_display_fences,
)
from project_ensemble.orchestration.report_palette import DEFAULT_PALETTE, PALETTES


_MATHJAX = "https://cdn.jsdelivr.net/npm/mathjax@4/tex-chtml.js"
HTML_RENDERING_PROFILE = "ACADEMIC_HTML_MATHJAX_ANNOTATIONS_NOTES_V29"
_INLINE_MATH = re.compile(r"(?<![\\$])\$(?!\$)([^\n$]+?)(?<!\\)\$(?!\$)|\\\((.+?)\\\)")
_STRONG_CJK_BOUNDARY = re.compile(r"(?<=[。！？；：，、.!?;:])\*\*(?=[\u3400-\u9fffA-Za-z])")
_CODE_SPAN = re.compile(r"(`+[^`\n]*`+)")
_CITATION = re.compile(r"\[(\d+(?:\s*[,，;；–-]\s*\d+)*)\]")
_GLOSSARY_ITEM = re.compile(r"^\s*-\s+\*\*(.+?)\*\*\s*[:：]\s*(.+)$")
_REFERENCE_ITEM = re.compile(r"^\s*\[(\d+)\]\s+(.+)$")
_NUMBERED_CITATION = r"\[[0-9]+(?:\s*[,，]\s*[0-9]+)*\]"
_NUMBERED_CITATION_RUN = rf"(?:{_NUMBERED_CITATION}\s*[；;,，、]\s*)*{_NUMBERED_CITATION}"
_PARENTHESIZED_CITATION_RUN = re.compile(
    rf"(?<!\])(?:（\s*(?P<wide>{_NUMBERED_CITATION_RUN})\s*）"
    rf"|\(\s*(?P<ascii>{_NUMBERED_CITATION_RUN})\s*\))"
)
_READER_NOTE_DEFINITION = re.compile(r"^\[\^(note-[A-Za-z0-9_-]+)\]:\s*(.+?)\s*$")
_READER_NOTE_CALLOUT = re.compile(
    r"^\s*>\s*\*\*(证据边界|来源状态|实现说明|Evidence note|Source status|Implementation note)"
    r"\*\*\s*[:：]?\s*(.+?)\s*$", re.I,
)


def _extract_reader_notes(
    markdown: str, *, callout_notes: bool = False,
) -> tuple[str, dict[str, str]]:
    """Hide Markdown note definitions in HTML; preserve the frozen source file."""
    notes: dict[str, str] = {}
    kept: list[str] = []
    for line in markdown.splitlines(keepends=True):
        match = _READER_NOTE_DEFINITION.match(line.rstrip("\r\n"))
        if match:
            notes[match.group(1)] = match.group(2)
        elif callout_notes and (callout := _READER_NOTE_CALLOUT.match(line.rstrip("\r\n"))):
            note_number = len(notes) + 1
            note_id = f"note-callout-{note_number}"
            while note_id in notes:
                note_number += 1
                note_id = f"note-callout-{note_number}"
            notes[note_id] = f"{callout.group(1)}：{callout.group(2)}"
            kept.append(f"[^{note_id}]\n")
        else:
            kept.append(line)
    return "".join(kept), notes


def _number_reader_sections(markdown: str) -> str:
    """Number sections nested below numbered module headings for HTML readers."""
    chapter: int | None = None
    subsection = subsubsection = subsubsubsection = 0
    in_fence = False
    output: list[str] = []
    numbered_chapter = re.compile(r"^(\d+)[.、．]\s*(.*?)\s*$")
    existing_section = re.compile(r"^\d+(?:\.\d+){0,3}[.、．]?\s+")

    for line in markdown.splitlines(keepends=True):
        if re.match(r"^\s*(?:```|~~~)", line):
            in_fence = not in_fence
            output.append(line)
            continue
        if in_fence:
            output.append(line)
            continue

        match = re.match(r"^(#{1,6})\s+(.+?)(\s*)$", line.rstrip("\r\n"))
        if not match:
            output.append(line)
            continue
        level, label, trailing = len(match.group(1)), match.group(2).strip(), match.group(3)
        if level == 2:
            chapter = None
            subsection = subsubsection = subsubsubsection = 0
            output.append(line)
            continue
        if level == 3:
            chapter_match = numbered_chapter.match(label)
            if chapter_match:
                chapter = int(chapter_match.group(1))
                label = chapter_match.group(2)
                subsection = subsubsection = subsubsubsection = 0
                output.append(f"{'#' * level} {chapter}. {label}{trailing}\n" if line.endswith("\n")
                              else f"{'#' * level} {chapter}. {label}{trailing}")
            else:
                chapter = None
                output.append(line)
            continue
        if chapter is None or level < 4:
            output.append(line)
            continue
        if level == 4 and re.match(r"^(?:表|图|Table|Figure)\s*\d", label, re.I):
            output.append(line)
            continue

        label = existing_section.sub("", label)
        if level == 4:
            subsection += 1
            subsubsection = subsubsubsection = 0
            number = f"{chapter}.{subsection}"
        elif level == 5:
            if subsection == 0:
                subsection = 1
            subsubsection += 1
            subsubsubsection = 0
            number = f"{chapter}.{subsection}.{subsubsection}"
        else:
            if subsection == 0:
                subsection = 1
            if subsubsection == 0:
                subsubsection = 1
            subsubsubsection += 1
            number = f"{chapter}.{subsection}.{subsubsection}.{subsubsubsection}"
        rewritten = f"{'#' * level} {number} {label}{trailing}"
        output.append(rewritten + ("\n" if line.endswith("\n") else ""))
    return "".join(output)


def normalize_reader_citation_groups(text: str) -> str:
    """Remove redundant outer parentheses from citation-only groups."""
    def replace(match: re.Match[str]) -> str:
        citations = match.group("wide") or match.group("ascii")
        numbers = [number for citation in re.findall(r"\[([^]]+)\]", citations)
                   for number in re.findall(r"\d+", citation)]
        return "[" + ", ".join(dict.fromkeys(numbers)) + "]"

    return _PARENTHESIZED_CITATION_RUN.sub(replace, text)


def _safe_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip("<>.,;；。")
    if not candidate.startswith(("https://", "http://")):
        return None
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    return candidate


def _reader_cards(
    markdown: str, reference_links: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, str]]]:
    """Extract only reader-visible glossary and reference text, not internal packets."""
    glossary: dict[str, str] = {}
    references: dict[str, str] = {}
    links: dict[str, dict[str, str]] = {}
    section = ""
    for line in markdown.splitlines():
        heading = re.match(r"^##\s+(?:\d+[.、．]?\s*)?(.+?)\s*$", line)
        if heading:
            label = heading.group(1).strip().casefold()
            section = "glossary" if label in {"术语表", "glossary", "glossaire"} else (
                "references" if label in {"参考文献", "references", "références"} else ""
            )
            continue
        if section == "glossary" and (match := _GLOSSARY_ITEM.match(line)):
            glossary[match.group(1).strip()] = match.group(2).strip()
        elif section == "references" and (match := _REFERENCE_ITEM.match(line)):
            references[match.group(1)] = match.group(2).strip()
            reference = references[match.group(1)]
            doi = re.search(r"\b(?:doi:\s*)?(10\.\d{4,9}/[^\s]+)", reference, re.I)
            raw_url = re.search(r"https?://[^\s<>]+", reference)
            target = f"https://doi.org/{doi.group(1).rstrip('.,;。')}" if doi else (
                _safe_http_url(raw_url.group(0)) if raw_url else None
            )
            target = _safe_http_url(target)
            if target:
                links[match.group(1)] = {"source": target}
    for number, values in (reference_links or {}).items():
        if not isinstance(values, dict):
            continue
        safe_values = {
            key: url for key, value in values.items()
            if key in {"source", "pdf"} and (url := _safe_http_url(value))
        }
        if safe_values:
            links.setdefault(str(number), {}).update(safe_values)
    return glossary, references, links


def _protect_math(markdown: str) -> tuple[str, dict[str, str]]:
    """Keep TeX intact while Markdown parses the surrounding document."""
    replacements: dict[str, str] = {}
    nonce = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]

    def token(expression: str, *, display: bool) -> str:
        key = f"ENSEMBLEMATH{nonce}{len(replacements):08d}TOKEN"
        escaped = html.escape(expression, quote=True)
        tag = "div" if display else "span"
        kind = "display" if display else "inline"
        delimiters = (r"\[", r"\]") if display else (r"\(", r"\)")
        replacements[key] = (
            f'<{tag} class="math {kind}" data-tex="{escaped}">'
            f'{delimiters[0]}{escaped}{delimiters[1]}</{tag}>'
        )
        return key

    output: list[str] = []
    lines = markdown.splitlines(keepends=True)
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if re.match(r"^\s*(?:```|~~~)", line):
            in_fence = not in_fence
            output.append(line)
            index += 1
            continue
        display = display_math_start(line) if not in_fence else None
        if display:
            closing, initial, closed = display
            if closed:
                output.append("\n" + token(initial, display=True) + "\n\n")
                index += 1
                continue
            collected: list[str] = [initial] if initial else []
            cursor = index + 1
            while cursor < len(lines):
                content, closed = display_math_continue(lines[cursor], closing)
                if content:
                    collected.append(content)
                if closed:
                    break
                cursor += 1
            if closed:
                output.append("\n" + token("\n".join(collected), display=True) + "\n\n")
                index = cursor + 1
                continue
        if in_fence:
            output.append(line)
        else:
            fragments: list[str] = []
            for segment in _CODE_SPAN.split(line):
                if segment.startswith("`") and segment.endswith("`"):
                    fragments.append(segment)
                    continue
                # CommonMark does not close a strong span after Chinese
                # punctuation when the next Han or Latin letter is adjacent.
                # An HTML entity gives the parser a zero-width boundary;
                # the frozen words and visible punctuation stay unchanged.
                segment = _STRONG_CJK_BOUNDARY.sub("**&#8203;", segment)
                fragments.append(_INLINE_MATH.sub(
                    lambda match: token(match.group(1) or match.group(2), display=False),
                    segment,
                ))
            output.append("".join(fragments))
        index += 1
    return "".join(output), replacements


def render_academic_review_html(
    markdown: str, *, meeting_id: str, language: str | None = None,
    palette: str = DEFAULT_PALETTE,
    reference_links: dict[str, dict[str, str]] | None = None,
    figure_assets: dict[str, bytes] | None = None,
) -> str:
    """Build a navigable report; MathJax typesets TeX in the browser."""
    if palette not in PALETTES:
        raise ValueError("unknown report palette")
    colors = PALETTES[palette]
    markdown = normalize_math_operator_commands(
        normalize_fragmented_inline_math(repair_json_decoded_math_commands(
            repair_nested_display_fences(markdown)
        ))
    )
    markdown = normalize_reader_citation_groups(markdown)
    reader_markdown, notes = _extract_reader_notes(markdown, callout_notes=True)
    reader_markdown = _number_reader_sections(reader_markdown)
    protected, replacements = _protect_math(reader_markdown)
    note_replacements: dict[str, str] = {}
    nonce = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]
    for number, note_id in enumerate(notes):
        token = f"ENSEMBLENOTE{nonce}{number:08d}TOKEN"
        protected = protected.replace(f"[^{note_id}]", token)
        note_replacements[token] = (
            f'<button type="button" class="note-trigger" data-note="{html.escape(note_id, quote=True)}" '
            f'aria-label="查看说明"><small>[注]</small></button>'
        )
    parser = MarkdownIt("commonmark", {"html": False, "breaks": False}).enable("table")
    glossary, references, reference_links = _reader_cards(markdown, reference_links)
    glossary_terms = sorted(glossary, key=len, reverse=True)
    glossary_pattern = re.compile("|".join(re.escape(term) for term in glossary_terms)) if glossary_terms else None

    def reader_text(_renderer, tokens, index, _options, _env):
        value = tokens[index].content
        if not _CITATION.search(value) and (not glossary_pattern or not glossary_pattern.search(value)):
            return html.escape(value)
        fragments: list[str] = []
        cursor = 0
        pattern = re.compile(
            _CITATION.pattern + ("|(?P<term>" + glossary_pattern.pattern + ")" if glossary_pattern else "")
        )
        for match in pattern.finditer(value):
            fragments.append(html.escape(value[cursor:match.start()]))
            if match.group(1):
                numbers: list[str] = []
                for part in re.split(r"[,，;；]", match.group(1)):
                    span = re.fullmatch(r"\s*(\d+)\s*[–-]\s*(\d+)\s*", part)
                    if span and 0 < int(span.group(2)) - int(span.group(1)) <= 49:
                        numbers.extend(str(number) for number in range(
                            int(span.group(1)), int(span.group(2)) + 1,
                        ))
                    else:
                        numbers.extend(re.findall(r"\d+", part))
                keys = ",".join(dict.fromkeys(numbers))
                fragments.append(
                    f'<button type="button" class="citation-trigger" data-references="{keys}" '
                    f'aria-label="查看引文 {html.escape(match.group(0), quote=True)}">'
                    f'<sup>{html.escape(match.group(0))}</sup></button>'
                )
            else:
                term = match.group("term")
                fragments.append(
                    f'<button type="button" class="term-trigger" data-term="{html.escape(term, quote=True)}">'
                    f'{html.escape(term)}</button>'
                )
            cursor = match.end()
        fragments.append(html.escape(value[cursor:]))
        return "".join(fragments)

    parser.add_render_rule("text", reader_text)
    original_image_rule = parser.renderer.rules["image"]
    def generated_image(_renderer, tokens, index, _options, _env):
        import base64
        token = tokens[index]
        source = token.attrGet("src") or ""
        if source.startswith("figures/generated-"):
            data = (figure_assets or {}).get(source)
            alt_text = token.content
            for key, rendered in replacements.items():
                if key in alt_text:
                    expression = re.search(r'data-tex="([^"]*)"', rendered)
                    if expression:
                        alt_text = alt_text.replace(key, r"\(" + html.unescape(expression[1]) + r"\)")
            for key in note_replacements:
                alt_text = alt_text.replace(key, "[注]")
            alt = html.escape(alt_text, quote=True)
            if data is None:
                return f'<span class="figure-fallback">{alt}</span>'
            encoded = base64.b64encode(data).decode("ascii")
            return (f'<img class="ensemble-figure" src="data:image/png;base64,{encoded}" '
                    f'alt="{alt}" style="display:block;max-width:100%;height:auto;margin:1em auto" loading="lazy">')
        return original_image_rule(tokens, index, _options, _env)
    parser.add_render_rule("image", generated_image)
    tokens = parser.parse(protected)
    toc: list[tuple[int, str, str]] = []
    heading_count = 0
    for index, item in enumerate(tokens):
        if item.type != "heading_open":
            continue
        heading_count += 1
        anchor = f"section-{heading_count}"
        item.attrSet("id", anchor)
        level = int(item.tag[1:])
        label = tokens[index + 1].content if index + 1 < len(tokens) else ""
        toc.append((level, anchor, label))
        if level == 3 and re.match(r"^\d+[.、．]\s*\S", label):
            item.attrSet("class", "module-heading")
    sections: list[tuple[str, list]] = []
    current_tokens: list = []
    current_class = "front-section"
    for index, item in enumerate(tokens):
        if item.type == "heading_open" and item.tag == "h2":
            if current_tokens:
                sections.append((current_class, current_tokens))
            current_tokens = []
            label = tokens[index + 1].content.strip() if index + 1 < len(tokens) else ""
            label = re.sub(r"^\d+[.、．]?\s*", "", label).casefold()
            current_class = (
                "abstract-section" if label in {"摘要", "abstract", "résumé"}
                else "report-section"
            )
        current_tokens.append(item)
    if current_tokens:
        sections.append((current_class, current_tokens))
    fragments = []
    for css_class, section_tokens in sections:
        if css_class == "report-section" and any(
            item.type == "heading_open" and item.attrGet("class") == "module-heading"
            for item in section_tokens
        ):
            css_class += " module-section"
        fragments.append(
            f'<section class="{css_class}">'
            + parser.renderer.render(section_tokens, parser.options, {})
            + "</section>"
        )
    body = "\n".join(fragments)
    for key, rendered in replacements.items():
        if rendered.startswith('<div class="math display"'):
            body = body.replace(f"<p>{key}</p>", rendered)
        body = body.replace(key, rendered)
    for key, rendered in note_replacements.items():
        body = body.replace(key, rendered)
    title = next((label for level, _, label in toc if level == 1), "Literature review")
    navigation = "\n".join(
        f'<li class="level-{level}"><a href="#{anchor}">{html.escape(label)}</a></li>'
        for level, anchor, label in toc if level <= 3
    )
    mathjax_url = os.environ.get("ENSEMBLE_MATHJAX_URL", _MATHJAX)
    if not mathjax_url.startswith(("https://", "./", "../")) or '"' in mathjax_url:
        raise ValueError("ENSEMBLE_MATHJAX_URL must be HTTPS or a relative asset path")
    mathjax_config = json.dumps({
        "tex": {"inlineMath": [[r"\(", r"\)"]], "displayMath": [[r"\[", r"\]"]]},
        "options": {"enableMenu": False},
    }, ensure_ascii=False)
    cards = json.dumps({"glossary": glossary, "references": references, "referenceLinks": reference_links, "notes": notes}, ensure_ascii=False).replace("<", "\\u003c")
    assets = Path(__file__).resolve().parents[1] / "assets"
    annotation_css = (assets / "reader_annotations.css").read_text(encoding="utf-8")
    annotation_js = (assets / "reader_annotations.js").read_text(encoding="utf-8")
    markdown_js = (assets / "reader_markdown.js").read_text(encoding="utf-8")
    navigation_js = (assets / "reader_navigation.js").read_text(encoding="utf-8")
    focus_js = (assets / "reader_focus.js").read_text(encoding="utf-8")
    qa_data = json.dumps({"title": title, "markdown": markdown}, ensure_ascii=False)
    qa_data = qa_data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    qa_document = (assets / "reader_qa.html").read_text(encoding="utf-8").replace(
        "ENSEMBLE_QA_REPORT_DATA", qa_data,
    )
    qa_document = qa_document.replace("<!-- ENSEMBLE_READER_MARKDOWN -->", "<script>" + markdown_js + "</script>")
    qa_srcdoc = html.escape(qa_document, quote=True)
    annotation_key = "ensemble-reader:" + hashlib.sha256(
        (meeting_id + "\n" + markdown).encode("utf-8")
    ).hexdigest()
    document_language = language if language in {"zh", "en", "fr"} else (
        "zh" if re.search(r"[\u3400-\u9fff]", markdown) else "en"
    )
    return f'''<!doctype html>
<html lang="{document_language}" data-annotation-key="{annotation_key}" data-meeting-id="{html.escape(meeting_id, quote=True)}">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: light; --ink:{colors['ink']}; --muted:{colors['mid']}; --line:{colors['line']}; --paper:#fff; --accent:{colors['accent']}; --pale:{colors['pale']}; --reader-shell-max:1320px; --reader-shell-padding:28px; --reader-nav-column:250px; --reader-shell-gap:28px; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:#fff; color:var(--ink); font:16px/1.8 system-ui,"Noto Sans CJK SC",sans-serif; }}
.shell {{ max-width:var(--reader-shell-max); margin:auto; display:grid; grid-template-columns:var(--reader-nav-column) minmax(0,1fr); gap:var(--reader-shell-gap); padding:var(--reader-shell-padding); }}
nav {{ position:sticky; top:20px; align-self:start; max-height:calc(100vh - 40px); min-height:0; display:flex; flex-direction:column; overflow:hidden; font-size:.88rem; }}
nav .toc-scroll {{ min-height:0; overflow:auto; flex:1; }}
nav ul {{ padding:0; list-style:none; }} nav li {{ margin:.25em 0; }} nav .level-3 {{ padding-left:1em; }}
nav a {{ color:var(--muted); text-decoration:none; }} nav a:hover {{ color:var(--accent); text-decoration:underline; }}
main {{ min-width:0; background:var(--paper); padding:clamp(24px,5vw,76px); border-radius:4px; box-shadow:0 6px 26px #0000001a,0 1px 6px #0000000d; }}
article {{ max-width:82ch; margin:auto; }} h1,h2,h3,h4,h5,h6 {{ line-height:1.35; break-after:avoid; scroll-margin-top:24px; }}
h1 {{ font-size:2.15rem; }} h2 {{ margin-top:2.5em; padding-top:.8em; border-top:1px solid var(--line); font-size:1.7rem; }}
h3 {{ margin-top:2em; font-size:1.45rem; }} h4 {{ margin-top:1.8em; font-size:1.3rem; }}
h5 {{ margin-top:1.55em; font-size:1.16rem; }} h6 {{ margin-top:1.4em; font-size:1.05rem; }}
p {{ margin:1em 0; }} blockquote {{ margin:1.5em 0; padding:.5em 1.3em; background:var(--pale); border-left:4px solid {colors['mid']}; }}
.abstract-section {{ margin:2em 0 2.8em; padding:1.2em 1.6em; border:1px solid var(--line); border-left:5px solid var(--accent); border-radius:8px; background:var(--pale); }}
.abstract-section h2 {{ margin:0 0 .6em; padding:0; border:0; font-size:1.22rem; }}
.module-section > h2 {{ color:var(--accent); border-top:2px solid var(--line); }}
.module-heading {{ margin:2.5em 0 1.15em; padding:.65em .9em; color:var(--accent); background:var(--pale); border-left:5px solid var(--accent); border-radius:0 6px 6px 0; font-size:1.55rem; }}
.module-heading + p {{ margin-top:1.1em; }}
article strong, article b {{ font-weight:700; color:var(--accent); background:linear-gradient(transparent 58%, var(--pale) 58%); }}
table {{ border-collapse:collapse; display:block; overflow-x:auto; width:100%; font-size:.92rem; }} th,td {{ border:1px solid var(--line); padding:.6em .75em; vertical-align:top; min-width:7em; }} th {{ background:var(--pale); }}
pre {{ overflow-x:auto; padding:1em; background:#f3f6f8; }} .math.display {{ overflow-x:auto; text-align:center; margin:1.3em 0; }}
.math {{ font-family:"STIX Two Math","Cambria Math",serif; }} .meta {{ color:var(--muted); font-size:.8rem; }}
.citation-trigger,.term-trigger,.note-trigger {{ border:0; padding:0; color:var(--accent); background:none; font:inherit; cursor:pointer; }}
.citation-trigger:hover,.term-trigger:hover,.note-trigger:hover {{ text-decoration:underline; }} .citation-trigger sup {{ font-size:.72em; vertical-align:super; }}
.note-trigger small {{ font-size:.72em; color:var(--muted); }}
.drawer {{ position:fixed; z-index:10; inset:0 0 0 auto; width:min(420px,100vw); background:white; box-shadow:-8px 0 30px #1b374344; padding:28px; overflow:auto; }}
.drawer[hidden] {{ display:none; }}
.drawer-controls {{ display:flex; justify-content:space-between; gap:1em; }}
.drawer-controls button {{ border:0; background:var(--pale); color:var(--accent); padding:.3em .7em; border-radius:4px; cursor:pointer; }}
.drawer-controls button[hidden] {{ display:none; }}
.drawer-body {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
.citation-reference {{ margin:0 0 1.4em; padding:0 0 1em; border-bottom:1px solid var(--line); }}
.citation-reference p {{ margin:.5em 0; }}
.citation-source-links {{ display:flex; flex-wrap:wrap; gap:.7em; font-size:.9rem; }}
.citation-source-links a {{ color:var(--accent); }}
@media(max-width:800px) {{ .shell {{ display:block; padding:0; }} nav {{ position:static; display:block; max-height:none; padding:1em; }} nav .toc-scroll {{ max-height:35vh; }} main {{ margin:12px 8px; padding-bottom:11em; }} }}
@media print {{ body {{ background:white; }} .shell {{ display:block; padding:0; }} nav {{ display:none; }} main {{ box-shadow:none; border-radius:0; margin:0; padding:0; }} h2 {{ break-before:page; }} a {{ color:inherit; }} }}
{annotation_css}
</style>
<script>window.MathJax = {mathjax_config};</script>
<script defer src="{html.escape(mathjax_url, quote=True)}"></script>
</head>
<body><div class="shell"><nav aria-label="目录">
<div class="reader-nav-tabs" role="tablist" aria-label="阅读导航"><button type="button" id="nav-toc" role="tab" aria-controls="toc-panel" aria-selected="true">目录</button><button type="button" id="nav-annotations" role="tab" aria-controls="annotation-tools" aria-selected="false" tabindex="-1">批注与问答</button></div>
<div class="reader-appearance" role="group" aria-label="阅读外观"><button type="button" id="reader-focus-toggle" aria-pressed="false" title="指向标注或批注临时聚焦；单击锁定，Esc 解除">聚焦模式：关</button><label for="reader-palette">配色</label><select id="reader-palette"><option value="white">原有白色</option><option value="butter">浅黄 · 奶油纸</option></select><span id="reader-focus-status" role="status" aria-live="polite"></span></div>
<div class="reader-search"><label for="reader-search">搜索正文</label><input id="reader-search" type="search" placeholder="搜索文字…"><div><button type="button" id="search-prev" aria-label="上一个结果">↑</button><button type="button" id="search-next" aria-label="下一个结果">↓</button><button type="button" id="search-clear">清除</button><span id="reader-search-status" role="status" aria-live="polite"></span></div></div>
<div class="toc-scroll" id="toc-panel" role="tabpanel" aria-labelledby="nav-toc"><ul>{navigation}</ul></div><section class="annotation-tools" id="annotation-tools" role="tabpanel" aria-labelledby="nav-annotations" hidden><div class="annotation-list" id="annotation-list"></div><button type="button" id="annotation-collapse">返回目录</button><button type="button" id="annotation-save-html">保存带批注和问答的 HTML</button><button type="button" id="annotation-export-data">导出批注数据</button><button type="button" id="annotation-import-data">导入数据／旧副本</button><input type="file" id="annotation-import-file" accept=".json,.html,application/json,text/html" hidden><span class="annotation-import-status" id="annotation-import-status" role="status" aria-live="polite"></span><button type="button" id="qa-open-general">问 AI</button></section><span class="annotation-status" id="annotation-status" role="status" aria-live="polite"></span></nav>
<main><article>{body}</article><p class="meta">Project ENSEMBLE · {html.escape(meeting_id)}</p></main></div>
<script type="application/json" id="embedded-annotations">[]</script>
<script type="application/json" id="embedded-qa-history">[]</script>
<div id="annotation-floats" aria-label="已保存的批注"></div>
<div id="reader-position-rail" aria-label="文档位置与搜索结果"><div id="reader-position-viewport"></div><div id="reader-position-ticks"></div></div>
<div id="reader-star-rail" role="group" aria-label="重要批注位置"></div>
<div id="selection-menu" class="selection-menu" role="toolbar" aria-label="选中文字操作" hidden><button type="button" id="annotation-highlight" title="Alt+Shift+H">高亮</button><button type="button" id="annotation-create" title="Alt+Shift+N">批注</button><button type="button" id="qa-open" title="Alt+Shift+A">问 AI</button><button type="button" id="selection-copy">复制</button></div>
<aside class="drawer" id="reader-drawer" aria-label="术语与引文详情" hidden><div class="drawer-controls"><button type="button" id="drawer-back" aria-label="返回上一条详情" hidden>← 返回</button><button type="button" id="drawer-close" aria-label="关闭所有侧栏">关闭 ×</button></div><h2 id="drawer-title"></h2><div class="drawer-body" id="drawer-body"></div></aside>
<aside class="annotation-panel" id="annotation-panel" aria-label="读者批注" hidden><div class="annotation-panel-header"><h2 id="annotation-title">批注</h2><button type="button" id="annotation-close">收起 ×</button></div><div class="annotation-detail" id="annotation-detail" hidden><blockquote id="annotation-quote" hidden></blockquote><div id="annotation-editor" hidden><label for="annotation-note">批注（可使用 $...$ 或 $$...$$ 写公式）</label><textarea id="annotation-note" maxlength="4000"></textarea></div><p class="annotation-empty-message" id="annotation-empty-message" hidden>尚无批注；输入内容后保存。</p><div class="annotation-preview" id="annotation-preview" aria-label="渲染后的批注" title="双击编辑批注" tabindex="0"></div><div class="annotation-detail-actions"><button type="button" id="annotation-save" hidden>保存批注</button><button type="button" id="annotation-edit">编辑</button><button type="button" id="annotation-cancel" hidden>取消编辑</button><button type="button" id="annotation-star" class="annotation-star-toggle" aria-label="加星，标为重要批注" aria-pressed="false" title="加星，标为重要批注" hidden>☆</button><button type="button" id="annotation-ask-ai">就这条高亮问 AI</button><button type="button" id="annotation-delete">删除标注</button></div></div></aside>
<aside class="qa-panel" id="qa-panel" aria-label="报告快速问答" hidden><button type="button" id="qa-close" aria-label="收起问答，保留本页密钥">收起问答 ×</button><iframe id="qa-frame" title="报告快速问答 · DeepSeek" sandbox="allow-scripts" referrerpolicy="no-referrer" data-srcdoc="{qa_srcdoc}"></iframe></aside>
<aside class="reader-entry-panel" id="reader-entry-panel" aria-label="批注与问答全文" hidden><div class="annotation-panel-header"><h2 id="reader-entry-title">完整内容</h2><button type="button" id="reader-entry-star" class="annotation-star-toggle" aria-label="加星，标为重要批注" aria-pressed="false" title="加星，标为重要批注" hidden>☆</button><button type="button" id="reader-entry-close">收起 ×</button></div><div class="annotation-preview" id="reader-entry-body"></div><button type="button" id="reader-entry-continue" hidden>继续问 AI</button></aside>
<script type="application/json" id="reader-cards">{cards}</script>
<script>
const cards = JSON.parse(document.getElementById('reader-cards').textContent);
const drawer = document.getElementById('reader-drawer');
const drawerBody = document.getElementById('drawer-body');
const drawerBack = document.getElementById('drawer-back');
const readerStack = [];
const glossaryTerms = Object.keys(cards.glossary).filter(term => term.length >= 2).sort((a, b) => b.length - a.length);
function referenceNumbers(raw) {{
  const numbers = [];
  for (const part of raw.split(/[,，;；]/)) {{
    const range = /^\\s*(\\d+)\\s*[–-]\\s*(\\d+)\\s*$/.exec(part);
    if (range && Number(range[2]) >= Number(range[1]) && Number(range[2]) - Number(range[1]) <= 49) {{
      for (let number = Number(range[1]); number <= Number(range[2]); number++) numbers.push(String(number));
    }} else {{
      numbers.push(...(part.match(/\\d+/g) || []));
    }}
  }}
  return [...new Set(numbers)];
}}
function appendLinkedText(container, value, currentTerm) {{
  let plain = '';
  function flush() {{
    if (plain) container.appendChild(document.createTextNode(plain));
    plain = '';
  }}
  for (let index = 0; index < value.length;) {{
    const citation = /^\\[(\\d+(?:\\s*[,，;；–-]\\s*\\d+)*)\\]/.exec(value.slice(index));
    if (citation) {{
      flush();
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'citation-trigger';
      button.dataset.references = referenceNumbers(citation[1]).join(',');
      button.textContent = citation[0];
      container.appendChild(button);
      index += citation[0].length;
      continue;
    }}
    const term = glossaryTerms.find(candidate => candidate !== currentTerm && value.startsWith(candidate, index));
    if (term) {{
      flush();
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'term-trigger';
      button.dataset.term = term;
      button.textContent = term;
      container.appendChild(button);
      index += term.length;
      continue;
    }}
    plain += value[index];
    index++;
  }}
  flush();
}}
function renderReaderCard() {{
  const card = readerStack[readerStack.length - 1];
  if (!card) {{ drawer.hidden = true; return; }}
  const key = card.key;
  const citationRefs = card.kind === 'citation' ? referenceNumbers(key) : [];
  const value = card.kind === 'term' ? cards.glossary[key] : card.kind === 'note' ? cards.notes[key] : '';
  document.getElementById('drawer-title').textContent = card.kind === 'term' ? key : card.kind === 'note' ? '说明' : '引文 ' + key;
  drawerBody.replaceChildren();
  if (card.kind === 'citation') {{
    for (const number of citationRefs) {{
      const entry = document.createElement('section');
      entry.className = 'citation-reference';
      const paragraph = document.createElement('p');
      appendLinkedText(paragraph, '[' + number + '] ' + (cards.references[number] || '当前报告没有对应的参考文献条目'), null);
      entry.appendChild(paragraph);
      const links = cards.referenceLinks[number] || {{}};
      const actions = document.createElement('div');
      actions.className = 'citation-source-links';
      for (const [field, label] of [['pdf', '打开原文 PDF ↗'], ['source', '打开来源网页 ↗']]) {{
        const href = links[field];
        if (!href || (field === 'source' && href === links.pdf)) continue;
        const anchor = document.createElement('a');
        anchor.href = href;
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
        anchor.textContent = label;
        actions.appendChild(anchor);
      }}
      if (actions.childElementCount) entry.appendChild(actions);
      drawerBody.appendChild(entry);
    }}
    if (!citationRefs.length) drawerBody.textContent = '当前报告没有可展示的对应条目；请核对参考文献与证据包。';
  }} else {{
    const paragraph = document.createElement('p');
    appendLinkedText(paragraph, value || '当前报告没有可展示的对应条目；请核对参考文献与证据包。', card.kind === 'term' ? key : null);
    drawerBody.appendChild(paragraph);
  }}
  drawerBack.hidden = readerStack.length < 2;
  document.dispatchEvent(new Event('ensemble-reader-card-open'));
  drawer.hidden = false;
  if (window.MathJax && window.MathJax.typesetPromise) window.MathJax.typesetPromise([drawer]);
}}
function closeReaderCards() {{ readerStack.length = 0; drawer.hidden = true; }}
document.addEventListener('click', event => {{
  const term = event.target.closest('.term-trigger');
  const citation = event.target.closest('.citation-trigger');
  const note = event.target.closest('.note-trigger');
  if (term || citation || note) {{
    const card = term ? {{kind: 'term', key: term.dataset.term}} : note ? {{kind: 'note', key: note.dataset.note}} : {{kind: 'citation', key: citation.dataset.references}};
    const current = readerStack[readerStack.length - 1];
    if (!current || current.kind !== card.kind || current.key !== card.key) {{
      if (readerStack.length >= 20) readerStack.shift();
      readerStack.push(card);
    }}
    renderReaderCard();
    return;
  }}
  if (event.target.closest('main') && !drawer.hidden) closeReaderCards();
}});
drawerBack.addEventListener('click', () => {{ readerStack.pop(); renderReaderCard(); }});
document.getElementById('drawer-close').addEventListener('click', closeReaderCards);
document.addEventListener('keydown', event => {{ if (event.key === 'Escape') closeReaderCards(); }});
</script><script>{markdown_js}</script><script>{navigation_js}</script><script>{annotation_js}</script><script>{focus_js}</script></body></html>'''
