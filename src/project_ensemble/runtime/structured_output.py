from __future__ import annotations

import json
import re
from typing import Any


_FENCED_JSON = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse one JSON object, tolerating only non-substantive wrappers."""
    stripped = text.strip()
    candidates = [stripped]
    fenced = _FENCED_JSON.match(stripped)
    if fenced:
        candidates.append(fenced.group("body").strip())
    candidates.extend(_balanced_objects(stripped))

    valid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            repaired = _escape_invalid_string_backslashes(candidate)
            if repaired == candidate:
                continue
            try:
                value = json.loads(repaired)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(value, dict):
            valid.append(value)
    if len(valid) == 1:
        return valid[0]
    if valid and all(item == valid[0] for item in valid[1:]):
        return valid[0]
    if not valid:
        raise ValueError("model output contains no valid JSON object")
    raise ValueError("model output contains multiple distinct JSON objects")


def _escape_invalid_string_backslashes(text: str) -> str:
    r"""Make literal backslashes inside JSON strings JSON-safe.

    Model output frequently contains LaTeX such as ``\kappa`` or ``\langle``.
    Those are meaningful literal backslashes but invalid JSON escapes.  This
    scanner doubles only backslashes inside strings whose following character
    is not one of JSON's defined escape characters.  It does not alter valid
    JSON escapes, text outside strings, field content, or object structure.
    """

    output: list[str] = []
    in_string = False
    in_math = False
    index = 0
    while index < len(text):
        character = text[index]
        if character == '"':
            in_string = not in_string
            if not in_string:
                in_math = False
            output.append(character)
            index += 1
            continue
        if in_string and character == "$":
            run_end = index + 1
            while run_end < len(text) and text[run_end] == "$":
                run_end += 1
            output.append(text[index:run_end])
            in_math = not in_math
            index = run_end
            continue
        if in_string and character == "\\":
            next_character = text[index + 1] if index + 1 < len(text) else ""
            unicode_escape = (
                next_character == "u"
                and len(text[index + 2 : index + 6]) == 4
                and all(item in "0123456789abcdefABCDEF" for item in text[index + 2 : index + 6])
            )
            if next_character in {'"', "\\", "/"} or unicode_escape:
                output.extend((character, next_character))
                index += 2
                continue
            if not in_math and next_character in {"b", "f", "n", "r", "t"}:
                output.extend((character, next_character))
                index += 2
                continue
            output.extend(("\\", character))
            index += 1
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _balanced_objects(text: str) -> list[str]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if start is None:
            if character == "{":
                start = index
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                objects.append(text[start : index + 1])
                start = None
    return objects
