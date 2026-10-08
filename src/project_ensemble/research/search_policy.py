"""Meeting-local search permission: frozen baseline plus Human runtime changes."""

from __future__ import annotations

import json


def _selection(repo) -> tuple[bool, str]:
    """Legacy meetings keep their existing backend policy; no global mutation."""
    if repo is None:
        return True, "tavily"
    permissions: list[bool] = []
    engines: list[str] = []
    for name in ("identity_private/meeting_manifest.json", "public/meeting_manifest.json"):
        path = repo.root / name
        if not path.is_file():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if "general_search_allowed" in record:
            value = record["general_search_allowed"]
            if not isinstance(value, bool):
                raise ValueError("会议通用搜索许可必须为布尔值；拒绝调用通用搜索")
            permissions.append(value)
        engine = record.get("general_search_engine")
        if engine is not None:
            if not isinstance(engine, str) or engine not in {"tavily", "parallel", "disabled"}:
                raise ValueError("会议搜索引擎记录无效；拒绝调用通用搜索")
            engines.append(engine)
    if permissions and any(value != permissions[0] for value in permissions):
        raise ValueError("会议通用搜索许可记录不一致；拒绝调用通用搜索")
    allowed = permissions[0] if permissions else True
    if engines and any(value != engines[0] for value in engines):
        raise ValueError("会议搜索引擎记录不一致；拒绝调用通用搜索")
    engine = engines[0] if engines else "tavily"
    if engine == "disabled":
        allowed, engine = False, "tavily"
    for path in sorted((repo.root / "human_private/runtime_controls").glob("control-*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("kind") == "general_search_allowed":
            value = record.get("value")
            if not isinstance(value, bool) or record.get("authority") != "HUMAN":
                raise ValueError("会议通用搜索开关记录无效；拒绝调用通用搜索")
            allowed = value
        elif record.get("kind") == "general_search_engine":
            value = record.get("value")
            if not isinstance(value, str) or value not in {"tavily", "parallel", "disabled"} or record.get("authority") != "HUMAN":
                raise ValueError("会议搜索引擎变更无效；拒绝调用通用搜索")
            allowed = value != "disabled"
            if allowed:
                engine = value
    return allowed, engine


def general_search_allowed(repo) -> bool:
    return _selection(repo)[0]


def general_search_engine(repo) -> str:
    allowed, engine = _selection(repo)
    return engine if allowed else "disabled"
