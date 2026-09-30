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
    problems = [
        str(problem).strip()
        for vote in recheck.get("votes", []) if isinstance(vote, dict)
        for problem in vote.get("remaining_material_problems", [])
        if str(problem).strip()
    ]
    # A final-round record may omit the original checklist but must still
    # display all concrete remaining objections from the current vote.
    problems = list(dict.fromkeys(problems))
    print(t("┌─ 科学异议 · 当前稿与反对意见 ─────────────────────────────",
            "┌─ Scientific objections · current draft versus objections ───"), file=output)
    print(t(f"│ {issue.issue_id} · 当前修订稿第 {revision} 版；未解决异议 {len(problems)} 条",
            f"│ {issue.issue_id} · current revision {revision}; {len(problems)} unresolved objections"), file=output)
    if not problems:
        print(t("│ 当前复核记录未列出具体剩余问题；请勿据此推定科学异议已经消失。",
                "│ The current record lists no specific remaining problem; do not infer that the objection was resolved."),
              file=output)
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
