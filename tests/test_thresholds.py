from project_ensemble.domain import DecisionRigor
from project_ensemble.governance_private.thresholds import (
    high_threshold,
    high_threshold_formula,
    supermajority_threshold,
    strict_majority_threshold,
)
from project_ensemble.storage.meeting import MeetingRepository


def test_thresholds():
    assert supermajority_threshold(12) == 9
    assert supermajority_threshold(3) == 3
    assert strict_majority_threshold(12) == 7
    assert strict_majority_threshold(3) == 2


def test_meeting_high_threshold_uses_frozen_human_choice(tmp_path):
    for mode, expected, formula in (
        (DecisionRigor.STRICT, 9, "ceil(3*N_ACTIVE/4)"),
        (DecisionRigor.RELAXED, 7, "floor(N_ACTIVE/2)+1"),
    ):
        repo = MeetingRepository.create(
            tmp_path / mode.value,
            selected_models=[("fake", "m")],
            chair_model=("fake", "m"),
            governance_docs="docs/governance",
            task_description="Test threshold policy.",
            decision_rigor=mode,
        )
        assert high_threshold(repo, 12) == expected
        assert high_threshold_formula(repo) == formula
        assert repo.events.verify()


def test_legacy_meeting_without_decision_mode_keeps_three_quarter_threshold():
    class LegacyDocs:
        def read_text(self, _path):
            return '{"meeting_id":"M-LEGACY"}'

    class LegacyRepo:
        docs = LegacyDocs()

    assert high_threshold(LegacyRepo(), 12) == 9
