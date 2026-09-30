"""Frozen, presentation-only colour palettes for reader-facing reports."""

from __future__ import annotations

import json
from pathlib import Path


PALETTES = {
    "ocean": {
        "zh": "海蓝", "en": "Ocean",
        "ink": "#24313A", "accent": "#195C78", "mid": "#81AABA",
        "pale": "#EDF4F7", "paper": "#FFFFFF", "line": "#DBE3E8",
    },
    "forest": {
        "zh": "松绿", "en": "Forest",
        "ink": "#26352E", "accent": "#24624A", "mid": "#86A993",
        "pale": "#EFF5F0", "paper": "#FFFFFF", "line": "#D9E4DB",
    },
    "plum": {
        "zh": "梅紫", "en": "Plum",
        "ink": "#332C39", "accent": "#704779", "mid": "#B39BBA",
        "pale": "#F6F1F7", "paper": "#FFFFFF", "line": "#E7DDEA",
    },
    "slate": {
        "zh": "石墨灰", "en": "Slate",
        "ink": "#2D3339", "accent": "#455D6A", "mid": "#9AAAB2",
        "pale": "#F1F3F4", "paper": "#FFFFFF", "line": "#DCE1E4",
    },
}
DEFAULT_PALETTE = "ocean"
PALETTE_DOCUMENT = Path("public/report_presentation.json")


def palette_options(language: str = "zh") -> list[tuple[str, str]]:
    """The visible TUI list states the colour quantity and every hex setpoint."""
    return [
        (
            key,
            f"{value['en' if language == 'en' else 'zh']} · "
            f"{'ink' if language == 'en' else '正文'} {value['ink']} → "
            f"{'accent' if language == 'en' else '标题/强调'} {value['accent']} → "
            f"{'mid' if language == 'en' else '中间色'} {value['mid']} → "
            f"{'callout' if language == 'en' else '摘要框'} {value['pale']} → "
            f"{'paper' if language == 'en' else '纸面'} {value['paper']}"
        )
        for key, value in PALETTES.items()
    ]


def read_meeting_palette(root: str | Path) -> str:
    path = Path(root) / PALETTE_DOCUMENT
    if not path.is_file():
        return DEFAULT_PALETTE
    value = json.loads(path.read_text(encoding="utf-8")).get("palette")
    if value not in PALETTES:
        raise ValueError("meeting has an unknown frozen report palette")
    return value
