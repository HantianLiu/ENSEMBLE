"""Presentation-only math parsing and vector typesetting for ReportLab PDFs.

The approved Markdown and generated LaTeX remain the copyable source of truth.
Unsupported math is an explicit publication error, never silently printed as
literal TeX commands or stripped from the reader-facing PDF.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, Spacer


INLINE_MATH_TOKEN = (
    r"(?<!\\)(?<!\$)\$(?!\$|\s)(?:\\.|[^\\$\n])+?(?<!\s)\$(?!\$)"
    r"|\\\(.+?\\\)"
)
_DISPLAY_ENVIRONMENT = re.compile(
    r"\\begin\{(?P<environment>aligned|align\*?|gathered|gather\*?|split|equation\*?)\}"
    r"(?P<body>.*?)\\end\{(?P=environment)\}",
    re.DOTALL,
)
_PMATRIX = re.compile(r"\\begin\{pmatrix\}(.*?)\\end\{pmatrix\}", re.DOTALL)

_MATH_SPAN = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]|\$\$(.*?)\$\$", re.DOTALL)
_JSON_ESCAPED_MATH_COMMANDS = (
    ("\x08eta", r"\beta"), ("\x08ar", r"\bar"),
    ("\theta", r"\theta"), ("\tau", r"\tau"), ("\to", r"\to"),
    ("\nho", r"\rho"), ("\nangle", r"\rangle"), ("\nu", r"\nu"),
)
_EXISTING_MATH_OR_LITERAL = re.compile(
    r"(\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$|(?<!\$)\$[^$\n]+\$"
    r"|```.*?```|`[^`\n]*`|https?://[^\s)]+)",
    re.DOTALL,
)
_UNMARKED_MATH_ATOM = re.compile(
    r"(?<![A-Za-z0-9_\\])"
    r"([A-Za-zΑ-Ωα-ωℓ\U0001D400-\U0001D7FF]"
    r"[A-Za-z0-9Α-Ωα-ωℓ\U0001D400-\U0001D7FF]*"
    r"(?:_[A-Za-z0-9Α-Ωα-ω∞Σ\U0001D400-\U0001D7FF]+"
    r"|\^[A-Za-z0-9Α-Ωα-ω∞Σ\U0001D400-\U0001D7FF]+)+"
    r"|[\U0001D400-\U0001D7FF])"
    r"(?![A-Za-z0-9_])"
)
_BARE_GREEK_ATOM = re.compile(
    r"(?<![A-Za-z0-9_Α-Ωα-ωℓ\U0001D400-\U0001D7FF^{=≡≈+−×÷*])"
    r"([Α-Ωα-ωℓ])"
    r"(?![A-Za-z0-9_Α-Ωα-ωℓ\U0001D400-\U0001D7FF^({=≡≈+−×÷*])"
)
_BARE_INTEGRAL_FORMULA = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:[A-Za-zΑ-Ωα-ωℓ][A-Za-z0-9Α-Ωα-ωℓ_]*[=≡＝])?"
    r"(?:_[\u3400-\u9fff]{1,8}|[^\s，；。\u3400-\u9fff])*?∫"
    r"(?:_[\u3400-\u9fff]{1,8}|[^\s，；。\u3400-\u9fff])*"
)
_NON_MATH_IDENTIFIERS = frozenset({
    "record_id", "source_id", "packet_id", "meeting_id", "model_id",
    "draft_path", "input_path", "output_path", "file_path",
})

# Publication-only repair for equations whose operators were left between
# separately delimited inline math atoms (for example ``\(A\)=\(B\)``).
_EQUATION_OPERATOR = r"[=＝+−\-×÷*/≡≈<>≤≥∝]"
_MATH_ATOM = r"[A-Za-zΑ-Ωα-ωℓ\U0001D400-\U0001D7FF][A-Za-z0-9Α-Ωα-ωℓ\U0001D400-\U0001D7FF]*"
_INLINE_MATH_SPAN = re.compile(r"\\\((.*?)\\\)")
_FORMULA_BRIDGE = re.compile(
    r"[A-Za-z0-9Α-Ωα-ωℓ∞Σ∑∫∂Δγπ𝒪_{}()<>≤≥=＝+＋−\-×÷*/≡≈→∝^., \t]*"
)
_FORMULA_SUFFIX = re.compile(
    r"[A-Za-z0-9Α-Ωα-ωℓ∞Σ∑∫∂Δγπ𝒪_{}()<>≤≥=＝+＋−\-×÷*/≡≈→∝^.,]*"
)
_JOIN_BARE_LEFT = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<atom>{_MATH_ATOM})(?P<operator>\s*{_EQUATION_OPERATOR}\s*)\\\((?P<right>.*?)\\\)"
)
_JOIN_BARE_RIGHT = re.compile(
    rf"\\\((?P<left>.*?)\\\)(?P<operator>\s*{_EQUATION_OPERATOR}\s*)(?P<atom>{_MATH_ATOM})(?![A-Za-z0-9_])"
)
_MATH_PUNCTUATION = str.maketrans("＝＋－＜＞／", "=+-<>/")
_BARE_LIMIT_OPERATOR = re.compile(
    r"(?<![A-Za-z\\])(?P<operator>lim|sup|inf|max|min)(?=\s*[_^])"
)
_BARE_WORD_SUBSCRIPT = re.compile(r"_(?![\\{])(?P<label>[A-Za-z]{2,})(?![A-Za-z])")
_ROMAN_SUBSCRIPT_LABELS = frozenset({
    "pot", "kin", "mech", "app", "proj", "int", "cw", "or", "min", "max",
    "crit", "eff", "obs", "bulk", "ref", "fit", "exp", "sim", "tot",
})
_TRAILING_SUPERSCRIPT = re.compile(
    r"\\\((?P<formula>(?:(?!\\\)|\\\().)*?)\\\)(?P<power>[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)"
    r"(?P<tail>[+−-]⋯|/[A-Za-zΑ-Ωα-ωεσπ][A-Za-z0-9Α-Ωα-ωεσπ_]*|\))?"
)
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_SIMPLE_RATIO_TERM = re.compile(
    r"(?P<numerator>[A-Za-zΑ-Ωα-ω][A-Za-z0-9Α-Ωα-ω_{}]*)/"
    r"(?P<denominator>[A-Za-zΑ-Ωα-ω][A-Za-z0-9Α-Ωα-ω_{}^]*)$"
)
_EQUATION_RELATION = re.compile(r"[=＝≡≈≤≥∝]|\\(?:approx|equiv|leq|geq|sim|simeq|propto)\b")
_MATH_CONTINUATION = frozenset("=＝≡≈≤≥∝+＋−-×÷*/／^_{([∫∑Σ")
_MATH_CLOSING_CONTINUATION = frozenset("=＝≡≈≤≥∝+＋−-×÷*/／^_}])")


def should_display_inline_equation(formula: str, before: str, after: str) -> bool:
    """Promote a complete equation, but keep variables and short conditions inline."""
    expression = formula.strip()
    if not _EQUATION_RELATION.search(expression) or not _balanced_formula(expression):
        return False
    if before.rstrip() and before.rstrip()[-1] in _MATH_CONTINUATION:
        return False
    if after.lstrip() and after.lstrip()[0] in _MATH_CLOSING_CONTINUATION:
        return False
    return True


def _joined_formula(left: str, operator: str, right: str) -> str:
    return rf"\({left}{operator.translate(_MATH_PUNCTUATION)}{right}\)"


def _is_formula_bridge(bridge: str) -> bool:
    if not bridge.strip():
        return True
    # A bridge may contain a bare operator, variable or TeX-like limit
    # fragment, but never prose or a citation. Examples: "+", "≡lim_{",
    # "→∞}". Preserve the exact symbols; only unite the math delimiters.
    return bool(
        len(bridge) <= 64
        and _FORMULA_BRIDGE.fullmatch(bridge)
        and re.search(r"[=＝+＋−\-×÷*/≡≈<>≤≥→∝_^{}]", bridge)
    )


def _balanced_formula(expression: str) -> bool:
    braces = 0
    parentheses = 0
    for character in expression:
        if character == "{":
            braces += 1
        elif character == "}":
            braces -= 1
            if braces < 0:
                return False
        elif character == "(":
            parentheses += 1
        elif character == ")":
            parentheses -= 1
            if parentheses < 0:
                return False
    return braces == 0 and parentheses == 0


def _combine_adjacent_math_spans(line: str) -> str:
    spans = list(_INLINE_MATH_SPAN.finditer(line))
    if len(spans) < 2:
        return line
    result: list[str] = []
    cursor = 0
    index = 0
    while index < len(spans):
        start = index
        expression = spans[index].group(1)
        while index + 1 < len(spans):
            bridge = line[spans[index].end():spans[index + 1].start()]
            if not _is_formula_bridge(bridge):
                break
            expression += bridge + spans[index + 1].group(1)
            index += 1
        end = spans[index].end()
        if index > start and not _balanced_formula(expression):
            suffix = _FORMULA_SUFFIX.match(line[end:]).group(0)
            if suffix and _balanced_formula(expression + suffix):
                expression += suffix
                end += len(suffix)
        if index > start and _balanced_formula(expression):
            result.append(line[cursor:spans[start].start()])
            result.append(r"\(" + expression.translate(_MATH_PUNCTUATION) + r"\)")
            cursor = end
        else:
            result.append(line[cursor:end])
            cursor = end
        index += 1
    result.append(line[cursor:])
    return "".join(result)


def repair_json_decoded_math_commands(markdown: str) -> str:
    """Restore unescaped TeX commands that JSON decoded as control characters.

    This only touches recognizable commands within math delimiters. It does not
    reinterpret ordinary whitespace or invent mathematical content.
    """
    def repair_span(match: re.Match[str]) -> str:
        span = match.group(0)
        for damaged, command in _JSON_ESCAPED_MATH_COMMANDS:
            span = span.replace(damaged, command)
        return span

    return _MATH_SPAN.sub(repair_span, markdown)


def _top_level_positions(expression: str, characters: str) -> list[int]:
    braces = parentheses = 0
    positions: list[int] = []
    for index, character in enumerate(expression):
        if character == "{":
            braces += 1
        elif character == "}":
            braces -= 1
        elif character == "(":
            parentheses += 1
        elif character == ")":
            parentheses -= 1
        elif character in characters and braces == parentheses == 0:
            positions.append(index)
    return positions


def _normalize_simple_fraction(expression: str, *, display: bool) -> str:
    """Typeset one unambiguous top-level ratio; never infer an equation."""
    if "\\frac" in expression or "\\begin" in expression or "\n" in expression:
        return expression
    relations = _top_level_positions(expression, "=≡≈")
    if not display and not relations:
        return expression
    start = relations[-1] + 1 if relations else 0
    rhs = expression[start:]
    operator_prefix = ""
    if rhs.startswith(r"\lim_{"):
        closing = next(
            (index for index in range(len(rhs))
             if rhs[index] == "}" and _balanced_formula(rhs[5:index + 1])),
            None,
        )
        if closing is None:
            return expression
        operator_prefix, rhs = rhs[:closing + 1], rhs[closing + 1:]
    elif rhs.lstrip().startswith((r"\lim", r"\sum", r"\int")):
        return expression
    slashes = _top_level_positions(rhs, "/")
    if len(slashes) != 1 or _top_level_positions(rhs, "+−-"):
        return expression
    slash = slashes[0]
    numerator, denominator = rhs[:slash].strip(), rhs[slash + 1:].strip()
    if not numerator or not denominator:
        return expression
    if denominator.startswith("(") and denominator.endswith(")") and _balanced_formula(denominator[1:-1]):
        denominator = denominator[1:-1]
    return expression[:start] + operator_prefix + rf"\frac{{{numerator}}}{{{denominator}}}"


def _normalize_additive_simple_fractions(expression: str) -> str:
    """Stack independent, simple ratio terms without changing their sum."""
    if "\\frac" in expression or "\\begin" in expression:
        return expression
    relations = _top_level_positions(expression, "=≡≈")
    if len(relations) != 1:
        return expression
    start = relations[0] + 1
    rhs = expression[start:]
    separators = _top_level_positions(rhs, "+")
    if not separators:
        return expression
    bounds = [0, *(position + 1 for position in separators), len(rhs) + 1]
    terms = [rhs[bounds[index]:bounds[index + 1] - 1]
             for index in range(len(bounds) - 1)]
    changed = False
    for index, term in enumerate(terms):
        match = _SIMPLE_RATIO_TERM.fullmatch(term)
        if match:
            terms[index] = rf"\frac{{{match['numerator']}}}{{{match['denominator']}}}"
            changed = True
    return expression[:start] + "+".join(terms) if changed else expression


def _absorb_trailing_superscripts(line: str) -> str:
    """Keep a superscript immediately after a math span in that formula."""
    def replace(match: re.Match[str]) -> str:
        formula = match["formula"]
        exponent = match["power"].translate(_SUPERSCRIPT_DIGITS)
        tail = match["tail"] or ""
        if not _balanced_formula(formula):
            # A single closing parenthesis left just outside the math span
            # belongs to its unmatched opening parenthesis; put the exponent
            # on the final atom before closing it. Other imbalances are unsafe.
            if tail == ")" and _balanced_formula(formula + ")"):
                return r"\(" + formula + "^{" + exponent + "})" + r"\)"
            return match.group(0)
        # In a standard curvature expansion, the order of a_n and 1/R^n
        # agrees. This narrow case is unambiguous; other compound expressions
        # retain the original scope with explicit parentheses.
        coefficient = re.search(r"a_(?P<order>\d+)/R$", formula)
        if coefficient and coefficient["order"] == exponent:
            formula += "^{" + exponent + "}"
        elif _top_level_positions(formula, "=≡≈"):
            formula += "^{" + exponent + "}"
        elif _top_level_positions(formula, "+−-"):
            formula = "(" + formula + ")^{" + exponent + "}"
        else:
            formula += "^{" + exponent + "}"
        if tail.endswith("⋯"):
            tail = tail[:-1] + r"\cdots"
        if tail == ")":
            return r"\(" + formula + r"\)" + tail
        return r"\(" + formula + tail + r"\)"

    return _TRAILING_SUPERSCRIPT.sub(replace, line)


def _normalize_math_notation(expression: str, *, display: bool = False) -> str:
    expression = expression.translate(_MATH_PUNCTUATION)
    superscripts = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ᴸ", "0123456789-+L")
    subscripts = str.maketrans("₀₁₂₃₄₅₆₇₈₉ₓ₋", "0123456789x-")
    expression = re.sub(
        r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ᴸ]+",
        lambda match: "^{" + match.group().translate(superscripts) + "}", expression,
    )
    expression = re.sub(
        r"[₀₁₂₃₄₅₆₇₈₉ₓ₋]+",
        lambda match: "_{" + match.group().translate(subscripts) + "}", expression,
    )
    expression = re.sub(
        r"([A-Za-zΑ-Ωα-ω])\u0304",
        lambda match: rf"\bar{{{match.group(1)}}}", expression,
    )
    expression = expression.replace("−", "-").replace("⟨", r"\langle ").replace("⟩", r"\rangle ")
    expression = re.sub(
        r"∫_([\u3400-\u9fff]+)(?=d[A-Za-z])",
        lambda match: rf"\int_{{\mathrm{{{match.group(1)}}}}}",
        expression,
    )
    expression = expression.replace("∫", r"\int ")
    expression = re.sub(r"\^-(\d+)", lambda match: "^{-" + match.group(1) + "}", expression)
    expression = re.sub(r"\^\(([^()]*)\)", lambda match: "^{(" + match.group(1) + ")}", expression)
    expression = _BARE_LIMIT_OPERATOR.sub(
        lambda match: "\\" + match["operator"], expression,
    )
    expression = _BARE_WORD_SUBSCRIPT.sub(
        lambda match: (
            "_{" + rf"\mathrm{{{match['label']}}}" + "}"
            if match["label"].lower() in _ROMAN_SUBSCRIPT_LABELS
            else "_{" + match["label"] + "}"
            if match["label"].lower() in {"xx", "yy", "xy", "yx"}
            else match.group(0)
        ),
        expression,
    )
    expression = _normalize_additive_simple_fractions(expression)
    return _normalize_simple_fraction(expression, display=display)


def normalize_math_operator_commands(markdown: str) -> str:
    """Repair math typography inside delimiters, without touching prose."""
    def repair_span(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith((r"\(", r"\[")):
            opening, closing = token[:2], token[-2:]
        elif token.startswith("$$"):
            opening = closing = "$$"
        elif token.startswith("$"):
            opening = closing = "$"
        else:
            return token
        expression = _normalize_math_notation(
            token[len(opening):-len(closing)], display=opening in {"$$", r"\["},
        )
        return opening + expression + closing

    return _EXISTING_MATH_OR_LITERAL.sub(repair_span, markdown)


def mark_unambiguous_math_atoms(markdown: str) -> str:
    """Mark plain subscript/superscript atoms without rewriting their symbols.

    This is a narrow publication repair, not an equation interpreter. Existing
    math, code, URLs, and bibliography entries remain untouched.
    """
    lines = markdown.splitlines(keepends=True)
    result: list[str] = []
    references = False
    in_fence = False
    display_closing: str | None = None
    for line in lines:
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            result.append(line)
            continue
        if in_fence:
            result.append(line)
            continue
        if display_closing is not None:
            result.append(line)
            if display_closing in line:
                display_closing = None
            continue
        math_start = display_math_start(line)
        if math_start is not None:
            closing, _content, closed = math_start
            if not closed:
                display_closing = closing
            result.append(line)
            continue
        if re.match(r"^##\s+(?:参考文献|References|Références)\s*$", line.strip(), re.I):
            references = True
        if references or line.lstrip().startswith("#"):
            result.append(line)
            continue
        # An older draft may delimit variables *inside* an otherwise bare
        # integral equation, e.g. ``ℓ(\(q_c\))=∫...h_{\(q_c\)}...``.
        # Treat the complete, punctuation-bounded equation as one formula;
        # leaving the nested spans in place makes later parsing truncate it.
        def rejoin_integral(match: re.Match[str]) -> str:
            formula = match.group()
            if "=" not in formula and "≡" not in formula:
                return formula
            if r"\(" not in formula or r"\)" not in formula:
                return formula
            return r"\(" + formula.replace(r"\(", "").replace(r"\)", "") + r"\)"

        line = _BARE_INTEGRAL_FORMULA.sub(rejoin_integral, line)
        chunks = _EXISTING_MATH_OR_LITERAL.split(line)
        for index in range(0, len(chunks), 2):
            with_integrals = _BARE_INTEGRAL_FORMULA.sub(
                lambda match: rf"\({match.group()}\)", chunks[index],
            )
            subchunks = _EXISTING_MATH_OR_LITERAL.split(with_integrals)
            for subindex in range(0, len(subchunks), 2):
                subchunks[subindex] = _UNMARKED_MATH_ATOM.sub(
                    lambda match: match.group(1) if match.group(1) in _NON_MATH_IDENTIFIERS
                    else rf"\({match.group(1)}\)",
                    subchunks[subindex],
                )
                subchunks[subindex] = _BARE_GREEK_ATOM.sub(
                    lambda match: rf"\({match.group(1)}\)", subchunks[subindex],
                )
            chunks[index] = "".join(subchunks)
        result.append("".join(chunks))
    return "".join(result)


def normalize_fragmented_inline_math(markdown: str) -> str:
    """Rejoin purely typographical splits in inline formulae for publication.

    Only explicit arithmetic/relation operators may bridge fragments. This
    changes math delimiters, not symbols or the scientific content. Code,
    display formulae, headings, and bibliography entries are left alone.
    """
    markdown = mark_unambiguous_math_atoms(markdown)
    result: list[str] = []
    references = False
    in_fence = False
    display_closing: str | None = None
    for line in markdown.splitlines(keepends=True):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            result.append(line)
            continue
        if in_fence:
            result.append(line)
            continue
        if display_closing is not None:
            result.append(line)
            if display_closing in line:
                display_closing = None
            continue
        math_start = display_math_start(line)
        if math_start is not None:
            closing, _content, closed = math_start
            if not closed:
                display_closing = closing
            result.append(line)
            continue
        if re.match(r"^##\s+(?:参考文献|References|Références)\s*$", line.strip(), re.I):
            references = True
        if references or line.lstrip().startswith("#") or "`" in line:
            result.append(line)
            continue
        current = line
        while True:
            updated = _combine_adjacent_math_spans(_absorb_trailing_superscripts(current))
            updated = _JOIN_BARE_LEFT.sub(
                lambda match: _joined_formula(match['atom'], match['operator'], match['right']),
                updated,
            )
            updated = _JOIN_BARE_RIGHT.sub(
                lambda match: _joined_formula(match['left'], match['operator'], match['atom']),
                updated,
            )
            if updated == current:
                break
            current = updated
        result.append(current)
    return "".join(result)


class MathSafeParagraph(Paragraph):
    """Keep inline vector math positioned by ReportLab's paragraph layout."""

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        try:
            return super().wrap(availWidth, availHeight)
        except TypeError as exc:
            if (
                "ord() expected a character" not in str(exc)
                or "<img " not in self.text
                or self.style.wordWrap != "CJK"
            ):
                raise
            self.style = self.style.clone(
                self.style.name + "MathLTR", wordWrap="LTR", splitLongWords=1,
            )
            return super().wrap(availWidth, availHeight)

    def draw(self) -> None:
        original_draw_image = self.canv.drawImage

        def draw_vector_or_image(image, x, y, width=None, height=None, **kwargs):
            layout = _VECTOR_MATH_LAYOUTS.get(str(getattr(image, "fileName", "")))
            if layout is None:
                return original_draw_image(image, x, y, width=width, height=height, **kwargs)
            _draw_vector_math(
                self.canv, layout, x, y,
                width if width is not None else float(layout.width),
                height if height is not None else float(layout.height),
            )
            return None

        self.canv.drawImage = draw_vector_or_image
        try:
            super().draw()
        finally:
            self.canv.drawImage = original_draw_image


