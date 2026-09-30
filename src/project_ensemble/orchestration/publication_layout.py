"""Presentation-only Markdown conversion; never mutates frozen meeting prose."""

from __future__ import annotations

import re

from project_ensemble.orchestration.math_rendering import (
    INLINE_MATH_TOKEN, display_math_continue, display_math_start,
)


_CITATION_LIST = re.compile(r"\[\s*\d+(?:\s*,\s*\d+)+\s*\]")
_INLINE = re.compile(
    r"(?P<math>" + INLINE_MATH_TOKEN + r")"
    r"|\[(?P<link_label>[^\]]+)\]\((?P<link_url>https?://[^\s)]+)\)"
    r"|(?P<url>https?://[^\s<>()\[\]{}]+)"
    r"|\*\*(?P<strong>.+?)\*\*"
    r"|(?<!\*)\*(?P<em>[^*\n]+)\*(?!\*)"
    r"|`(?P<code>[^`\n]+)`"
    r"|<br\s*/?>"
    r"|(?P<citation>\[\s*\d+(?:\s*,\s*\d+)+\s*\])",
    re.IGNORECASE,
)
_HEADING_NUMBER = re.compile(
    r"^(?:(?:\d{1,2}(?:\.\d{1,2}){0,3})[.、．]?\s+"
    r"|[一二三四五六七八九十百]+[、．.]\s*"
    r"|[（(][一二三四五六七八九十百]+[）)]\s*"
    r"|第[一二三四五六七八九十百]+[章节]\s*)"
)
_INLINE_ENUMERATION = re.compile(r"[（(]([1-9]\d*)[）)]")
_INTERNAL_MODULE = re.compile(r"(?i)\bRM[-_ ]?(\d{2})(?:[-_ ]?Q\d+|\s+INF[-_ ]?\d+)?\b")
_OTHER_INTERNAL = re.compile(r"(?i)\b(?:RD|SR)[-_ ]?\d{2,4}\b|\bINF[-_ ]?\d{2,4}\b|\brecord_id\b")


def compact_citation_list(value: str) -> str:
    """Compress only adjacent increasing runs; never drop or reorder source IDs."""
    numbers = [int(part.strip()) for part in value.strip("[]").split(",")]
    groups: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:] + [None]:
        if number is not None and number == previous + 1:
            previous = number
            continue
        groups.append(f"{start}–{previous}" if previous - start >= 2 else
                      f"{start}, {previous}" if previous != start else str(start))
        if number is not None:
            start = previous = number
    return "[" + ", ".join(groups) + "]"


def compact_numeric_citations(text: str) -> str:
    return _CITATION_LIST.sub(lambda match: compact_citation_list(match.group()), text)


def sort_numeric_citations(markdown: str) -> str:
    """Sort numeric citation groups without changing their IDs or multiplicity."""
    in_fence = False
    result: list[str] = []
    for line in markdown.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            result.append(line)
            continue
        if in_fence:
            result.append(line)
            continue
        result.append(_CITATION_LIST.sub(
            lambda match: "[" + ", ".join(str(number) for number in sorted(
                int(part.strip()) for part in match.group()[1:-1].split(",")
            )) + "]", line
        ))
    return "\n".join(result) + ("\n" if markdown.endswith("\n") else "")


def split_inline_enumerations(markdown: str) -> str:
    """Expand clear 1/2/... prose enumerations into readable Markdown lists."""
    in_fence = False
    result: list[str] = []
    for line in markdown.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            result.append(line)
            continue
        if (in_fence or not line.strip() or line.lstrip().startswith(("#", "|", ">", "- ", "* "))
                or re.match(r"^\s*\d+[.)]\s+", line)):
            result.append(line)
            continue
        markers = list(_INLINE_ENUMERATION.finditer(line))
        if (len(markers) < 2
                or [int(marker.group(1)) for marker in markers] != list(range(1, len(markers) + 1))):
            result.append(line)
            continue
        introduction = line[:markers[0].start()].rstrip()
        if introduction and not introduction.endswith(("：", ":", "。", ".")):
            result.append(line)
            continue
        items = [line[marker.end():markers[index + 1].start() if index + 1 < len(markers) else len(line)].strip()
                 for index, marker in enumerate(markers)]
        if any(len(item) < 8 for item in items):
            result.append(line)
            continue
        if introduction:
            result.extend([introduction, ""])
        result.extend(f"{index}. {item}" for index, item in enumerate(items, start=1))
        result.append("")
    return "\n".join(result).rstrip("\n") + ("\n" if markdown.endswith("\n") else "")


