import io
import re

from project_ensemble.runtime import consultation_view


def _context():
    return {
        "cycle": 1,
        "objection_number": 5,
        "objection_count": 7,
        "objection": {
            "issue": "断崖式失效超出证据。",
            "current_excerpt": "体内往往断崖式失效。",
            "proposed_wording": "体内可能存在显著落差。",
        },
        "chair_advice": {
            "redraw_excerpt": "体内往往断崖式失效。",
            "suggestion": "建议收窄结论强度。",
        },
    }


def test_science_view_is_two_columns_with_separate_opinion_and_chair_advice(monkeypatch):
    monkeypatch.setattr(consultation_view, "supports_color", lambda _stream: True)
    output = io.StringIO()
    consultation_view.render_science_consultation(
        "HC-SR-SR-012-C001-O005", _context(), output=output,
    )
    rendered = output.getvalue()
    assert "当前稿" in rendered
    assert "建议修改稿（未采纳）" in rendered
    assert "│ 体内" in re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    assert "修正意见" in rendered
    assert "主席建议" in rendered
    assert rendered.index("修正意见") < rendered.index("断崖式失效超出证据")
    assert rendered.index("断崖式失效超出证据") < rendered.index("主席建议")
    assert "\x1b[31m" in rendered
    assert "\x1b[32m" in rendered
    table = [line for line in rendered.splitlines() if line.startswith(("┌", "├", "│", "└"))]
    assert all(
        consultation_view._display_width(re.sub(r"\x1b\[[0-9;]*m", "", line)) == 112
        for line in table
    )


def test_science_view_uses_stacked_layout_on_narrow_terminal(monkeypatch):
    monkeypatch.setattr(consultation_view, "rule_width", lambda *_args, **_kwargs: 60)
    output = io.StringIO()
    consultation_view.render_science_consultation("HC-SR-SR-012-C001-O005", _context(), output=output)
    rendered = output.getvalue()
    assert "当前稿\n" in rendered
    assert "建议修改稿（未采纳）\n" in rendered
    assert "┬" not in rendered


def test_science_view_does_not_render_provider_control_sequences():
    context = _context()
    context["objection"]["issue"] = "问题\x1b[2J仍存在。"
    output = io.StringIO()
    consultation_view.render_science_consultation("HC-SR-SR-012-C001-O005", context, output=output)
    assert "\x1b[2J" not in output.getvalue()
    assert "问题�[2J仍存在。" in output.getvalue()


def test_science_view_identifies_a_replacement_already_in_current_draft():
    context = _context()
    context["objection"]["current_excerpt"] = "体内常存在显著落差。"
    context["objection"]["proposed_wording"] = "改为源支持的「体内常存在显著落差」"
    output = io.StringIO()
    consultation_view.render_science_consultation("HC-SR-SR-012-C001-O005", context, output=output)
    rendered = output.getvalue()
    assert "审阅者建议的这句表述已见于当前稿" in rendered
    assert "改为源支持的" not in rendered.split("修正意见")[0]


def test_science_view_collapses_long_advice_without_discarding_context():
    context = _context()
    context["chair_advice"]["suggestion"] = "建议" * 900
    output = io.StringIO()
    consultation_view.render_science_consultation("HC-SR-LONG", context, output=output)
    rendered = output.getvalue()
    assert "修正意见或主席建议已折叠；按 v 查看完整材料。" in rendered
    assert "建议" * 900 not in rendered


def test_english_science_view_translates_controls_but_preserves_objection(monkeypatch):
    monkeypatch.setattr(consultation_view, "interface_language", lambda: "en")
    output = io.StringIO()
    consultation_view.render_science_consultation("HC-SR-EN", _context(), output=output)
    rendered = output.getvalue()
    assert "Current draft" in rendered
    assert "Proposed wording (not adopted)" in rendered
    assert "Chair's advice" in rendered
    assert "断崖式失效超出证据。" in rendered
    assert "建议收窄结论强度。" in rendered