_VECTOR_MATH_LAYOUTS: dict[str, object] = {}


def _draw_vector_math(sheet, layout, x: float, y: float, width: float, height: float) -> None:
    """Draw Matplotlib's laid-out glyphs as selectable PDF text, not a bitmap."""
    x_scale = width / max(1.0, float(layout.width))
    y_scale = height / max(1.0, float(layout.height))
    sheet.saveState()
    try:
        for glyph in layout.glyphs:
            if len(glyph) == 6:
                font, size, codepoint, _glyph_index, x_offset, y_offset = glyph
            else:
                font, size, codepoint, x_offset, y_offset = glyph
            font_path = Path(font.fname)
            name = "ENSEMBLE-MATH-" + hashlib.sha256(str(font_path).encode()).hexdigest()[:12]
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(font_path)))
            if codepoint not in pdfmetrics.getFont(name).face.charWidths:
                raise ValueError(
                    f"PDF_MATH_RENDERING_FAILED: glyph U+{codepoint:04X} missing in {font_path.name}"
                )
            sheet.setFont(name, float(size) * y_scale)
            sheet.drawString(
                x + float(x_offset) * x_scale,
                y + (float(y_offset) + float(layout.depth)) * y_scale,
                chr(codepoint),
            )
        for left, bottom, rect_width, rect_height in layout.rects:
            sheet.rect(
                x + float(left) * x_scale,
                y + (float(bottom) + float(layout.depth)) * y_scale,
                float(rect_width) * x_scale,
                float(rect_height) * y_scale,
                fill=1, stroke=0,
            )
    finally:
        sheet.restoreState()