def replace_internal_references(
    markdown: str, module_chapters: dict[str, int], *, language: str = "zh"
) -> str:
    """Replace presentation-only tracking IDs with reader-facing navigation."""
    labels = {
        "zh": ("第{number}章", "相关章节", "相关研究记录", "核验记录", "记录编号"),
        "en": ("Chapter {number}", "related chapter", "research record", "verification record", "record number"),
        "fr": ("chapitre {number}", "chapitre connexe", "dossier de recherche", "dossier de vérification", "numéro de dossier"),
    }
    chapter_label, unknown_chapter, research_record, verification_record, record_number = labels.get(language, labels["en"])

    def module_label(match: re.Match[str]) -> str:
        chapter = module_chapters.get(f"RM-{match.group(1)}")
        label = chapter_label.format(number=chapter) if chapter else unknown_chapter
        if re.search(r"(?i)Q\d+", match.group()):
            return label + {"zh": "的研究问题", "en": " research question", "fr": " question de recherche"}.get(language, " research question")
        if re.search(r"(?i)INF[-_ ]?\d+", match.group()):
            return label + {"zh": "的核验记录", "en": " verification record", "fr": " dossier de vérification"}.get(language, " verification record")
        return label

    text = _INTERNAL_MODULE.sub(module_label, markdown)
    return _OTHER_INTERNAL.sub(
        lambda match: record_number if match.group().lower() == "record_id"
        else verification_record if match.group().upper().startswith("INF")
        else research_record if match.group().upper().startswith("RD")
        else unknown_chapter,
        text,
    )


def convert_intro_headings(markdown: str) -> str:
    """Turn a standalone short abstract/introduction heading into a callout."""
    lines = markdown.splitlines()
    output: list[str] = []
    index = 0
    in_fence = False
    intro_labels = {"摘要", "本节摘要", "章节摘要", "导言", "引言", "概述", "导读", "导读与章节摘要"}
    while index < len(lines):
        line = lines[index]
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
        heading = re.match(r"^#{3,6}\s+(.+?)\s*$", line) if not in_fence else None
        title = _HEADING_NUMBER.sub("", heading.group(1)).strip() if heading else ""
        if title not in intro_labels:
            output.append(line)
            index += 1
            continue
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines) or re.match(r"^(?:#{1,6}\s+|```|~~~|\||\d+[.)]\s+)", lines[next_index]):
            output.append(line)
            index += 1
            continue
        is_quote = lines[next_index].lstrip().startswith(">")
        passage: list[str] = []
        while next_index < len(lines) and lines[next_index].strip():
            current = lines[next_index]
            if is_quote != current.lstrip().startswith(">"):
                break
            passage.append(current.lstrip()[1:].lstrip() if is_quote else current)
            next_index += 1
        output.extend(["> **章节导读**", ">"])
        output.extend("> " + part for part in passage)
        output.append("")
        index = next_index
    return "\n".join(output) + ("\n" if markdown.endswith("\n") else "")


def normalize_publication_headings(markdown: str) -> str:
    """Number article headings consistently without changing section prose."""
    chapter = subsection = subsubsection = 0
    in_fence = in_contents = False
    result: list[str] = []
    for line in markdown.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
        if in_fence:
            result.append(line)
            continue
        match = re.match(r"^(#{2,6})\s+(.+)$", line)
        if not match:
            result.append(line)
            continue
        level, label = len(match.group(1)), match.group(2).strip()
        if level == 2 and label in {"目录", "Contents", "Sommaire"}:
            in_contents = True
            result.append(line)
            continue
        if level == 2:
            in_contents = False
            chapter += 1
            subsection = subsubsection = 0
            label = _HEADING_NUMBER.sub("", label)
            result.append(f"## {chapter}. {label}")
        elif in_contents or not chapter:
            result.append(line)
        elif level == 3:
            subsection += 1
            subsubsection = 0
            label = _HEADING_NUMBER.sub("", label)
            result.append(f"### {chapter}.{subsection} {label}")
        elif level == 4 and subsection and not re.match(r"^(?:表|图|Table|Figure)\s*\d", label, re.I):
            subsubsection += 1
            label = _HEADING_NUMBER.sub("", label)
            result.append(f"#### {chapter}.{subsection}.{subsubsection} {label}")
        else:
            result.append(f"{'#' * level} {_HEADING_NUMBER.sub('', label)}")
    return "\n".join(result) + ("\n" if markdown.endswith("\n") else "")


