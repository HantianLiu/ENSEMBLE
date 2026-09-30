from project_ensemble.runtime.structured_output import parse_json_object


def test_parse_json_object_removes_only_presentation_wrapper():
    expected = {"action": "SUPPORT"}
    assert parse_json_object('```json\n{"action":"SUPPORT"}\n```') == expected
    assert parse_json_object('Result follows:\n{"action":"SUPPORT"}') == expected


def test_parse_json_object_preserves_latex_with_unescaped_backslashes():
    assert parse_json_object(
        '```json\n{"body":"$\\kappa_e$ and $\\langle v^2 \\rangle$"}\n```'
    ) == {"body": "$\\kappa_e$ and $\\langle v^2 \\rangle$"}


def test_parse_json_object_does_not_change_valid_json_escapes():
    assert parse_json_object('{"body":"line 1\\nline 2\\t\\\"quoted\\\""}') == {
        "body": 'line 1\nline 2\t"quoted"'
    }
