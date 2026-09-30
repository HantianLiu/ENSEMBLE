from __future__ import annotations

"""Compact, Unicode-aware terminal views for Human consultations."""

from difflib import SequenceMatcher
from pathlib import Path
from typing import TextIO
import json
import re
import unicodedata

from project_ensemble.interface_language import ui_label
from project_ensemble.user_settings import interface_language

from project_ensemble.runtime.terminal_style import (
    BOLD,
    CYAN,
    GREEN,
    RED,
    YELLOW,
    supports_color,
    styled,
    rule_width,
)


_MAX_PREVIEW_CHARS = 1200


def _t(chinese: str, english: str) -> str:
    return ui_label(chinese, english, interface_language() or "zh")


def local_science_review_context(root: Path, context: dict) -> dict:
    """Read only the frozen local-check result and adjacent Writer versions.

    Old open consultations contain only ``check_path`` and ``problems``.  This
    resolver therefore works without rewriting an immutable Human issue.
    """
    root = root.resolve()
    relative = context.get("check_path")
    result = {"problems": [str(value) for value in context.get("problems") or []],
              "check_path": str(relative or ""), "changes": []}
    if not isinstance(relative, str) or not relative:
        return result
    check_path = (root / relative).resolve()
    if not check_path.is_relative_to(root) or not check_path.is_file():
        return result
    try:
        check = json.loads(check_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    findings = [str(item.get("problem")) for item in check.get("checks", [])
                if item.get("status") == "MATERIAL_PROBLEM" and item.get("problem")]
    if findings:
        result["problems"] = findings
    match = re.fullmatch(r"final_local_science_check_(\d+)\.json", check_path.name)
    if not match:
        return result
    number = int(match.group(1))
    before_path = check_path.parent / f"writer_v{number + 1}_validated.json"
    after_path = check_path.parent / f"writer_v{number + 2}_validated.json"
    try:
        before = json.loads(before_path.read_text(encoding="utf-8"))["draft"]["body_markdown"]
        after = json.loads(after_path.read_text(encoding="utf-8"))["draft"]["body_markdown"]
    except (OSError, ValueError, KeyError, TypeError):
        return result
    old_paragraphs = [item.strip() for item in re.split(r"\n\s*\n", before) if item.strip()]
    new_paragraphs = [item.strip() for item in re.split(r"\n\s*\n", after) if item.strip()]
    changes = []
    for action, a0, a1, b0, b1 in SequenceMatcher(
        None, old_paragraphs, new_paragraphs
    ).get_opcodes():
        if action != "equal":
            old_group = old_paragraphs[a0:a1]
            for current_paragraph in new_paragraphs[b0:b1]:
                closest_old = max(
                    old_group, key=lambda item: _science_problem_match(
                        current_paragraph, {"new": item}
                    ), default="",
                )
                changes.append({"old": closest_old, "new": current_paragraph})
    result["changes"] = changes
    return result


def _science_problem_match(problem: str, change: dict) -> int:
    """Rank changed paragraphs for display only; never use this as scientific QC."""
    def grams(value: str) -> set[str]:
        normalized = re.sub(r"\s+", "", value.casefold())
        return {normalized[index:index + 4] for index in range(max(0, len(normalized) - 3))
                if sum(char.isalnum() for char in normalized[index:index + 4]) >= 3}
    candidate = grams(str(change.get("new") or ""))
    lead = problem.split("。", 1)[0]
    return len(grams(problem) & candidate) + 3 * len(grams(lead) & candidate)


def render_local_science_consultation(issue_id: str, context: dict, *, output: TextIO,
                                      full: bool = False) -> None:
    """Put concrete scientific objections before a Human publish/repair choice."""
    width = rule_width(output, maximum=112)
    print(f"┌─ {issue_id} · {_t('局部科学核验未通过', 'Local science check did not pass')}", file=output)
    problems = context.get("problems") or []
    if not problems:
        print("│ " + _t("科学疑点未能从冻结核验记录读取；请先查看完整记录，不宜直接选择出版。", "The scientific concern could not be read from the frozen check; review the full record before publishing."), file=output)
    for index, problem in enumerate(problems, 1):
        print(f"├─ {_t('科学疑点', 'Scientific concern')} {index}/{len(problems)}", file=output)
        preview = _sanitize(str(problem))
        if not full and len(preview) > 1600:
            preview = preview[:1600] + "…"
        _outline_wrapped(preview, output=output, width=width, indent="│ ")
        changes = context.get("changes") or []
        if changes:
            chosen = max(changes, key=lambda item: _science_problem_match(str(problem), item))
            if _science_problem_match(str(problem), chosen) > 0:
                for label, key in ((_t("返修前", "Before revision"), "old"), (_t("当前稿相关位置（自动匹配）", "Current draft excerpt (automatic match)"), "new")):
                    value = _sanitize(str(chosen.get(key) or ""))
                    if not full and len(value) > 1000:
                        value = value[:1000] + "…"
                    print(f"│ {label}：", file=output)
                    _outline_wrapped(value, output=output, width=width, indent="│   ")
    print(f"│ {_t('完整核验记录', 'Full check record')}: {context.get('check_path') or _t('未找到', 'not found')}", file=output)
    if not full:
        print("│ " + _t("按 v 可展开完整疑点及自动匹配的返修前后段落。", "Press v to see the full concern and automatically matched before/after paragraphs."), file=output)


def _sanitize(value: str) -> str:
    return "".join(
        char if char in {"\n", "\t"} or unicodedata.category(char) != "Cc" else "�"
        for char in value
    )


def _char_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in {"Cf", "Mn", "Me"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _display_width(value: str) -> int:
    return sum(_char_width(char) for char in value)


def _diff_cells(current: str, proposed: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    left: list[tuple[str, bool]] = []
    right: list[tuple[str, bool]] = []
    for operation, a0, a1, b0, b1 in SequenceMatcher(
        None, current, proposed, autojunk=False
    ).get_opcodes():
        left.extend((char, operation != "equal") for char in current[a0:a1])
        right.extend((char, operation != "equal") for char in proposed[b0:b1])
    return left, right


def _wrap_cells(cells: list[tuple[str, bool]], width: int) -> list[list[tuple[str, bool]]]:
    rows: list[list[tuple[str, bool]]] = []
    row: list[tuple[str, bool]] = []
    used = 0
    for char, changed in cells:
        if char == "\n":
            rows.append(row)
            row, used = [], 0
            continue
        if char == "\t":
            char = " "
        char_width = _char_width(char)
        if row and used + char_width > width:
            rows.append(row)
            row, used = [], 0
        row.append((char, changed))
        used += char_width
    rows.append(row)
    return rows


def _styled_cells(cells: list[tuple[str, bool]], *, color: str, enabled: bool) -> str:
    if not enabled:
        return "".join(char for char, _changed in cells)
    parts: list[str] = []
    buffer = ""
    previous = False
    for char, changed in cells:
        if buffer and changed != previous:
            parts.append(styled(buffer, color, BOLD, enabled=True) if previous else buffer)
            buffer = ""
        buffer += char
        previous = changed
    if buffer:
        parts.append(styled(buffer, color, BOLD, enabled=True) if previous else buffer)
    return "".join(parts)


def _plain_rows(text: str, width: int) -> list[str]:
    return ["".join(char for char, _ in row) for row in _wrap_cells(
        [(char, False) for char in text], width
    )]


def _already_present_proposal(current: str, proposed: str) -> bool:
    """Recognize a single quoted replacement that is already the current wording."""
    match = re.fullmatch(r"(?:改为|修改为|替换为)[^「]*「([^」]+)」\s*", proposed)
    return bool(match and match.group(1).rstrip("。；， ") == current.rstrip("。；， "))


def render_science_consultation(
    issue_id: str, context: dict, *, output: TextIO
) -> None:
    """Show only the contested excerpt; complete frozen text remains available via ``v``."""
    color = supports_color(output)
    width = rule_width(output, maximum=112)
    objection = context.get("objection") or {}
    advice = context.get("chair_advice") or {}
    current = _sanitize(str(objection.get("current_excerpt") or advice.get("redraw_excerpt") or _t("未定位到当前稿相关片段", "No relevant current-draft excerpt was located")))
    proposed_wording = objection.get("proposed_wording")
    proposed = _sanitize(str(proposed_wording or _t("未提供具体修改措辞；请参阅下方修正意见", "No exact replacement wording was supplied; see the objection below")))
    already_present = _already_present_proposal(current, proposed)
    if already_present:
        proposed = current
    current_preview = current[:_MAX_PREVIEW_CHARS] + ("…" if len(current) > _MAX_PREVIEW_CHARS else "")
    proposed_preview = proposed[:_MAX_PREVIEW_CHARS] + ("…" if len(proposed) > _MAX_PREVIEW_CHARS else "")
    left, right = _diff_cells(current_preview, proposed_preview) if proposed_wording else (
        [(char, False) for char in current_preview],
        [(char, False) for char in proposed_preview],
    )

    cycle = context.get("cycle")
    number = context.get("objection_number")
    count = context.get("objection_count")
    subtitle = (
        _t(f"第 {cycle} 个审核周期 · 异议 {number}/{count}", f"Review cycle {cycle} · objection {number}/{count}")
        if all(isinstance(value, int) for value in (cycle, number, count))
        else _t("科学事实异议", "Scientific objection")
    )
    print(styled(f"{issue_id} · {subtitle}", CYAN, BOLD, enabled=color), file=output)
    if width >= 72:
        content_width = width - 7
        left_width = content_width // 2
        right_width = content_width - left_width
        left_rows = _wrap_cells(left, left_width)
        right_rows = _wrap_cells(right, right_width)
        left_label = _t("当前稿", "Current draft")
        right_label = _t("建议修改稿（未采纳）", "Proposed wording (not adopted)")
        print("┌" + "─" * (left_width + 2) + "┬" + "─" * (right_width + 2) + "┐", file=output)
        print(
            "│ " + left_label + " " * (left_width - _display_width(left_label))
            + " │ " + right_label + " " * (right_width - _display_width(right_label)) + " │",
            file=output,
        )
        print("├" + "─" * (left_width + 2) + "┼" + "─" * (right_width + 2) + "┤", file=output)
        for index in range(max(len(left_rows), len(right_rows))):
            left_line = left_rows[index] if index < len(left_rows) else []
            right_line = right_rows[index] if index < len(right_rows) else []
            left_text = "".join(char for char, _ in left_line)
            right_text = "".join(char for char, _ in right_line)
            print(
                "│ " + _styled_cells(left_line, color=RED, enabled=color)
                + " " * (left_width - _display_width(left_text))
                + " │ " + _styled_cells(right_line, color=GREEN, enabled=color)
                + " " * (right_width - _display_width(right_text)) + " │",
                file=output,
            )
        print("└" + "─" * (left_width + 2) + "┴" + "─" * (right_width + 2) + "┘", file=output)
    else:
        # A genuine two-column table would leave unreadably narrow text here.
        for heading, cells, highlight in ((_t("当前稿", "Current draft"), left, RED), (_t("建议修改稿（未采纳）", "Proposed wording (not adopted)"), right, GREEN)):
            print(styled(heading, CYAN, BOLD, enabled=color), file=output)
            for row in _wrap_cells(cells, max(20, width - 2)):
                print("  " + _styled_cells(row, color=highlight, enabled=color), file=output)
    if len(current) > _MAX_PREVIEW_CHARS or len(proposed) > _MAX_PREVIEW_CHARS:
        print(_t("预览已截短；按 v 查看完整原文、当前稿和本条异议。", "Preview shortened; press v for the full source, draft, and objection."), file=output)
    if already_present:
        print(_t("提示：审阅者建议的这句表述已见于当前稿；请结合下方原始意见判断。", "Note: the reviewer's suggested wording already appears in the draft; assess the original objection below."), file=output)

    long_explanation = False
    for heading, content in (
        (_t("修正意见", "Objection"), str(objection.get("issue") or _t("未提供修正意见", "No objection supplied"))),
        (_t("主席建议", "Chair's advice"), str(advice.get("suggestion") or _t("尚无主席建议", "No Chair advice yet"))),
    ):
        prefix = "── " + heading + " "
        print(prefix + "─" * max(4, width - _display_width(prefix)), file=output)
        sanitized = _sanitize(content)
        if len(sanitized) > _MAX_PREVIEW_CHARS:
            sanitized = sanitized[:_MAX_PREVIEW_CHARS] + "…"
            long_explanation = True
        for line in _plain_rows(sanitized, max(20, width - 2)):
            print("  " + line, file=output)
    if long_explanation:
        print(_t("修正意见或主席建议已折叠；按 v 查看完整材料。", "Objection or Chair advice shortened; press v for the full text."), file=output)
    print("─" * width, file=output)
    print(_t("左栏红色为拟替换文字，右栏绿色为建议新增文字；按 v 查看完整材料。", "Red on the left marks proposed removals; green on the right marks proposed additions. Press v for full text."), file=output)


def _outline_wrapped(
    text: str, *, output: TextIO, width: int, indent: str = "  ",
    continuation_indent: str | None = None,
) -> None:
    for index, row in enumerate(_plain_rows(
        _sanitize(text), max(20, width - _display_width(indent))
    )):
        print((indent if index == 0 else continuation_indent or indent) + row, file=output)


def _outline_scope_parts(notice: str) -> list[str]:
    numbered = re.split(r"(?=\(\d+\)\s*)", notice)
    parts = [part.strip(" ；。\n") for part in numbered if part.strip(" ；。\n")]
    return [part for part in parts if re.match(r"^\(\d+\)", part)] or parts


def render_outline_consultation(
    issue_id: str,
    question: str,
    context: dict,
    outline: dict,
    *,
    output: TextIO,
) -> None:
    """Show the decision and module map first; keep long source prose drillable."""

    color = supports_color(output)
    width = rule_width(output, maximum=112)
    modules = outline.get("modules") if isinstance(outline.get("modules"), list) else []
    skeleton = context.get("article_skeleton") or outline.get("article_skeleton") or []
    disciplines = context.get("proposed_disciplines") or outline.get("proposed_disciplines") or []
    notice = str(context.get("scope_notice") or outline.get("scope_concern_notice") or "")
    print(styled("┌─ " + _t("研究总纲 · 人工审阅", "Research outline · Human review") + " " + "─" * max(4, width - 18), CYAN, BOLD, enabled=color), file=output)
    print(styled(f"│ {issue_id}", CYAN, enabled=color), file=output)
    title = str(outline.get("report_title") or _t("研究模块划分", "Research module plan"))
    _outline_wrapped(
        title, output=output, width=width,
        indent="│ " + _t("题目", "Title") + ": ", continuation_indent="│       ",
    )
    print("├─ " + _t("本次决定", "Decision") + " " + "─" * max(4, width - 11), file=output)
    _outline_wrapped(
        _t(
            f"研究模块 {len(modules) if modules else '待查看'} 个。批准：进入正式检索。否决：写明异议，完整重做规划与审阅。仅骨架有问题：要求主席局部修改。",
            f"Research modules: {len(modules) if modules else 'not available'}. Approve to begin retrieval; reject with reasons to redo planning and review; request a local Chair edit if only the article structure needs work.",
        ),
        output=output, width=width, indent="│ ",
    )
    _outline_wrapped(
        _t("范围意见不授权代表或主席自行缩减原始任务；如需改变范围，请退回重做。", "Scope comments do not authorize representatives or the Chair to narrow the original task. Reject and replan to change scope."),
        output=output, width=width, indent="│ ",
    )
    print("├─ " + _t("模块清单", "Modules") + " " + "─" * max(4, width - 11), file=output)
    if modules:
        for module in modules:
            module_id = str(module.get("module_id") or "?")
            module_title = str(module.get("title") or _t("未命名", "Untitled"))
            _outline_wrapped(f"{module_id}  {module_title}", output=output, width=width, indent="│ ")
    else:
        _outline_wrapped(_t("总纲文件暂不可用；按 v 查看冻结的原始咨询文书。", "Outline unavailable; press v to read the frozen original consultation."), output=output, width=width, indent="│ ")
        _outline_wrapped(question[:240] + ("…" if len(question) > 240 else ""), output=output, width=width, indent="│ ")
    print("├─ " + _t("范围与写作规划", "Scope and writing plan") + " " + "─" * max(4, width - 17), file=output)
    parts = _outline_scope_parts(notice)
    if parts:
        print(styled(_t(f"│ 范围意见 {len(parts)} 项（下列为摘要；按 s 查看完整原文）：", f"│ Scope comments: {len(parts)} (summarized below; press s for full text):"), YELLOW, enabled=color), file=output)
        for part in parts:
            compact = " ".join(part.split())
            heading = compact.split("：", 1)[0]
            if "：" in compact and len(heading) <= 95:
                preview = heading
            elif "（R-" in compact:
                preview = compact.split("（R-", 1)[0]
            else:
                preview = compact[:86] + ("…" if len(compact) > 86 else "")
            _outline_wrapped(preview, output=output, width=width, indent="│   ")
    else:
        _outline_wrapped(_t("未单列主席转呈的范围意见；原始任务范围仍须保留。", "No separate Chair-transmitted scope comments; retain the original task scope."), output=output, width=width, indent="│ ")
    _outline_wrapped(
        _t(f"建议学科 {len(disciplines)} 项 · 文章骨架 {len(skeleton)} 项；按 d / a 分别展开。", f"Suggested disciplines: {len(disciplines)} · article-structure items: {len(skeleton)}; press d / a to expand."),
        output=output, width=width, indent="│ ",
    )
    source = context.get("outline_path")
    if source:
        _outline_wrapped(_t(f"冻结总纲：{source}", f"Frozen outline: {source}"), output=output, width=width, indent="│ ")
    print("└" + "─" * max(4, width - 1), file=output)
    print(_t("按 m 查看单个模块细节；s 范围意见；a 文章骨架；d 学科；v 原始咨询文书。", "Press m for module details; s for scope comments; a for article structure; d for disciplines; v for the original consultation."), file=output)


def render_outline_detail(
    kind: str, question: str, context: dict, outline: dict, *, output: TextIO,
    module_index: int | None = None,
) -> None:
    """Expand one part without reprinting the whole frozen consultation."""

    width = rule_width(output, maximum=112)
    sections = {
        "s": (_t("主席转呈的完整范围意见", "Full scope comments transmitted by the Chair"), context.get("scope_notice") or outline.get("scope_concern_notice") or _t("原始文书中未单列范围意见。", "No separate scope comments in the original document.")),
        "a": (_t("文章骨架", "Article structure"), context.get("article_skeleton") or outline.get("article_skeleton") or []),
        "d": (_t("建议学科", "Suggested disciplines"), context.get("proposed_disciplines") or outline.get("proposed_disciplines") or []),
        "v": (_t("冻结的原始咨询问题", "Frozen original consultation question"), question),
    }
    if kind == "m":
        modules = outline.get("modules") or []
        if module_index is None or not 0 <= module_index < len(modules):
            raise ValueError("module index is outside frozen outline")
        module = modules[module_index]
        print(f"\n── {module.get('module_id', '?')} · {module.get('title', _t('未命名', 'Untitled'))} " + "─" * 8, file=output)
        for field, label in (
            ("research_questions", _t("研究问题", "Research questions")), ("included_scope", _t("纳入范围", "Included scope")),
            ("excluded_scope", _t("排除范围", "Excluded scope")), ("required_evidence", _t("所需证据", "Required evidence")),
            ("cross_module_links", _t("相邻模块", "Related modules")),
        ):
            values = module.get(field) or []
            if not values:
                continue
            print(label + "：", file=output)
            for index, value in enumerate(values, start=1):
                _outline_wrapped(f"{index}. {value}", output=output, width=width)
        return
    heading, value = sections[kind]
    print(f"\n── {heading} " + "─" * max(4, width - _display_width(heading) - 4), file=output)
    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            _outline_wrapped(f"{index}. {item}", output=output, width=width)
        if not value:
            print("  " + _t("未列出。", "None listed."), file=output)
    else:
        _outline_wrapped(str(value), output=output, width=width)
    if kind == "s":
        print(_t("提示：上述意见不授权代表或主席自行缩减原始任务。", "Note: these comments do not authorize representatives or the Chair to narrow the original task."), file=output)