def heading_numbering_issues(markdown: str) -> list[str]:
    """Check the reader-facing chapter and subsection sequence after assembly."""

    chapter = subsection = subsubsection = 0
    in_fence = in_contents = False
    issues: list[str] = []
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
        if in_fence:
            continue
        match = re.match(r"^(#{2,4})\s+(.+)$", line)
        if not match:
            continue
        level, label = len(match.group(1)), match.group(2).strip()
        if level == 2 and label in {"目录", "Contents", "Sommaire"}:
            in_contents = True
            continue
        if level == 2:
            in_contents = False
            chapter += 1
            subsection = subsubsection = 0
            expected = f"{chapter}. "
        elif in_contents or chapter == 0:
            continue
        elif level == 3:
            subsection += 1
            subsubsection = 0
            expected = f"{chapter}.{subsection} "
        elif subsection and not re.match(r"^(?:表|图|Table|Figure)\s*\d", label, re.I):
            subsubsection += 1
            expected = f"{chapter}.{subsection}.{subsubsection} "
        else:
            continue
        if not label.startswith(expected):
            issues.append(f"line {line_number}: expected heading prefix {expected!r}, got {label!r}")
    return issues


def _escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
        "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in value)


def _inline(value: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _INLINE.finditer(value):
        parts.append(_escape(value[cursor:match.start()]))
        if match.group("math") is not None:
            parts.append(match.group("math"))
        elif match.group("strong") is not None:
            parts.append(r"\textbf{" + _inline(match.group("strong")) + "}")
        elif match.group("em") is not None:
            parts.append(r"\emph{" + _inline(match.group("em")) + "}")
        elif match.group("code") is not None:
            parts.append(r"\texttt{" + _escape(match.group("code")) + "}")
        elif match.group("link_label") is not None:
            parts.append(r"\href{" + _escape(match.group("link_url")) + "}{" +
                         _inline(match.group("link_label")) + "}")
        elif match.group("url") is not None:
            url = match.group("url").rstrip(".,;，。")
            punctuation = match.group("url")[len(url):]
            parts.append(r"\url{" + url + "}" + _escape(punctuation))
        elif match.group("citation") is not None:
            display = compact_citation_list(match.group("citation"))
            parts.append(r"{\footnotesize\color{gray}" + _escape(display).replace(", ", r",\allowbreak ") + "}")
        else:
            parts.append(r"\newline{}")
        cursor = match.end()
    parts.append(_escape(value[cursor:]))
    return "".join(parts)


def markdown_to_latex(markdown: str) -> str:
    """Create an XeLaTeX-ready article, with real headings, lists, and tables."""
    lines = markdown.splitlines()
    title = next((match.group(1).strip() for line in lines
                  if (match := re.match(r"^#\s+(.+)$", line))), "Project ENSEMBLE")
    output = [
        r"\documentclass[11pt,a4paper]{ctexart}",
        r"\usepackage[margin=25mm]{geometry}",
        r"\usepackage{longtable,array,xcolor,hyperref}",
        r"\usepackage{amsmath,amssymb}",
        r"\hypersetup{colorlinks=true,linkcolor=blue,urlcolor=blue}",
        r"\setcounter{secnumdepth}{3}", r"\setcounter{tocdepth}{2}",
        r"\setlength{\emergencystretch}{3em}",
        r"\sloppy",
        r"\title{" + _inline(title) + "}",
        r"\date{}", r"\begin{document}", r"\maketitle", r"\tableofcontents", r"\clearpage",
    ]
    in_fence = False
    list_kind: str | None = None
    table: list[list[str]] = []
    skip_contents = False
    math_closing: str | None = None
    math_lines: list[str] = []

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            output.append(r"\end{" + list_kind + "}")
            list_kind = None

    def flush_table() -> None:
        nonlocal table
        if not table:
            return
        width = max(len(row) for row in table)
        fraction = 0.94 / width
        spec = "@{}" + "".join(
            r">{\raggedright\arraybackslash}p{" + f"{fraction:.4f}" + r"\linewidth}"
            for _ in range(width)
        ) + "@{}"
        output.append(r"\begingroup\footnotesize")
        output.append(r"\begin{longtable}{" + spec + "}")
        for index, row in enumerate(table):
            padded = row + [""] * (width - len(row))
            output.append(" & ".join(_inline(cell) for cell in padded) + r" \\")
            if index == 0:
                output.append(r"\hline")
        output.extend([r"\end{longtable}", r"\endgroup"])
        table = []

    for line in lines:
        fence = re.match(r"^\s*(```|~~~)", line)
        if fence:
            close_list(); flush_table()
            output.append(r"\end{verbatim}" if in_fence else r"\begin{verbatim}")
            in_fence = not in_fence
            continue
        if in_fence:
            output.append(line)
            continue
        if math_closing is not None:
            content, closed = display_math_continue(line, math_closing)
            if content:
                math_lines.append(content)
            if closed:
                output.extend([r"\[", "\n".join(math_lines), r"\]"])
                math_lines.clear()
                math_closing = None
            continue
        math_start = display_math_start(line)
        if math_start is not None:
            close_list(); flush_table()
            math_closing, content, closed = math_start
            if content:
                math_lines.append(content)
            if closed:
                output.extend([r"\[", "\n".join(math_lines), r"\]"])
                math_lines.clear()
                math_closing = None
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if skip_contents:
            if heading:
                skip_contents = False
            else:
                continue
        if heading:
            close_list(); flush_table()
            level = len(heading.group(1))
            label = heading.group(2).strip()
            if level == 1:
                continue
            if level == 2 and label == "目录":
                skip_contents = True
                continue
            if level == 2 and "参考文献" in label:
                output.append(r"\clearpage")
            label = _HEADING_NUMBER.sub("", label)
            command = {2: "section", 3: "subsection", 4: "subsubsection",
                       5: "paragraph", 6: "subparagraph"}[level]
            if level >= 5 or (level == 4 and re.match(r"^(?:表|图|Table|Figure)\s*\d", label, re.I)):
                command += "*"
            output.append("\\" + command + "{" + _inline(label) + "}")
            continue
        if not line.strip():
            close_list(); flush_table(); output.append(r"\par")
            continue
        if re.match(r"^\s*\|.*\|\s*$", line):
            close_list()
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            table.append(cells)
            continue
        flush_table()
        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        unordered = re.match(r"^\s*[-*]\s+(.+)$", line)
        if ordered or unordered:
            kind = "enumerate" if ordered else "itemize"
            if kind != list_kind:
                close_list(); output.append(r"\begin{" + kind + "}"); list_kind = kind
            output.append(r"\item " + _inline((ordered or unordered).group(1)))
            continue
        close_list()
        reference = re.match(r"^\[(\d+)\]\s+(.+)$", line)
        if reference:
            output.append(r"\noindent\hangindent=2em\hangafter=1 " +
                          _inline("[" + reference.group(1) + "] " + reference.group(2)) + r"\par")
        elif line.startswith(">"):
            output.append(r"\begin{quote}" + _inline(line[1:].strip()) + r"\end{quote}")
        elif re.match(r"^\s*[-*_]{3,}\s*$", line):
            output.append(r"\medskip\hrule\medskip")
        else:
            output.append(_inline(line))
    close_list(); flush_table()
    if math_closing is not None:
        raise ValueError("LATEX_MATH_RENDERING_FAILED: unclosed display math block")
    if in_fence:
        output.append(r"\end{verbatim}")
    output.append(r"\end{document}")
    return "\n".join(output) + "\n"
