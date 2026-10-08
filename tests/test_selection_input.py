import pytest

from project_ensemble.selection_input import parse_number_selection


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 2,3，4、5;6；7", [1, 2, 3, 4, 5, 6, 7]),
        ("3-5 1 - 2", [3, 4, 5, 1, 2]),
        ("1，3-4", [1, 3, 4]),
        ("1-8", None),
        ("3-1", None),
        ("0", None),
        ("1--3", None),
        ("1,a", None),
        ("", None),
    ],
)
def test_parse_number_selection(raw, expected):
    assert parse_number_selection(raw, 7) == expected
