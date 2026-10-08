"""Appearance assets are self-contained and never change the report identity."""
from pathlib import Path

from project_ensemble.orchestration.academic_html import render_academic_review_html


def test_focus_asset_and_palette_controls_embedded_without_external_dependency():
    output = render_academic_review_html("# 测试\n\n一句正文。\n", meeting_id="LR-FOCUS")
    assert 'id="reader-focus-toggle" aria-pressed="false"' in output
    assert '<option value="butter">浅黄 · 奶油纸</option>' in output
    assert "window.EnsembleReaderFocus" in output
    assert "ensemble-reader:appearance-v1" in output
    assert "prefers-reduced-motion:reduce" in output
    assert "reader_focus.js" not in output  # Inline: a standalone HTML file.


def test_focus_asset_is_in_wheel_package_data():
    config = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert '"assets/reader_focus.js"' in config.read_text(encoding="utf-8")
