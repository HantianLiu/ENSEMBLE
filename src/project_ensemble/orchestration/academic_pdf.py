"""Reader-facing A4 review layout from the assembled Markdown structure.

This is a presentation renderer, not a scientific or citation validator.  It
keeps the approved Markdown as the source of truth and does not require an
external TeX or Pandoc executable on the runtime host.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
from pathlib import Path
from typing import Callable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import reportlab
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate, CondPageBreak, Frame, HRFlowable, Image, LongTable,
    PageBreak, PageTemplate, Spacer, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from project_ensemble.orchestration.final_publication import (
    _inline_markup, resolve_cjk_font, resolve_symbol_font, validate_pdf,
)
from project_ensemble.orchestration.math_rendering import (
    INLINE_MATH_TOKEN, MathSafeParagraph as Paragraph, PdfMathRenderer,
    display_math_continue, display_math_start, inline_math_content,
    normalize_fragmented_inline_math, normalize_math_operator_commands,
    repair_json_decoded_math_commands, repair_nested_display_fences,
    should_display_inline_equation,
)
from project_ensemble.orchestration.report_palette import DEFAULT_PALETTE, PALETTES
from project_ensemble.orchestration.academic_html import _extract_reader_notes
from project_ensemble.orchestration.academic_figures import GENERATED_IMAGE


def _pdf_reader_note_fallback(markdown: str) -> str:
    """Keep HTML drawer notes readable as numbered endnotes in static PDF."""
    body, notes = _extract_reader_notes(markdown)
    if not notes:
        return markdown
    for index, note_id in enumerate(notes, start=1):
        body = body.replace(f"[^{note_id}]", f"〔注{index}〕")
    appendix = "\n\n## 注释\n\n" + "\n\n".join(
        f"{index}. {value}" for index, value in enumerate(notes.values(), start=1)
    )
    return body.rstrip() + appendix + "\n"


def _pdf_colors(palette: str) -> dict[str, colors.Color]:
    if palette not in PALETTES:
        raise ValueError("unknown report palette")
    return {key: colors.HexColor(PALETTES[palette][key])
            for key in ("ink", "accent", "pale", "line", "mid")}
PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X = 21 * mm
MARGIN_TOP = 20 * mm
MARGIN_BOTTOM = 18 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN_X


class _Chapter(Paragraph):
    def __init__(self, markup: str, style: ParagraphStyle, title: str, number: int):
        super().__init__(markup, style)
        self.toc_title = title
        self.toc_markup = markup
        self.toc_number = number


class _ReviewDocument(BaseDocTemplate):
    def __init__(self, target: io.BytesIO, *, title: str, meeting_id: str, font: str,
                 palette_colors: dict):
        super().__init__(
            target, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
            topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
            title=title, author="Project ENSEMBLE",
        )
        self.meeting_id = meeting_id
        self.font = font
        self.palette_colors = palette_colors
        frame = Frame(
            MARGIN_X, MARGIN_BOTTOM, CONTENT_WIDTH,
            PAGE_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM,
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        )
        self.addPageTemplates(PageTemplate(id="review", frames=[frame], onPage=self._decorate))

    def _decorate(self, sheet: canvas.Canvas, document: BaseDocTemplate) -> None:
        if document.page <= 2:
            return
        sheet.saveState()
        sheet.setStrokeColor(self.palette_colors["line"])
        sheet.line(MARGIN_X, PAGE_HEIGHT - 12 * mm, PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 12 * mm)
        sheet.setFont(self.font, 8)
        sheet.setFillColor(self.palette_colors["mid"])
        sheet.drawString(MARGIN_X, 10 * mm, f"Project ENSEMBLE · {self.meeting_id}")
        sheet.drawRightString(PAGE_WIDTH - MARGIN_X, 10 * mm, str(document.page))
        sheet.restoreState()

    def afterFlowable(self, flowable) -> None:
        if isinstance(flowable, _Chapter):
            key = f"chapter-{flowable.toc_number}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.toc_title, key, level=0)
            self.notify("TOCEntry", (0, flowable.toc_markup, self.page, key))


def _styles(
    font: str, heading_font: str | None = None, section_font: str | None = None,
    palette_colors: dict | None = None,
) -> dict[str, ParagraphStyle]:
    c = palette_colors or _pdf_colors(DEFAULT_PALETTE)
    base = dict(fontName=font, wordWrap="CJK", splitLongWords=1, textColor=c["ink"])
    heading_font = heading_font or font
    section_font = section_font or heading_font
    def style(name: str, size: float, leading: float, **extra) -> ParagraphStyle:
        options = dict(base, fontSize=size, leading=leading, alignment=TA_LEFT)
        options.update(extra)
        return ParagraphStyle(name, **options)
    return {
        "cover": style("AcademicCover", 23, 34, alignment=TA_CENTER,
                       fontName=heading_font, spaceAfter=13 * mm),
        "cover_meta": style("AcademicCoverMeta", 10, 16, alignment=TA_CENTER, textColor=c["accent"]),
        "toc_title": style("AcademicTOCTitle", 20, 28, fontName=heading_font,
                           textColor=c["ink"], spaceAfter=9 * mm),
        "toc_entry": style("AcademicTOCEntry", 9.6, 17, fontName=heading_font,
                           leftIndent=5 * mm, spaceAfter=2 * mm),
        "chapter": style("AcademicChapter", 18.5, 28, fontName=heading_font,
                         spaceAfter=6 * mm, keepWithNext=True),
        "section": style("AcademicSection", 14, 22, fontName=section_font,
                         textColor=c["accent"], backColor=c["pale"], borderColor=c["line"],
                         borderWidth=0.35, borderPadding=5,
                         spaceBefore=7 * mm, spaceAfter=3.5 * mm, keepWithNext=True),
        "subsection": style("AcademicSubsection", 11.7, 18.5,
                            fontName=section_font, textColor=c["accent"],
                            spaceBefore=5 * mm, spaceAfter=2.5 * mm,
                            keepWithNext=True),
        "body": style("AcademicBody", 9.8, 16.4, textColor=c["ink"],
                      spaceAfter=3.5 * mm, allowWidows=0, allowOrphans=0),
        "list": style("AcademicList", 9.6, 15.3, leftIndent=6 * mm, firstLineIndent=-4 * mm, spaceAfter=2 * mm),
        "quote": style("AcademicQuote", 9.7, 15.6, leftIndent=4 * mm, rightIndent=4 * mm,
                       backColor=c["pale"], borderColor=c["accent"], borderWidth=0.5,
                       borderPadding=8, spaceAfter=5 * mm),
        "reference": style("AcademicReference", 8.6, 13.2, leftIndent=7 * mm,
                           firstLineIndent=-7 * mm, spaceAfter=2 * mm),
        "table": style("AcademicTable", 8, 12, spaceAfter=0),
        "card_key": style("AcademicCardKey", 7.8, 11.5, textColor=c["accent"], spaceAfter=0),
        "card_value": style("AcademicCardValue", 7.9, 11.7, spaceAfter=0),
        "card_title": style("AcademicCardTitle", 9, 13, textColor=c["accent"],
                            spaceBefore=2 * mm, spaceAfter=1 * mm, keepWithNext=True),
    }


def _table_flowables(
    rows: list[list[str]], styles: dict[str, ParagraphStyle], markup, palette_colors: dict
) -> list:
    c = palette_colors
    if not rows:
        return []
    width = max(map(len, rows))
    rows = [row + [""] * (width - len(row)) for row in rows]
    if width >= 5:
        headers = rows[0]
        result: list = []
        for index, record in enumerate(rows[1:], start=1):
            result.append(CondPageBreak(27 * mm))
            result.append(Paragraph(f"记录 {index} · " + markup(record[0]), styles["card_title"]))
            cells = [
                [Paragraph(markup(headers[col] or f"字段 {col + 1}"), styles["card_key"]),
                 Paragraph(markup(record[col] or "—"), styles["card_value"])]
                for col in range(width)
            ]
            card = LongTable(
                cells, colWidths=[0.27 * CONTENT_WIDTH, 0.73 * CONTENT_WIDTH],
                splitByRow=1, splitInRow=1, spaceAfter=3 * mm,
            )
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), c["pale"]),
                ("GRID", (0, 0), (-1, -1), 0.25, c["line"]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            result.append(card)
        return result
    data = [[Paragraph(markup(cell or " "), styles["table"]) for cell in row] for row in rows]
    weights: list[float] = []
    for column in range(width):
        lengths = sorted(
            sum(1.0 if ord(char) > 255 else 0.55 for char in re.sub(r"<[^>]+>", "", row[column]))
            for row in rows
        )
        typical = lengths[int(0.65 * (len(lengths) - 1))] if lengths else 12
        weights.append(math.sqrt(min(150, max(16, typical))))
    minimum = 24 * mm if width <= 3 else 18 * mm
    remaining = CONTENT_WIDTH - width * minimum
    widths = [minimum + remaining * value / sum(weights) for value in weights]
    widths[-1] = CONTENT_WIDTH - sum(widths[:-1])
    table = LongTable(
        data, colWidths=widths,
        repeatRows=1, splitByRow=1, splitInRow=1,
        spaceBefore=1 * mm, spaceAfter=3 * mm,
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), c["pale"]),
        ("GRID", (0, 0), (-1, -1), 0.25, c["line"]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return [table]


def render_academic_review_pdf(
    markdown: str, *, meeting_id: str, palette: str = DEFAULT_PALETTE,
    repair_formula: Callable[[str, str, bool], str | None] | None = None,
    figure_assets: dict[str, bytes] | None = None,
) -> tuple[bytes, Path]:
    """Render approved prose with real chapter breaks, TOC, callouts and tables."""
    markdown = _pdf_reader_note_fallback(markdown)
    markdown = normalize_math_operator_commands(
        normalize_fragmented_inline_math(repair_json_decoded_math_commands(
            repair_nested_display_fences(markdown)
        ))
    )
    with PdfMathRenderer(repair_formula=repair_formula) as math_renderer:
        return _render_academic_review_pdf(
            markdown, meeting_id=meeting_id, math_renderer=math_renderer,
            palette_colors=_pdf_colors(palette),
            figure_assets=figure_assets,
        )


def _render_academic_review_pdf(
    markdown: str, *, meeting_id: str, math_renderer: PdfMathRenderer,
    palette_colors: dict,
    figure_assets: dict[str, bytes] | None = None,
) -> tuple[bytes, Path]:
    font_path = resolve_cjk_font()
    font = "ENSEMBLE-ACADEMIC-" + hashlib.sha256(str(font_path).encode()).hexdigest()[:10]
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font, str(font_path)))
    pdfmetrics.registerFontFamily(font, normal=font, bold=font, italic=font, boldItalic=font)
    symbol_path = resolve_symbol_font()
    symbol = "ENSEMBLE-ACADEMIC-SYMBOL-" + hashlib.sha256(str(symbol_path).encode()).hexdigest()[:10]
    if symbol not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(symbol, str(symbol_path)))
    pdfmetrics.registerFontFamily(symbol, normal=symbol, bold=symbol, italic=symbol, boldItalic=symbol)
    glyphs = (
        font, frozenset(pdfmetrics.getFont(font).face.charWidths),
        symbol, frozenset(pdfmetrics.getFont(symbol).face.charWidths),
    )
    configured_heading = os.environ.get("ENSEMBLE_CJK_HEADING_FONT")
    default_heading = font_path.with_name(font_path.name.replace("_Regular.ttf", "_Bold.ttf"))
    heading_path = (
        Path(configured_heading).expanduser() if configured_heading
        else default_heading if default_heading != font_path and default_heading.is_file()
        else Path(reportlab.__file__).resolve().parent / "fonts/VeraBd.ttf"
    )
    heading_font = font
    heading_glyphs = glyphs
    if heading_path.is_file():
        heading_font = "ENSEMBLE-ACADEMIC-HEADING-" + hashlib.sha256(
            str(heading_path).encode()
        ).hexdigest()[:10]
        if heading_font not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(heading_font, str(heading_path)))
        pdfmetrics.registerFontFamily(
            heading_font, normal=heading_font, bold=heading_font,
            italic=heading_font, boldItalic=heading_font,
        )
        heading_glyphs = (
            heading_font, frozenset(pdfmetrics.getFont(heading_font).face.charWidths),
            font, frozenset(pdfmetrics.getFont(font).face.charWidths),
        )
    medium_path = font_path.with_name(font_path.name.replace("_Regular.ttf", "_Medium.ttf"))
    section_font = heading_font
    section_glyphs = heading_glyphs
    if medium_path != font_path and medium_path.is_file() and not configured_heading:
        section_font = "ENSEMBLE-ACADEMIC-MEDIUM-" + hashlib.sha256(
            str(medium_path).encode()
        ).hexdigest()[:10]
        if section_font not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(section_font, str(medium_path)))
        pdfmetrics.registerFontFamily(
            section_font, normal=section_font, bold=section_font,
            italic=section_font, boldItalic=section_font,
        )
        section_glyphs = (
            section_font, frozenset(pdfmetrics.getFont(section_font).face.charWidths),
            font, frozenset(pdfmetrics.getFont(font).face.charWidths),
        )
    markup = lambda value: _inline_markup(
        value.replace("<br>", "<br/>"),
        glyph_context=glyphs, math_renderer=math_renderer,
    )
    reference_markup = lambda value: _inline_markup(
        value.replace("<br>", "<br/>"),
        glyph_context=glyphs, math_renderer=math_renderer,
        citation_mode=False,
    )
    heading_markup = lambda value: _inline_markup(
        value.replace("<br>", "<br/>"),
        glyph_context=heading_glyphs, math_renderer=math_renderer,
    )
    section_markup = lambda value: _inline_markup(
        value.replace("<br>", "<br/>"),
        glyph_context=section_glyphs, math_renderer=math_renderer,
    )
    styles = _styles(font, heading_font, section_font, palette_colors)
    title = next((match.group(1).strip() for line in markdown.splitlines()
                  if (match := re.match(r"^#\s+(.+)$", line))), "文献综述")
    story: list = [
        Spacer(1, 57 * mm), Paragraph(heading_markup(title), styles["cover"]),
        Paragraph(markup(f"Project ENSEMBLE · {meeting_id}"), styles["cover_meta"]),
        PageBreak(), Paragraph(heading_markup("目录"), styles["toc_title"]),
    ]
    toc = TableOfContents()
    toc.levelStyles = [styles["toc_entry"]]
    story.extend([toc, PageBreak()])
    chapter_count = 0
    references = False
    in_fence = False
    skip_contents = False
    abstract_mode = False
    in_modules = False
    module_number = 0
    subsection_number = 0
    paragraph: list[str] = []
    quotes: list[str] = []
    table_rows: list[list[str]] = []
    math_closing: str | None = None
    math_lines: list[str] = []
    inline_math = re.compile(INLINE_MATH_TOKEN)

    def flush_paragraph() -> None:
        if paragraph:
            value = " ".join(part.strip() for part in paragraph)
            style = styles["reference"] if references and re.match(r"^\[\d+\]\s", value) else styles["body"]
            if style is styles["reference"]:
                story.append(Paragraph(reference_markup(value), style))
            else:
                cursor = 0
                displayed = False
                for match in inline_math.finditer(value):
                    formula = inline_math_content(match.group())
                    if not should_display_inline_equation(
                        formula, value[cursor:match.start()], value[match.end():],
                    ):
                        continue
                    prose = value[cursor:match.start()].strip()
                    if displayed:
                        prose = prose.lstrip("，,；;。 ")
                    if prose:
                        story.append(Paragraph(markup(prose), style))
                    story.extend(math_renderer.display_flowables(
                        formula, max_width=CONTENT_WIDTH,
                    ))
                    displayed = True
                    cursor = match.end()
                tail = value[cursor:].strip()
                if displayed:
                    tail = tail.lstrip("，,；;。 ")
                if tail:
                    story.append(Paragraph(markup(tail), style))
            paragraph.clear()

    def flush_quote() -> None:
        if quotes:
            content = "<br/>".join(markup(line.lstrip("> ")) for line in quotes if line.lstrip("> ").strip())
            if content:
                story.append(CondPageBreak(23 * mm))
                story.append(Paragraph(content, styles["quote"]))
            quotes.clear()

    def flush_table() -> None:
        if table_rows:
            story.extend(_table_flowables(table_rows, styles, markup, palette_colors))
            table_rows.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*(```|~~~)", line):
            flush_paragraph(); flush_quote(); flush_table()
            in_fence = not in_fence
            continue
        if in_fence:
            story.append(Paragraph(
                _inline_markup(line, glyph_context=glyphs), styles["body"],
            ))
            continue
        if math_closing is not None:
            content, closed = display_math_continue(line, math_closing)
            if content:
                math_lines.append(content)
            if closed:
                story.extend(math_renderer.display_flowables(
                    " ".join(math_lines), max_width=CONTENT_WIDTH,
                ))
                math_lines.clear()
                math_closing = None
            continue
        math_start = display_math_start(line)
        if math_start is not None:
            flush_paragraph(); flush_quote(); flush_table()
            math_closing, content, closed = math_start
            if content:
                math_lines.append(content)
            if closed:
                story.extend(math_renderer.display_flowables(
                    " ".join(math_lines), max_width=CONTENT_WIDTH,
                ))
                math_lines.clear()
                math_closing = None
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if skip_contents:
            if heading:
                skip_contents = False
            else:
                continue
        generated = GENERATED_IMAGE.fullmatch(line)
        if generated:
            flush_paragraph(); flush_quote(); flush_table()
            data = (figure_assets or {}).get(generated[2])
            if data:
                picture = Image(io.BytesIO(data))
                scale = min(CONTENT_WIDTH / picture.imageWidth, 135 * mm / picture.imageHeight)
                picture.drawWidth = picture.imageWidth * scale
                picture.drawHeight = picture.imageHeight * scale
                picture.hAlign = "CENTER"
                story.extend([Spacer(1, 3 * mm), picture, Spacer(1, 2 * mm)])
            else:
                story.append(Paragraph(markup(generated[1]), styles["body"]))
            continue
        if heading:
            ends_abstract = abstract_mode
            flush_paragraph(); flush_quote(); flush_table()
            level, label = len(heading.group(1)), heading.group(2).strip()
            if ends_abstract:
                story.append(PageBreak())
            if level == 1:
                continue
            if level == 2 and label.casefold() in {"摘要", "abstract", "résumé"}:
                abstract_mode = True
                quotes.append(f"**{label}**")
                continue
            abstract_mode = False
            if level == 2 and label == "目录":
                skip_contents = True
                continue
            if level == 2 and (
                label.casefold() in {
                    "分模块调研结果", "research findings", "résultats de la recherche",
                }
                or label.startswith("正文：")
            ):
                in_modules = True
                continue
            if level == 2:
                in_modules = False
                chapter_count += 1
                if chapter_count > 1:
                    story.append(PageBreak())
                references = "参考文献" in label or "references" in label.lower()
                story.append(Spacer(1, 4 * mm))
                story.append(_Chapter(heading_markup(label), styles["chapter"], label, chapter_count))
                story.append(HRFlowable(width="100%", thickness=0.9, color=palette_colors["accent"], spaceAfter=4 * mm))
            elif level == 3 and in_modules and re.match(r"^\d+[.、]\s+", label):
                chapter_count += 1
                module_number = int(re.match(r"^(\d+)", label).group(1))
                subsection_number = 0
                if chapter_count > 1:
                    story.append(PageBreak())
                story.append(Spacer(1, 4 * mm))
                story.append(_Chapter(heading_markup(label), styles["chapter"], label, chapter_count))
                story.append(HRFlowable(width="100%", thickness=0.9, color=palette_colors["accent"], spaceAfter=4 * mm))
            else:
                if level == 4 and in_modules and module_number:
                    subsection_number += 1
                    if not re.match(r"^\d+(?:\.\d+)+[.、]?\s+", label):
                        label = f"{module_number}.{subsection_number} {label}"
                story.append(CondPageBreak(19 * mm))
                story.append(Paragraph(
                    section_markup(label),
                    styles["section"] if level == 3 else styles["subsection"],
                ))
            continue
        if not line.strip():
            if abstract_mode:
                quotes.append(" ")
                continue
            flush_paragraph(); flush_quote(); flush_table()
            continue
        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            flush_paragraph(); flush_quote(); flush_table()
            story.append(HRFlowable(width="100%", thickness=0.3, color=palette_colors["line"], spaceAfter=2 * mm))
            continue
        if abstract_mode:
            quotes.append(line.lstrip("> ") if line.lstrip().startswith(">") else line)
            continue
        if line.lstrip().startswith(">"):
            flush_paragraph(); flush_table()
            quotes.append(line)
            continue
        flush_quote()
        if re.match(r"^\s*\|.*\|\s*$", line):
            flush_paragraph()
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                table_rows.append(cells)
            continue
        flush_table()
        if re.match(r"^\*\*(?:本章摘要|章节摘要|摘要[。.]?)\*\*[:：]?", line):
            flush_paragraph()
            quotes.append(line)
            continue
        item = re.match(r"^\s*(\d+[.)]|[-*])\s+(.+)$", line)
        if item:
            flush_paragraph()
            marker = "•" if item.group(1) in {"-", "*"} else item.group(1)
            story.append(Paragraph(markup(marker + " " + item.group(2)), styles["list"]))
            continue
        paragraph.append(line)
    if math_closing is not None:
        raise ValueError("PDF_MATH_RENDERING_FAILED: unclosed display math block")
    flush_paragraph(); flush_quote(); flush_table()
    output = io.BytesIO()
    doc = _ReviewDocument(
        output, title=title, meeting_id=meeting_id, font=font,
        palette_colors=palette_colors,
    )

    def canvas_maker(filename, **kwargs):
        kwargs["invariant"] = 1
        return canvas.Canvas(filename, **kwargs)

    doc.multiBuild(story, maxPasses=4, canvasmaker=canvas_maker)
    pdf = output.getvalue()
    validate_pdf(pdf)
    return pdf, font_path
