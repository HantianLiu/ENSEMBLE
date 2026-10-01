"""Readable evidence for Human decisions on fast-workflow science objections."""

from __future__ import annotations

import json
import re
from pathlib import Path

from project_ensemble.interface_language import ui_label
from project_ensemble.runtime.fast_scope_consultation import _side_by_side
from project_ensemble.user_settings import interface_language


def _safe_json(root: Path, relative: str | None) -> dict | None:
    if not relative:
        return None
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _related_paragraph(body: str, objection: str) -> str:
    paragraphs = [value.strip() for value in re.split(r"\n\s*\n", body) if value.strip()]
    if not paragraphs:
        return "当前修订稿正文为空；请核对冻结文稿。"
    words = set(re.findall(r"[\u3400-\u9fff]{2}|[A-Za-z0-9]{3,}", objection))
    def score(paragraph: str) -> tuple[int, int]:
        return sum(word in paragraph for word in words), -len(paragraph)
    selected = max(paragraphs, key=score)
    if len(selected) <= 900:
        return selected
    matching = next((selected.find(word) for word in words if word in selected), 0)
    start = max(0, matching - 220)
    return ("…" if start else "") + selected[start:start + 900] + (
        "…" if start + 900 < len(selected) else ""
    )


def _review_objections(record: dict) -> list[str]:
    """Read specific objections from either a review checklist or a recheck vote."""
    problems: list[str] = []
    for vote in record.get("votes", []):
        if isinstance(vote, dict):
            problems.extend(
                str(problem).strip()
                for problem in vote.get("remaining_material_problems", [])
                if str(problem).strip()
            )
    for review in record.get("reviews", []):
        if not isinstance(review, dict):
            continue
        checklist = review.get("checklist") or {}
        for issue in checklist.get("issues", []):
            if not isinstance(issue, dict):
                continue
            parts = [
                "位置：" + str(issue.get("location_excerpt", "未提供")),
                "受质疑表述：" + str(issue.get("questioned_claim", "未提供")),
                "问题及影响：" + str(issue.get("why_it_matters", "未提供")),
            ]
            if issue.get("suggested_response"):
                parts.append("建议处理：" + str(issue["suggested_response"]))
            problems.append("；".join(parts))
        problems.extend(
            "术语表修订意见：" + str(problem).strip()
            for problem in checklist.get("glossary_corrections", [])
            if str(problem).strip()
        )
    return list(dict.fromkeys(problem for problem in problems if problem))


def render_fast_science_consultation(repo, issue, *, output) -> None:
    """Show the current frozen wording against every unresolved objection."""
    lang = interface_language() or "zh"
    t = lambda zh, en: ui_label(zh, en, lang)
    root = repo.root.resolve()
    module_id = str(issue.context.get("module_id", ""))
    recheck_relative = issue.context.get("recheck_path")
    recheck = _safe_json(root, recheck_relative) or {}
    match = re.search(r"_v(\d+)\.json$", str(recheck_relative))
    revision = int(match.group(1)) if match else 2
    draft_relative = (
        f"public/literature_report/modules/{module_id}/writing_v071/"
        f"writer_v{revision}_validated.json"
    )
    draft = _safe_json(root, draft_relative) or {}
    body = str((draft.get("draft") or {}).get("body_markdown") or "")
    problems = _review_objections(recheck)
    patch_failure = str(issue.context.get("last_problem", "")).strip()
    if patch_failure:
        print(t("┌─ 局部修稿遇到定位/格式问题 · 当前稿与原审阅意见 ───────────",
                "┌─ Local patch could not be applied · draft and review points ─"), file=output)
        print(t(f"│ {issue.issue_id} · 主笔未能把补丁准确应用到正文",
                f"│ {issue.issue_id} · writer could not apply the patch exactly"), file=output)
        if "old_text must occur exactly once" in patch_failure:
            explanation = (
                "模型给出的待替换原句无法在可编辑文本中唯一定位（可能未出现或出现多次）；"
                "这表示补丁没有应用，不表示科学审阅意见为空。"
            )
        else:
            explanation = "局部补丁未通过机械校验；这不是科学复核已通过或异议已消失。"
        print(t(f"│ 技术原因：{explanation}（{patch_failure}）",
                f"│ Patch validation: {explanation} ({patch_failure})"), file=output)
        print(t(
            "│ 选择局部修稿会重新生成补丁，不会重放这次失败的输出；正文按编号段落定位，术语按词名和释义／公式栏定位。",
            "│ Retrying creates a fresh patch; it will not replay this failed output. Body edits use paragraph numbers, glossary edits use term and field.",
        ), file=output)
        print(t(f"│ 原审阅意见：{len(problems)} 条",
                f"│ Original review points: {len(problems)}"), file=output)
    else:
        print(t("┌─ 科学异议 · 当前稿与反对意见 ─────────────────────────────",
                "┌─ Scientific objections · current draft versus objections ───"), file=output)
        print(t(f"│ {issue.issue_id} · 当前修订稿第 {revision} 版；未解决异议 {len(problems)} 条",
                f"│ {issue.issue_id} · current revision {revision}; {len(problems)} unresolved objections"), file=output)
    if not problems:
        message = (
            "当前复核记录未列出具体问题；请勿据此推定科学异议已经消失。"
            if not patch_failure
            else "原审阅文件中没有可展开的具体异议，请核对审阅记录后再决定是否重试。"
        )
        print(t("│ " + message, "│ " + message), file=output)
    for index, problem in enumerate(problems, 1):
        print(t(f"│ 异议 {index}/{len(problems)}", f"│ Objection {index}/{len(problems)}"), file=output)
        _side_by_side(
            _related_paragraph(body, problem) if body else
            t("当前修订稿片段未能读取；请检查下列原始文件。", "Current draft unavailable; inspect the source path below."),
            problem,
            output=output,
            left_heading=t("当前稿相关表述", "Current draft wording"),
            right_heading=t("未解决的科学异议", "Unresolved science objection"),
        )
    print(t(f"│ 当前稿：{draft_relative}", f"│ Current draft: {draft_relative}"), file=output)
    print(t(f"│ 复核记录：{recheck_relative}", f"│ Recheck record: {recheck_relative}"), file=output)