class _VectorMathFlowable(Flowable):
    def __init__(self, layout, width: float, height: float) -> None:
        super().__init__()
        self.layout = layout
        self.width = width
        self.height = height
        self.hAlign = "CENTER"

    def draw(self) -> None:
        _draw_vector_math(self.canv, self.layout, 0, 0, self.width, self.height)


def inline_math_content(token: str) -> str:
    if token.startswith(r"\(") and token.endswith(r"\)"):
        return token[2:-2]
    return token[1:-1]


def display_math_start(line: str) -> tuple[str, str, bool] | None:
    """Return closing delimiter, initial content, and whether it closes here."""

    stripped = line.strip()
    for opening, closing in (("$$", "$$"), (r"\[", r"\]")):
        if stripped.startswith(opening):
            rest = stripped[len(opening):]
            if rest.endswith(closing) and len(rest) > len(closing):
                return closing, rest[:-len(closing)].strip(), True
            return closing, rest, False
    return None


def display_math_continue(line: str, closing: str) -> tuple[str, bool]:
    stripped = line.strip()
    if stripped.endswith(closing):
        return stripped[:-len(closing)].strip(), True
    return stripped, False


def repair_nested_display_fences(markdown: str) -> str:
    """Remove redundant display wrappers without swallowing explanatory prose.

    Some drafts emit ``$$ / $$equation$$ / explanatory prose / $$``, or
    ``$$ / $$ / equation / $$ / explanatory prose / $$``. Treating
    the wrapper as TeX swallows prose and leaves the real equation in Markdown.
    Repair only a complete inner equation with trailing explanation, bounded by
    the next list/heading/code fence. Strip nested inline delimiters only inside
    a recognized display formula. The frozen Markdown remains untouched.
    """
    lines = markdown.splitlines(keepends=True)
    repaired: list[str] = []
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if re.match(r"^\s*(?:```|~~~)", line):
            in_fence = not in_fence
        if not in_fence and line.strip() == "$$" and index + 3 < len(lines):
            inner = lines[index + 1].strip()
            if inner == "$$":
                # A duplicated opening fence: find the inner close, then an
                # outer close after explanation, without crossing a new item.
                candidates: list[int] = []
                for cursor in range(index + 2, min(index + 82, len(lines))):
                    text = lines[cursor].strip()
                    if text.startswith(("#", "- ", "* ", "```", "~~~")):
                        break
                    if text == "$$":
                        candidates.append(cursor)
                        if len(candidates) == 2:
                            break
                if len(candidates) == 2:
                    inner_close, outer_close = candidates
                    formula = "".join(lines[index + 2:inner_close]).strip()
                    prose = "".join(lines[inner_close + 1:outer_close]).strip()
                    # A math command/relation and an explanation are required;
                    # never reinterpret empty or arbitrary paired fences.
                    if (formula and prose and (
                            _EQUATION_RELATION.search(formula) or re.search(r"\\[A-Za-z]+", formula))
                            and not re.search(r"[\u3400-\u9fff]", formula)
                            and (r"\(" in prose or re.search(r"[\u3400-\u9fff]", prose))):
                        repaired.extend([lines[index + 1], *lines[index + 2:inner_close],
                                         lines[inner_close], "\n",
                                         *lines[inner_close + 1:outer_close], "\n"])
                        index = outer_close + 1
                        continue
            if (inner.startswith("$$") and inner.endswith("$$")
                    and len(inner) > 4 and "$$" not in inner[2:-2]):
                closing = next(
                    (candidate for candidate in range(index + 3, min(index + 7, len(lines)))
                     if lines[candidate].strip() == "$$"), None,
                )
                prose = lines[index + 2:closing] if closing is not None else []
                if prose and all(
                    item.strip() and not item.lstrip().startswith(("#", "- ", "* ", "$$", "```", "~~~"))
                    for item in prose
                ):
                    repaired.extend([lines[index + 1], *prose])
                    index = closing + 1
                    continue
        repaired.append(line)
        index += 1
    # Inline delimiters nested in an already-delimited display formula are not
    # TeX grouping; their removal preserves every symbol and its ordering.
    output: list[str] = []
    closing: str | None = None
    in_fence = False
    for line in repaired:
        if re.match(r"^\s*(?:```|~~~)", line):
            in_fence = not in_fence
        if in_fence:
            output.append(line)
            continue
        if closing is not None:
            is_closed = display_math_continue(line, closing)[1]
            output.append(re.sub(r"\\\((.*?)\\\)", r"\1", line))
            if is_closed:
                closing = None
            continue
        display = display_math_start(line)
        if display:
            delimiter, _content, closed = display
            output.append(re.sub(r"\\\((.*?)\\\)", r"\1", line))
            if not closed:
                closing = delimiter
        else:
            output.append(line)
    return "".join(output)


