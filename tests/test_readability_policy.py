from project_ensemble.orchestration.readability_policy import reader_style_policy


def test_signposting_uses_next_level_only_for_complex_passages():
    policy = reader_style_policy({"signposting_1_to_5": 3}, {"统计物理": 2})
    assert policy["argument_signposting"]["ordinary_level"] == 3
    assert policy["argument_signposting"]["complex_level"] == 4
    assert "大众科普" in policy["discipline_proficiency"]["统计物理"]["instruction"]
    assert "证据标准" in policy["invariants"]


def test_top_signposting_level_does_not_exceed_five():
    policy = reader_style_policy({"signposting_1_to_5": 5})
    assert policy["argument_signposting"]["complex_level"] == 5


def test_old_meetings_receive_named_default_instead_of_bare_number():
    policy = reader_style_policy({"segmentation_1_to_5": None})
    assert policy["argument_signposting"]["ordinary_level"] == 4
    assert policy["segmentation"]["instruction"]
