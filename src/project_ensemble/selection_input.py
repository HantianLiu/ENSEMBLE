"""Parse numbered selections in interactive menus."""

from __future__ import annotations

import re


def parse_number_selection(raw: str, maximum: int) -> list[int] | None:
    """Expand inclusive ranges while preserving order; return None for invalid input.

    Whitespace, ASCII/Chinese commas, enumeration commas, and semicolons separate
    menu numbers. A hyphen between two numbers denotes an inclusive range.
    Duplicate handling belongs to the menu using this parser.
    """
    if maximum < 1:
        return None
    normalized = re.sub(r"(?<=[0-9])\s*-\s*(?=[0-9])", "-", raw.strip())
    tokens = [token for token in re.split(r"[\s,，、;；]+", normalized) if token]
    if not tokens:
        return None

    result: list[int] = []
    for token in tokens:
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", token)
        if match is None:
            return None
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        if not 1 <= start <= end <= maximum:
            return None
        result.extend(range(start, end + 1))
    return result