def glossary_formula_markdown(fragment: str) -> str:
    """Keep existing equation/prose boundaries instead of wrapping the whole field."""
    fragment = repair_nested_display_fences(fragment.strip())
    if any(display_math_start(line) for line in fragment.splitlines()):
        return fragment
    if fragment.startswith(r"\(") and fragment.endswith(r"\)") and fragment.count(r"\(") == 1:
        fragment = fragment[2:-2]
    # Formula fields sometimes also contain explanations. Do not put Chinese
    # prose in a display-math wrapper; existing inline math remains Markdown.
    outside_math = re.sub(INLINE_MATH_TOKEN, "", fragment)
    outside_math = re.sub(r"\\(?:text|mathrm)\{[^{}]*\}", "", outside_math)
    if re.search(r"[\u3400-\u9fff]", outside_math):
        return fragment
    return repair_nested_display_fences("$$\n" + fragment + "\n$$")


def safe_pdf_font_grouping_repair(formula: str) -> str | None:
    """Brace a single TeX atom after a font or root command, without changing it."""
    repaired = re.sub(
        r"\\(mathrm|mathbf|mathit|mathsf|mathcal|mathbb|mathfrak|mathtt)([0-9])",
        lambda match: rf"\{match.group(1)}{{{match.group(2)}}}", formula,
    )
    repaired = re.sub(
        r"\\sqrt\s*([A-Za-z0-9])(?![A-Za-z0-9])",
        lambda match: r"\sqrt{" + match.group(1) + "}", repaired,
    )
    repaired = re.sub(
        r"(?<![A-Za-z0-9])([A-Za-z]_(?:\{[^{}]+\}|[A-Za-z0-9]))(?=_(?:\{|[A-Za-z0-9]))",
        lambda match: "{" + match.group(1) + "}", repaired,
    )
    return repaired if repaired != formula else None


