import inspect
import json
from types import SimpleNamespace

import pytest

from project_ensemble.orchestration.clause_application import ClauseApplicationRunner
from project_ensemble.orchestration.clause_explanations import ClauseExplanationRunner
from project_ensemble.orchestration.clause_review import ClauseReviewRunner
from project_ensemble.storage.meeting import MeetingRepository


def test_review_continuation_flag_belongs_to_application_runner_only():
    assert "continue_into_reviews" in inspect.signature(ClauseApplicationRunner).parameters
    assert "continue_into_reviews" not in inspect.signature(ClauseExplanationRunner).parameters


def test_trial_docket_uses_second_level_clauses_and_keeps_nested_requirements():
    clauses = ClauseReviewRunner._parse_second_level_clauses(
        """Title
0.1 Scope
0.1.1 Nested scope requirement.

1.1 First review item
1.1.1 First internal requirement.
1.1.2 Second internal requirement.

1.2 Second review item
1.2.1 Another internal requirement.
"""
    )

    assert [item["clause_id"] for item in clauses] == ["0.1", "1.1", "1.2"]
    assert "1.1.1 First internal requirement." in clauses[1]["text"]
    assert "1.1.2 Second internal requirement." in clauses[1]["text"]
    assert "1.2 Second review item" not in clauses[1]["text"]


def test_trial_docket_rejects_duplicate_second_level_identifiers():
    with pytest.raises(ValueError, match="must be unique"):
        ClauseReviewRunner._parse_second_level_clauses("1.1 First\n1.1 Duplicate\n")


def test_trial_docket_stops_before_next_article_and_accepts_split_suffixes():
    clauses = ClauseReviewRunner._parse_second_level_clauses(
        """第二条 Inputs
2.1 First
2.2-A Input whitelist
Split body A.
2.2-B NESS definition
Split body B.
第三条 Analysis
3.1 Next article clause
"""
    )

    assert [item["clause_id"] for item in clauses] == ["2.1", "2.2-A", "2.2-B", "3.1"]
    assert "第三条" not in clauses[2]["text"]


def test_split_replacement_preserves_following_article_heading():
    source = """第二条 Inputs
2.1 First
2.2 Combined requirement.
第三条 Analysis
3.1 Next clause.
"""

    result = ClauseReviewRunner._replace_docket_clause(
        source,
        "2.2",
        "2.2-A First part.\n\n2.2-B Second part.",
    )

    assert "2.2 Combined requirement." not in result
    assert "2.2-A First part." in result
    assert "2.2-B Second part." in result
    assert "第三条 Analysis\n3.1 Next clause." in result


def test_adopted_split_creates_rebased_draft_docket_and_reviews(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once(
        "public/meeting_manifest.json",
        json.dumps({"meeting_id": "M-TEST"}),
    )
    draft_relative = "public/detailed_clauses/C0.md"
    docket_relative = "public/detailed_clauses/clause_docket.json"
    reviews_relative = "public/detailed_clauses/active_reviews.json"
    source = """第二条 Inputs
2.1 First.
2.2 Combined requirement.
2.3 Existing next requirement.
第三条 Analysis
3.1 Next article clause.
"""
    repo.docs.write_once(draft_relative, source)
    clauses = ClauseReviewRunner._parse_second_level_clauses(source)
    docket = {
        "meeting_id": "M-TEST",
        "policy_id": "trial",
        "status": "FROZEN",
        "source_draft_path": draft_relative,
        "source_draft_sha256": ClauseReviewRunner._sha256(source),
        "clause_count": len(clauses),
        "clauses": clauses,
    }
    reviews = {
        "meeting_id": "M-TEST",
        "status": "FROZEN",
        "submission_count": 1,
        "proposals": [],
        "proposal_groups": {},
        "split_motions": [],
        "suspension_motions": [],
    }
    repo.docs.write_once(docket_relative, json.dumps(docket))
    repo.docs.write_once(reviews_relative, json.dumps(reviews))
    runner = ClauseReviewRunner.__new__(ClauseReviewRunner)
    runner.repo = repo
    runner.engine = SimpleNamespace(
        progress=SimpleNamespace(status=lambda *args, **kwargs: None)
    )
    adopted = [
        {
            "motion_id": "CS-R-TEST-001",
            "target_clause_id": "2.2",
            "proposed_parts": [
                {"label": "2.2-A Input whitelist", "text": "Only listed inputs."},
                {"label": "2.2-B NESS definition", "text": "Define the steady state."},
            ],
        }
    ]

    draft_path, rebased_reviews_path, rebased_clauses = runner._ensure_split_application(
        draft_path=tmp_path / draft_relative,
        docket_path=tmp_path / docket_relative,
        public_review_path=tmp_path / reviews_relative,
        frozen_reviews=reviews,
        adopted_splits=adopted,
    )

    assert draft_path.name == "C0R1.md"
    assert rebased_reviews_path.name == "active_reviews_rebased_001.json"
    assert [item["clause_id"] for item in rebased_clauses] == [
        "2.1",
        "2.2-A",
        "2.2-B",
        "2.3",
        "3.1",
    ]
    assert (tmp_path / draft_relative).read_text() == source
    rebased_text = draft_path.read_text()
    assert "2.2 Combined requirement." not in rebased_text
    assert "第三条 Analysis\n3.1 Next article clause." in rebased_text
    assert repo.events.verify()
