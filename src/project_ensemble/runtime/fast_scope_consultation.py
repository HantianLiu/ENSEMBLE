"""Itemized, resumable Human review of fast-workflow module scope questions."""

from __future__ import annotations

import json
import hashlib
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_ensemble.interface_language import ui_label
from project_ensemble.orchestration.consultations import HumanConsultationService
from project_ensemble.user_settings import interface_language


def _ui(chinese: str, english: str) -> str:
    return ui_label(chinese, english, interface_language() or "zh")


class WriterScopeRuling(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["KEEP", "CHANGE"]
    rationale: str = Field(min_length=1, max_length=1000)
    new_scope: str | None = Field(default=None, max_length=1600)

    @model_validator(mode="after")
    def change_needs_wording(self) -> "WriterScopeRuling":
        if self.decision == "CHANGE" and not self.new_scope:
            raise ValueError("a scope change needs concrete new wording")
        if self.decision == "KEEP" and self.new_scope:
            raise ValueError("a keep ruling cannot change scope")
        return self


def scope_items(issue) -> list[dict]:
    items = [
        {"question": str(question), "proposal": None}
        for question in issue.context.get("questions", []) if str(question).strip()
    ]
    proposal = issue.context.get("proposed_scope_change")
    if isinstance(proposal, str) and proposal.strip():
        items.append({"question": "主笔提出的局部范围变更", "proposal": proposal.strip()})
    return items


def _record_path(issue_id: str, index: int, suffix: str) -> Path:
    return Path("human_private/consultations") / f"{issue_id}.item-{index:02d}.{suffix}.json"


_STANDING_DELEGATION = Path("human_private/consultations/fast_scope_writer_standing_delegation.json")


def _standing_writer_delegation(repo) -> bool:
    path = repo.root / _STANDING_DELEGATION
    if not path.is_file():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("authority") == "HUMAN" and payload.get("scope") == "FUTURE_FAST_SCOPE_ITEMS_IN_THIS_MEETING"


def _save_standing_writer_delegation(repo, issue_id: str) -> None:
    payload = {
        "authority": "HUMAN",
        "scope": "FUTURE_FAST_SCOPE_ITEMS_IN_THIS_MEETING",
        "starting_issue_id": issue_id,
        "effect": "WRITER_DECIDES_EACH_ITEM;APPROVED_TASKBOOK_REMAINS_SOURCE",
    }
    repo.docs.write_once(_STANDING_DELEGATION, json.dumps(payload, ensure_ascii=False, indent=2))
    repo.events.append("FAST_SCOPE_WRITER_STANDING_DELEGATION_GRANTED", payload, actor="HUMAN")


def _effective_decision_path(repo, issue_id: str, index: int) -> Path | None:
    prefix = f"{issue_id}.item-{index:02d}"
    folder = repo.root / "human_private/consultations"
    withdrawal_count = len(list(folder.glob(f"{prefix}.withdrawal-*.json")))
    suffix = ("decision" if withdrawal_count == 0 else
              f"decision-revision-{withdrawal_count:02d}")
    path = repo.root / _record_path(issue_id, index, suffix)
    return path if path.is_file() else None


def withdraw_fast_scope_item(repo, issue_id: str, index: int, *, reason: str) -> Path:
    """Void the effective decision without deleting its immutable audit record."""
    if not reason.strip():
        raise ValueError("withdrawal needs a Human reason")
    service = HumanConsultationService(repo)
    issue = next((item for item in service.open_issues() if item.issue_id == issue_id), None)
    if issue is None or issue.stage != "FAST_SCOPE_QUESTION":
        raise ValueError("only an open fast scope consultation can be corrected")
    if not 1 <= index <= len(scope_items(issue)):
        raise ValueError("scope item number is outside this consultation")
    decision_path = _effective_decision_path(repo, issue_id, index)
    if decision_path is None:
        raise ValueError("this scope item has no effective decision to withdraw")
    prefix = f"{issue_id}.item-{index:02d}"
    folder = repo.root / "human_private/consultations"
    number = len(list(folder.glob(f"{prefix}.withdrawal-*.json"))) + 1
    relative = _record_path(issue_id, index, f"withdrawal-{number:02d}")
    payload = {
        "issue_id": issue_id, "item_number": index,
        "withdrawn_decision_path": str(decision_path.relative_to(repo.root)),
        "withdrawn_decision_sha256": hashlib.sha256(decision_path.read_bytes()).hexdigest(),
        "reason": reason.strip(), "authority": "HUMAN",
        "effect": "PREVIOUS_DECISION_VOID; ASK_ITEM_AGAIN",
    }
    repo.docs.write_once(relative, json.dumps(payload, ensure_ascii=False, indent=2))
    repo.docs.write_once(
        Path("public/procedural_consultations") / f"{prefix}.withdrawal-{number:02d}.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    repo.events.append("FAST_SCOPE_ITEM_WITHDRAWN", payload, actor="HUMAN")
    return repo.root / relative


def read_scope_decisions(repo, issue_id: str, item_count: int) -> list[dict] | None:
    decisions = []
    for index in range(1, item_count + 1):
        path = _effective_decision_path(repo, issue_id, index)
        if path is None:
            return None
        decisions.append(json.loads(path.read_text(encoding="utf-8")))
    return decisions


def unique_scope_changes(decisions: list[dict]) -> list[str]:
    """Apply byte-equivalent Human scope changes only once, preserving audit rows."""
    seen: set[str] = set()
    changes: list[str] = []
    for item in decisions:
        if item.get("decision") != "CHANGE" or not item.get("new_scope"):
            continue
        wording = str(item["new_scope"])
        key = " ".join(wording.split())
        if key not in seen:
            seen.add(key)
            changes.append(wording)
    return changes


def _approved_module(repo, module_id: str) -> dict:
    path = repo.root / "public/literature_report/fast/approved_taskbook.json"
    if not path.is_file():
        return {}
    taskbook = json.loads(path.read_text(encoding="utf-8"))
    for module in taskbook.get("outline", {}).get("modules", []):
        if module.get("module_id") == module_id:
            return module
    return {}


def _wrap(text: str, width: int) -> list[str]:
    lines = []
    for raw in text.splitlines() or [""]:
        line = ""
        length = 0
        for character in raw:
            size = 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
            if length + size > width:
                lines.append(line)
                line, length = "", 0
            line += character
            length += size
        lines.append(line)
    return lines


def _pad(text: str, width: int) -> str:
    used = sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
               for char in text)
    return text + " " * max(0, width - used)


def _side_by_side(
    left: str, right: str, *, output,
    left_heading: str | None = None, right_heading: str | None = None,
) -> None:
    # Fixed widths make the comparison legible even when a long Chinese sentence wraps.
    width = 46
    left_lines, right_lines = _wrap(left, width), _wrap(right, width)
    print("┌" + "─" * 48 + "┬" + "─" * 48 + "┐", file=output)
    print(f"│ {_pad(left_heading or _ui('已批准范围', 'Approved scope'), width)} │ "
          f"{_pad(right_heading or _ui('拟议范围', 'Proposed scope'), width)} │", file=output)
    for index in range(max(len(left_lines), len(right_lines))):
        a = left_lines[index] if index < len(left_lines) else ""
        b = right_lines[index] if index < len(right_lines) else ""
        print(f"│ {_pad(a, width)} │ {_pad(b, width)} │", file=output)
    print("└" + "─" * 48 + "┴" + "─" * 48 + "┘", file=output)


def _writer_scope_advice(repo, issue, index: int, item: dict, approved: dict,
                         *, engine, max_output_tokens) -> WriterScopeRuling:
    relative = _record_path(issue.issue_id, index, "writer_advice")
    path = repo.root / relative
    if path.is_file():
        return WriterScopeRuling.model_validate_json(path.read_text(encoding="utf-8"))
    request = {
        "module_id": issue.context.get("module_id"),
        "approved_scope": approved,
        "one_scope_question": item["question"],
        "existing_proposal": item["proposal"],
    }
    response = engine.invoke_participant(
        "WRITER", stage=f"fast_scope_advice_item_{index:02d}",
        system_text=(
            "快速文献调研的学术主笔在此只为人类逐条审阅提供非约束性范围建议；"
            "只有人类明确选择交你代裁时，这项建议才成为程序裁定。"
            "不是重新拟定全局任务。阅读已批准范围与唯一一条问题。"
            "判断是否确需改变本模块研究范围；若否，返回 KEEP，解释如何在原范围内研究。"
            "若是，返回 CHANGE，并给出具体、局部、可执行的新范围表述；不得暗中删去人类硬约束。"
            "只返回符合 WriterScopeRuling 的 JSON。"
        ),
        user_text=(json.dumps(request, ensure_ascii=False)
                   + "\n\nTARGET JSON SCHEMA:\n"
                   + json.dumps(WriterScopeRuling.model_json_schema(), ensure_ascii=False)),
        max_output_tokens=max_output_tokens,
    )
    ruling = engine.validate_structured_response(
        "WRITER", response=response, schema_model=WriterScopeRuling,
        stage=f"fast_scope_advice_item_{index:02d}",
        max_output_tokens=max_output_tokens,
    )
    repo.docs.write_once(relative, ruling.model_dump_json(indent=2))
    return ruling


def prompt_fast_scope_consultation(repo, issue, *, input_fn, output,
                                   engine=None, max_output_tokens=None) -> bool:
    """Return True only after every scope item is frozen and the parent issue closes."""
    service = HumanConsultationService(repo)
    items = scope_items(issue)
    approved = _approved_module(repo, str(issue.context.get("module_id", "")))
    if not items:
        return False
    old_scope = "\n".join([
        _ui("模块：", "Module: ") + str(approved.get('title', issue.context.get('module_id', ''))),
        _ui("纳入：", "Included: ") + "；".join(approved.get("included_scope") or approved.get("research_questions") or [_ui("见已批准任务书", "See approved task brief")]),
        _ui("排除：", "Excluded: ") + "；".join(approved.get("excluded_scope") or [_ui("无单列排除项", "No separately listed exclusions")]),
    ])
    for index, item in enumerate(items, 1):
        if _effective_decision_path(repo, issue.issue_id, index) is not None:
            print(_ui(f"已恢复第 {index}/{len(items)} 条范围决定；不重复询问。",
                      f"Restored scope decision {index}/{len(items)}; not asking again."), file=output)
            continue
        withdrawal_count = len(list((repo.root / "human_private/consultations").glob(
            f"{issue.issue_id}.item-{index:02d}.withdrawal-*.json"
        )))
        if withdrawal_count:
            print(_ui(f"第 {index} 条此前的决定已撤回；请重新回答。",
                      f"The earlier decision for item {index} was withdrawn; please answer again."),
                  file=output)
        authorization_path = _record_path(issue.issue_id, index, "delegation")
        delegated = (
            ((repo.root / authorization_path).is_file() or _standing_writer_delegation(repo))
            and not (repo.root / _record_path(issue.issue_id, index, "delegation_override")).is_file()
        )
        print(_ui(f"\n┌─ {issue.context.get('module_id', '')} · 范围问题 {index}/{len(items)} ──",
                  f"\n┌─ {issue.context.get('module_id', '')} · scope item {index}/{len(items)} ──"), file=output)
        print(_ui("问题：", "Question: ") + item["question"], file=output)
        proposed = item["proposal"]
        advice = None
        advice_path = repo.root / _record_path(issue.issue_id, index, "writer_advice")
        if not proposed and (engine is not None or advice_path.is_file()):
            try:
                advice = _writer_scope_advice(repo, issue, index, item, approved,
                                              engine=engine, max_output_tokens=max_output_tokens)
                proposed = (advice.new_scope if advice.decision == "CHANGE" else
                            _ui("学术主笔建议维持原范围：", "Writer recommends keeping the approved scope: ") + advice.rationale)
            except Exception as exc:
                print(_ui(f"学术主笔暂未给出拟议范围（{exc}）；你仍可直接决定。",
                          f"Writer advice is unavailable ({exc}); you can still decide directly."), file=output)
        proposed = proposed or _ui(
            "尚无具体修改稿。这是一条待裁定的问题，并非已获批准的范围变更。",
            "No concrete revision yet. This is an open question, not an approved scope change.",
        )
        suggested_decision = (advice.decision if advice is not None else
                              "CHANGE" if item["proposal"] else None)
        suggested_scope = (advice.new_scope if advice is not None else item["proposal"])
        _side_by_side(old_scope, proposed, output=output)
        if not delegated:
            while True:
                print(_ui("1. 修改范围（输入具体改动）  2. 不修改，维持已批准范围  3. 交学术主笔裁定",
                          "1. Change scope (enter wording)  2. Keep approved scope  3. Let the writer decide"), file=output)
                if index > 1:
                    print(_ui("4. 本条已由前一条覆盖；不重复增加范围",
                              "4. Already covered by the previous item; do not add scope twice"), file=output)
                    print(_ui("u. 撤回上一条决定并重新回答",
                              "u. Withdraw the previous answer and answer it again"), file=output)
                if suggested_decision:
                    print(_ui(
                        "5. 直接采纳主笔建议（维持已批准范围）" if suggested_decision == "KEEP"
                        else "5. 直接采纳右栏拟议范围（无须重新输入）",
                        "5. Accept the writer's suggestion (keep approved scope)"
                        if suggested_decision == "KEEP"
                        else "5. Accept the proposed scope shown on the right (no retyping)",
                    ), file=output)
                try:
                    choice = input_fn(_ui(
                        "选择菜单编号（也可直接输入修改文字）；回车保持暂停: ",
                        "Choose a menu number (or enter revised wording); Enter keeps meeting paused: ",
                    )).strip()
                except (EOFError, KeyboardInterrupt):
                    return False
                if choice == "":
                    return False
                if choice.lower() == "u" and index > 1:
                    withdraw_fast_scope_item(
                        repo, issue.issue_id, index - 1,
                        reason="人类在连续设问中撤回上一条范围决定，准备重新回答",
                    )
                    return prompt_fast_scope_consultation(
                        repo, issue, input_fn=input_fn, output=output, engine=engine,
                        max_output_tokens=max_output_tokens,
                    )
                allowed = {"1", "2", "3"} | ({"4"} if index > 1 else set())
                if suggested_decision:
                    allowed.add("5")
                if choice == "1" or (choice not in allowed and len(choice) >= 6
                                     and not choice.isdigit()):
                    if choice == "1":
                        try:
                            new_scope = input_fn(_ui("请具体说明这一条如何修改研究范围: ",
                                                     "Describe the concrete change for this item: ")).strip()
                        except (EOFError, KeyboardInterrupt):
                            return False
                    else:
                        new_scope = choice
                        print(_ui("检测到直接输入的范围说明；将按“修改范围”预览，确认前不会保存。",
                                  "Detected scope wording; previewing it as a change. Nothing is saved before confirmation."), file=output)
                    if not new_scope:
                        print(_ui("尚未提供具体修改；请重新选择。",
                                  "No concrete change supplied; choose again."), file=output)
                        continue
                    excluded = approved.get("excluded_scope") or []
                    if any(str(term).strip() and str(term).strip() in new_scope for term in excluded):
                        print(_ui("提醒：拟议文字涉及原排除项，请确认是否有意扩大范围。",
                                  "Note: the proposed wording mentions an excluded topic; check whether you intend to expand scope."), file=output)
                    _side_by_side(old_scope, new_scope, output=output)
                    try:
                        confirm = input_fn(_ui("确认采用这项修改？[y/N]: ",
                                               "Confirm this change? [y/N]: ")).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        return False
                    if confirm not in {"y", "yes"}:
                        print(_ui("未保存本次修改；请重新选择。",
                                  "This change was not saved; choose again."), file=output)
                        continue
                    decision = {"item_number": index, "question": item["question"],
                                "decision": "CHANGE", "new_scope": new_scope,
                                "authority": "HUMAN", "rationale": "人类逐条修改范围"}
                elif choice == "2":
                    decision = {"item_number": index, "question": item["question"],
                                "decision": "KEEP", "new_scope": None,
                                "authority": "HUMAN", "rationale": "人类决定维持已批准范围"}
                elif choice == "5" and suggested_decision:
                    if suggested_decision == "CHANGE":
                        assert suggested_scope
                        excluded = approved.get("excluded_scope") or []
                        if any(str(term).strip() and str(term).strip() in suggested_scope
                               for term in excluded):
                            print(_ui("提醒：拟议文字涉及原排除项，请确认是否有意扩大范围。",
                                      "Note: this proposal mentions an excluded topic; check whether you intend to expand scope."), file=output)
                        try:
                            confirm = input_fn(_ui("确认直接采纳右栏拟议范围？[y/N]: ",
                                                   "Accept the proposed scope on the right? [y/N]: ")).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            return False
                        if confirm not in {"y", "yes"}:
                            print(_ui("未保存本次建议；请重新选择。",
                                      "The suggestion was not saved; choose again."), file=output)
                            continue
                    decision = {"item_number": index, "question": item["question"],
                                "decision": suggested_decision,
                                "new_scope": suggested_scope if suggested_decision == "CHANGE" else None,
                                "authority": "HUMAN",
                                "rationale": "人类直接采纳主笔建议" if advice is not None
                                else "人类直接采纳右栏拟议范围"}
                elif choice == "4":
                    previous = _effective_decision_path(repo, issue.issue_id, index - 1)
                    if previous is None:
                        print(_ui("前一条尚无有效决定，无法合并；请重新选择。",
                                  "The previous item has no effective decision; choose again."), file=output)
                        continue
                    decision = {"item_number": index, "question": item["question"],
                                "decision": "COVERED_BY_PREVIOUS", "new_scope": None,
                                "covered_by_item": index - 1,
                                "authority": "HUMAN", "rationale": "人类确认本条已由前项覆盖"}
                elif choice == "3":
                    if engine is None:
                        print(_ui("当前入口无法调用学术主笔；请重新选择或回车保持暂停。",
                                  "The writer is unavailable here; choose again or press Enter to pause."), file=output)
                        continue
                    try:
                        duration = input_fn(_ui(
                            "代裁范围：1. 仅本条  2. 本会议此后所有模块执行单范围异议均由主笔代裁（回车默认仅本条）: ",
                            "Delegation: 1. This item only  2. All later fast module scope items in this meeting (Enter: this item): ",
                        )).strip()
                    except (EOFError, KeyboardInterrupt):
                        return False
                    if duration not in {"", "1", "2"}:
                        print(_ui("请输入 1 或 2；尚未保存代裁授权。",
                                  "Enter 1 or 2; delegation was not saved."), file=output)
                        continue
                    if duration == "2":
                        _save_standing_writer_delegation(repo, issue.issue_id)
                    repo.docs.write_once(authorization_path, json.dumps({
                        "issue_id": issue.issue_id, "item_number": index,
                        "authority": "HUMAN_DELEGATED_TO_WRITER", "question": item["question"],
                    }, ensure_ascii=False, indent=2))
                    delegated = True
                else:
                    print(_ui("请输入菜单编号，或直接输入具体范围说明；本题仍在等待回答。",
                              "Enter a menu number or concrete scope wording; this item is still awaiting an answer."), file=output)
                    continue
                break
        if delegated:
            while True:
                failure = None
                if engine is None:
                    failure = _ui("当前入口无法调用学术主笔", "The writer is unavailable in this entry point")
                else:
                    try:
                        ruling = _writer_scope_advice(
                            repo, issue, index, item, approved,
                            engine=engine, max_output_tokens=max_output_tokens,
                        )
                    except Exception as exc:
                        failure = f"{type(exc).__name__}: {exc}"
                if failure is None:
                    print(_ui("学术主笔裁定：", "Writer's ruling: ")
                          + f"{ruling.decision}；{ruling.rationale}", file=output)
                    if ruling.new_scope:
                        _side_by_side(old_scope, ruling.new_scope, output=output)
                    decision = {"item_number": index, "question": item["question"],
                                "decision": ruling.decision, "new_scope": ruling.new_scope,
                                "authority": "HUMAN_DELEGATED_TO_WRITER",
                                "rationale": ruling.rationale}
                    break
                print(_ui(f"学术主笔未能裁决第 {index} 条：{failure}",
                          f"The writer could not rule on item {index}: {failure}"), file=output)
                print(_ui("1. 重试主笔代裁  2. 改由人类裁决本条  3. 保持暂停" if engine is not None
                          else "1. 改由人类裁决本条  2. 保持暂停",
                          "1. Retry writer  2. Let the Human decide this item  3. Pause" if engine is not None
                          else "1. Let the Human decide this item  2. Pause"), file=output)
                try:
                    recovery = input_fn(_ui("选择排障方式: ", "Choose a recovery action: ")).strip()
                except (EOFError, KeyboardInterrupt):
                    return False
                if engine is not None and recovery == "1":
                    continue
                if recovery == ("2" if engine is not None else "1"):
                    while True:
                        try:
                            human_choice = input_fn(_ui(
                                "人类裁决：1. 维持已批准范围  2. 修改范围  b. 返回排障菜单: ",
                                "Human decision: 1. Keep approved scope  2. Change scope  b. Back: ",
                            )).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            return False
                        if human_choice == "b":
                            break
                        if human_choice not in {"1", "2"}:
                            print(_ui("请输入 1、2 或 b。", "Enter 1, 2, or b."), file=output)
                            continue
                        new_scope = None
                        if human_choice == "2":
                            try:
                                new_scope = input_fn(_ui("请写明具体范围变更: ",
                                                         "Enter the concrete scope change: ")).strip()
                            except (EOFError, KeyboardInterrupt):
                                return False
                            if not new_scope:
                                print(_ui("范围变更不能为空。", "The scope change cannot be empty."), file=output)
                                continue
                        repo.docs.write_once(
                            _record_path(issue.issue_id, index, "delegation_override"),
                            json.dumps({"reason": failure, "authority": "HUMAN",
                                        "item_number": index}, ensure_ascii=False, indent=2),
                        )
                        decision = {"item_number": index, "question": item["question"],
                                    "decision": "KEEP" if human_choice == "1" else "CHANGE",
                                    "new_scope": new_scope, "authority": "HUMAN",
                                    "rationale": "主笔代裁失败后由人类直接裁决"}
                        break
                    if human_choice == "b":
                        continue
                    break
                if recovery == ("3" if engine is not None else "2") or recovery == "":
                    print(_ui("本条保持暂停；已落盘的代裁授权仍有效，下次可重试或改由人类裁决。",
                              "This item remains paused; the saved delegation can be retried or overridden by the Human."), file=output)
                    return False
                print(_ui("请输入列出的编号。", "Enter one of the listed numbers."), file=output)
        suffix = ("decision" if withdrawal_count == 0 else
                  f"decision-revision-{withdrawal_count:02d}")
        repo.docs.write_once(_record_path(issue.issue_id, index, suffix),
                             json.dumps(decision, ensure_ascii=False, indent=2))
        print(_ui(f"第 {index}/{len(items)} 条决定已保存。",
                  f"Saved decision {index}/{len(items)}."), file=output)
    decisions = read_scope_decisions(repo, issue.issue_id, len(items))
    assert decisions is not None
    all_delegated = all(
        item.get("authority") == "HUMAN_DELEGATED_TO_WRITER" for item in decisions
    )
    print(_ui("\n┌─ 范围决定总览 · 主笔代裁自动提交 ──" if all_delegated
              else "\n┌─ 范围决定总览 · 尚未提交 ──",
              "\n┌─ Scope decisions · writer rulings submit automatically ──" if all_delegated
              else "\n┌─ Scope decisions · not submitted yet ──"), file=output)
    seen_changes: dict[str, int] = {}
    for item in decisions:
        if item["decision"] == "COVERED_BY_PREVIOUS":
            wording = _ui(f"与第 {item['covered_by_item']} 条合并，不重复增加范围",
                          f"Covered by item {item['covered_by_item']}; no duplicate scope change")
        else:
            wording = item.get("new_scope") or _ui("维持已批准范围", "Keep approved scope")
            if item.get("new_scope"):
                key = " ".join(str(item["new_scope"]).split())
                if key in seen_changes:
                    wording = _ui(f"与第 {seen_changes[key]} 条变更文字相同；执行时只应用一次",
                                  f"Identical to item {seen_changes[key]}; applied only once")
                else:
                    seen_changes[key] = item["item_number"]
        print(f"{item['item_number']}. {item['question']} → {wording}", file=output)
    if all_delegated:
        final_choice = "1"
        print(_ui("已获主笔代裁授权；以上裁定自动落盘并继续，无需人类再次确认。",
                  "Writer delegation is active; these rulings are saved automatically without another Human confirmation."), file=output)
    else:
        while True:
            try:
                final_choice = input_fn(_ui(
                    "输入 1 提交整组决定；u 撤回最后一条；回车保持暂停: ",
                    "Enter 1 to submit all decisions; u to withdraw the last; Enter to pause: ",
                )).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            if final_choice in {"", "1", "u"}:
                break
            print(_ui("请输入 1 或 u；整组决定尚未提交，请继续选择。",
                      "Enter 1 or u; the decisions are not submitted yet, so choose again."), file=output)
    if final_choice == "u":
        withdraw_fast_scope_item(
            repo, issue.issue_id, len(items),
            reason="人类在提交整组范围决定前撤回最后一条，准备重新回答",
        )
        return prompt_fast_scope_consultation(
            repo, issue, input_fn=input_fn, output=output, engine=engine,
            max_output_tokens=max_output_tokens,
        )
    if final_choice != "1":
        print(_ui("整组决定尚未提交；会议保持暂停。",
                  "The decisions were not submitted; the meeting stays paused."), file=output)
        return False
    effective_changes = unique_scope_changes(decisions)
    changed = bool(effective_changes)
    public_relative = (Path("public/procedural_consultations")
                       / f"{issue.issue_id}.itemized_scope_decisions.json")
    public_payload = {
        "issue_id": issue.issue_id,
        "module_id": issue.context.get("module_id"),
        "item_decisions": decisions,
        "effective_scope_changed": changed,
        "effective_scope_changes": effective_changes,
        "note": "逐条决定优先于旧版父咨询的单项兼容选项；原范围和问题保持可追溯。",
    }
    public_path = repo.root / public_relative
    if public_path.is_file():
        if json.loads(public_path.read_text(encoding="utf-8")) != public_payload:
            raise ValueError("frozen itemized scope summary conflicts with saved decisions")
    else:
        repo.docs.write_once(public_relative,
                             json.dumps(public_payload, ensure_ascii=False, indent=2))
    parent_decision = (
        "APPROVE_SCOPE_CHANGE" if changed and "APPROVE_SCOPE_CHANGE" in issue.options
        else "KEEP_APPROVED_SCOPE"
    )
    service.resolve(issue_id=issue.issue_id, decision=parent_decision,
                    rationale=("逐条范围决定已冻结；具体变更及主笔代裁授权见同一咨询编号的 item 决定文件。"
                               "原咨询的单项选项仅用于关闭程序，实际范围以逐条记录为准。"),
                    scope="THIS_CONSULTATION_ONLY")
    print(_ui(f"{len(items)} 条范围问题已逐条处理；会议继续。",
              f"All {len(items)} scope items were decided; the meeting continues."), file=output)
    return True