class PdfMathRenderer:
    """Typeset formulas as vector text; PNGs are layout-only placeholders."""

    def __init__(self, *, repair_formula: Callable[[str, str, bool], str | None] | None = None) -> None:
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._cache: dict[tuple[str, bool], tuple[Path, float, float, object]] = {}
        self._math_parser = None
        self._cjk_math_family: tuple[Path, str] | None = None
        self._repair_formula = repair_formula

    def __enter__(self) -> "PdfMathRenderer":
        self._temporary = tempfile.TemporaryDirectory(prefix="ensemble-pdf-math-")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._temporary is not None:
            for path, _width, _height, _layout in self._cache.values():
                _VECTOR_MATH_LAYOUTS.pop(str(path), None)
            self._temporary.cleanup()
            self._temporary = None

    def _image(self, formula: str, *, display: bool,
               allow_repair: bool = True) -> tuple[Path, float, float, object]:
        if self._temporary is None:
            raise RuntimeError("PDF math renderer must be used as a context manager")
        expression = _normalize_math_notation(formula.strip(), display=display)
        # Matplotlib mathtext requires braces where ordinary TeX permits a
        # one-token font command (for example ``\mathrm d`` or ``\mathbf r``).
        expression = re.sub(
            r"\\(mathrm|mathbf|mathit|mathsf|mathcal|mathbb|mathfrak|mathtt)\s+([A-Za-z])",
            lambda match: rf"\{match.group(1)}{{{match.group(2)}}}",
            expression,
        )
        expression = re.sub(
            r"\\boldsymbol\s*(\\[A-Za-z]+|[A-Za-z])",
            lambda match: r"\boldsymbol{" + match.group(1) + "}",
            expression,
        )
        expression = re.sub(r"\\ge(?![A-Za-z])", r"\\geq", expression)
        expression = expression.replace(r"\lVert", r"\|").replace(r"\rVert", r"\|")
        # MathText does not implement TeX's explicit delimiter-size commands.
        # Removing only their sizing instruction keeps the same delimiters and
        # their grouping, while leaving supported \left...\right pairs intact.
        expression = re.sub(
            r"\\(?:bigl|bigr|biggl|biggr|Bigl|Bigr|Biggl|Biggr|bigg|Bigg|big|Big)(?=[()\[\]{}|])",
            "", expression,
        )
        def replace_pmatrix(match: re.Match[str]) -> str:
            rows = [[cell.strip() for cell in row.split("&")]
                    for row in match.group(1).split(r"\\")]
            if len(rows) != 2 or not all(rows) or len({len(row) for row in rows}) != 1:
                raise ValueError("PDF_MATH_RENDERING_FAILED: unsupported pmatrix shape")
            # MathText lacks pmatrix but supports a two-row stack enclosed by
            # scalable parentheses. Preserve every matrix entry and row order.
            stacked = r"\\".join(r"\quad ".join(row) for row in rows)
            return r"\left(\substack{" + stacked + r"}\right)"

        expression = _PMATRIX.sub(replace_pmatrix, expression)
        if not expression:
            raise ValueError("PDF_MATH_RENDERING_FAILED: empty mathematical expression")
        key = (expression, display)
        if key in self._cache:
            return self._cache[key]
        digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:20]
        path = Path(self._temporary.name) / f"{digest}.png"
        try:
            from matplotlib import rc_context
            from matplotlib.font_manager import FontProperties, fontManager
            from matplotlib.mathtext import MathTextParser
            from PIL import Image as PILImage

            cjk_path = Path(os.environ.get("ENSEMBLE_CJK_FONT") or "")
            if not cjk_path.is_file():
                cjk_path = Path(__file__).resolve().parents[1] / "assets/fonts/HarmonyOS_Sans_SC_Regular.ttf"
            math_fonts = {"mathtext.fontset": "stix"}
            # Pure mathematical expressions use one serif math family.  The
            # CJK fallback is needed only when an equation itself contains
            # Chinese text, since STIX does not contain Han glyphs.
            if (re.search(r"[\u3400-\u9fff]", expression)
                    and cjk_path.is_file() and cjk_path.suffix.lower() == ".ttf"):
                if self._cjk_math_family is None or self._cjk_math_family[0] != cjk_path:
                    fontManager.addfont(str(cjk_path))
                    self._cjk_math_family = (
                        cjk_path, FontProperties(fname=str(cjk_path)).get_name(),
                    )
                cjk_family = self._cjk_math_family[1]
                math_fonts = {
                    "mathtext.fontset": "custom",
                    "mathtext.fallback": "stix",
                    "mathtext.rm": cjk_family,
                    "mathtext.sf": cjk_family,
                    "mathtext.it": "STIXGeneral:italic",
                    "mathtext.bf": "STIXGeneral:bold",
                    "mathtext.cal": "STIXGeneral:italic",
                    "mathtext.tt": cjk_family,
                }
            with rc_context(math_fonts):
                if self._math_parser is None:
                    self._math_parser = MathTextParser("path")
                layout = self._math_parser.parse(
                    "$" + expression + "$", dpi=72,
                    prop=FontProperties(family="serif", size=12 if display else 9.6),
                )
            if not layout.glyphs and not layout.rects:
                raise ValueError("formula has no drawable glyphs")
            # ReportLab's inline image tag supplies layout dimensions; its
            # transparent placeholder is intercepted before PDF embedding.
            PILImage.new("RGBA", (1, 1), (0, 0, 0, 0)).save(path)
        except (ImportError, ValueError, RuntimeError) as exc:
            reason = str(exc).splitlines()[-1] if str(exc) else type(exc).__name__
            if allow_repair and self._repair_formula is not None:
                candidate = self._repair_formula(formula, reason, display)
                if candidate and candidate != formula:
                    return self._image(candidate, display=display, allow_repair=False)
            raise ValueError(
                "PDF_MATH_RENDERING_FAILED: "
                f"formula {expression[:160]!r}; {type(exc).__name__}: {reason}"
            ) from exc
        dimensions = (path, float(layout.width), float(layout.height), layout)
        _VECTOR_MATH_LAYOUTS[str(path)] = layout
        self._cache[key] = dimensions
        return dimensions

    def inline_markup(self, formula: str) -> str:
        path, width, height, _layout = self._image(formula, display=False)
        return (
            f'<img src="{html.escape(str(path), quote=True)}" '
            f'width="{width:.2f}" height="{height:.2f}" valign="middle"/>'
        )

    def display_flowables(self, formula: str, *, max_width: float = 170 * mm) -> list:
        expression = formula.strip()
        environment = _DISPLAY_ENVIRONMENT.fullmatch(expression)
        if environment is not None:
            body = environment.group("body")
            rows = [row.replace("&", "").strip() for row in re.split(r"\\\\(?:\[[^\]]*\])?", body)]
            rows = [row for row in rows if row]
        else:
            rows = [expression]
        result: list = [Spacer(1, 2 * mm)]
        for index, row in enumerate(rows):
            _path, width, height, layout = self._image(row, display=True)
            scale = min(1.0, max_width / width, (80 * mm) / height)
            result.append(_VectorMathFlowable(layout, width * scale, height * scale))
            if index < len(rows) - 1:
                result.append(Spacer(1, 1 * mm))
        result.append(Spacer(1, 2 * mm))
        return result
