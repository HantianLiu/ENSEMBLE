"""The prompt's examples must themselves survive exactly one JSON decode."""
import json

from project_ensemble.orchestration.math_output_prompt import (
    ALIGNED_EXAMPLE, DISPLAY_EXAMPLE, DOUBLE_ESCAPED_NEWLINE_EXAMPLE,
    INLINE_EXAMPLE, MATH_JSON_EXAMPLES, MATH_OUTPUT_FORMAT_RULES,
)
from project_ensemble.orchestration.readability_policy import reader_facing_prose_contract


def test_serialized_display_example_decodes_to_markdown_with_real_newlines():
    wire = MATH_JSON_EXAMPLES["display"]
    body = json.loads(wire)["body_markdown"]
    assert body == DISPLAY_EXAMPLE
    assert "\n$$\n" in body
    assert r"\n" not in body
    assert r"\beta" in body
    assert r"\\beta" in wire
    assert not any(c in body for c in ("\b", "\r", "\f", "\t"))
    assert body.splitlines().count("$$") == 2


def test_inline_example_preserves_math_commands_and_delimiters():
    body = json.loads(MATH_JSON_EXAMPLES["inline"])["short_summary"]
    assert body == INLINE_EXAMPLE
    assert r"\(x\)" in body
    assert r"\(\beta\)" in body
    assert not any(c in body for c in ("\b", "\r", "\f", "\t"))


def test_aligned_example_distinguishes_tex_linebreak_from_markdown_newline():
    wire = MATH_JSON_EXAMPLES["aligned"]
    formula = json.loads(wire)["formula"]
    assert formula == ALIGNED_EXAMPLE
    assert r"\begin{aligned}" in formula
    assert r"x &= y+z \\" in formula
    assert r"\\" in formula
    assert r"\\\\" in wire
    assert "\n" in formula
    assert r"\n" not in formula
    assert formula.splitlines().count("$$") == 2
    assert not any(c in formula for c in ("\b", "\r", "\f", "\t"))


def test_wrong_example_explicitly_demonstrates_overescaped_newlines():
    body = json.loads(DOUBLE_ESCAPED_NEWLINE_EXAMPLE)["body_markdown"]
    assert "\n" not in body
    assert r"\n" in body
    assert body != DISPLAY_EXAMPLE
    assert DOUBLE_ESCAPED_NEWLINE_EXAMPLE in MATH_OUTPUT_FORMAT_RULES
    assert "错误：下面的换行被多编码了一次" in MATH_OUTPUT_FORMAT_RULES


def test_local_replacement_uses_same_encoding_rules():
    body = json.loads(MATH_JSON_EXAMPLES["replacement"])["new_text"]
    assert body == DISPLAY_EXAMPLE


def test_reader_contract_contains_complete_math_rules_and_scope_boundaries():
    contract = reader_facing_prose_contract()
    assert MATH_OUTPUT_FORMAT_RULES in contract
    assert "只在当前获准修改的片段" in contract
    assert "不改变来源、科学内容或修订权限" in contract
    assert "LaTeX 命令的单个反斜杠编码为两个反斜杠" in contract
    for wire in MATH_JSON_EXAMPLES.values():
        assert wire in contract
