from __future__ import annotations

import os
import shutil
from pathlib import Path
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
SOFT_BLUE = "\x1b[38;5;110m"
SOFT_GREEN = "\x1b[38;5;108m"


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


def format_directory_path(path: str | Path, *, compact: bool = True, color: bool = False) -> str:
    """Show the last three directory levels in lists, with subdued accents."""
    directory = Path(path).expanduser()
    parts = list(directory.parts)
    if directory.anchor:
        parts = parts[1:]
    hidden = max(0, len(parts) - 3) if compact else 0
    prefix = "…" + os.sep if hidden else directory.anchor
    accents = (SOFT_BLUE, SOFT_GREEN)
    separator = styled(os.sep, DIM, enabled=color)
    segments = [
        styled(part, accents[index % len(accents)], enabled=color)
        for index, part in enumerate(parts[hidden:], start=hidden)
    ]
    return styled(prefix, DIM, enabled=color) + separator.join(segments)


def rule_width(stream: TextIO, *, maximum: int = 100) -> int:
    _, configured_width = appearance()
    maximum = configured_width if maximum == 100 else maximum
    try:
        fallback = (maximum, 24)
        columns = shutil.get_terminal_size(fallback).columns if stream.isatty() else maximum
    except (AttributeError, OSError):
        columns = maximum
    return max(40, min(columns, maximum))
