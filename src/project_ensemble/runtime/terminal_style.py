from __future__ import annotations

import os
import shutil
from project_ensemble.user_settings import appearance
from typing import TextIO


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
LIGHT_GRAY = "\x1b[90m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"


def supports_color(stream: TextIO, override: bool | None = None) -> bool:
    if override is not None:
        return override
    color, _ = appearance()
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if color == "never":
        return False
    if color == "always":
        return True
    try:
        return stream.isatty()
    except (AttributeError, OSError):
        return False


def styled(text: str, *codes: str, enabled: bool) -> str:
    if not enabled or not codes:
        return text
    return "".join(codes) + text + RESET


def rule_width(stream: TextIO, *, maximum: int = 100) -> int:
    _, configured_width = appearance()
    maximum = configured_width if maximum == 100 else maximum
    try:
        fallback = (maximum, 24)
        columns = shutil.get_terminal_size(fallback).columns if stream.isatty() else maximum
    except (AttributeError, OSError):
        columns = maximum
    return max(40, min(columns, maximum))
