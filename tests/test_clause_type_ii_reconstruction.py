import json

from project_ensemble.orchestration.clause_application import ClauseApplicationRunner
from project_ensemble.orchestration.clause_explanations import ClauseExplanationRunner
from project_ensemble.orchestration.clause_type_ii_reconstruction import (
    TypeIINewOptionRunner,
)
from project_ensemble.storage.meeting import MeetingRepository


def _fixture(tmp_path):
    repo = MeetingRepository(tmp_path)
    repo.docs.write_once(
        "public/meeting_manifest.json", json.dumps({"meeting_id": "M-TEST"})
    )
    draft = repo.docs.write_once(
        "public/detailed_clauses/C0.md",
        "1.1 Standing clause text.\n\n1.2 Following clause.\n",
    )
    options = [
        {
            "proposal_id": "CP-A",
            "target_clause_id": "1.1",
            "proposal_type": "REPLACE",
            "text": "Option A.",
        },
        {
            "proposal_id": "CP-B",
            "target_clause_id": "1.1",
            "proposal_type": "REPLACE",
            "text": "Option B.",
        },
    ]
    option_sets = repo.docs.write_once(
        "public/detailed_clauses/option_sets.json",
        json.dumps(
            {
                "meeting_id": "M-TEST",
                "option_sets": [
                    {
                        "option_set_id": "OS-1",
                        "target_clause_id": "1.1",
                        "option_set_type": "TYPE_II",
                        "proposal_ids": ["CP-A", "CP-B"],
                        "options": options,
                    }
                ],
            }
        ),
    )
    unresolved = [
        {
            "option_set_id": "OS-1",
            "proposal_ids": ["CP-A", "CP-B"],
            "options": options,
        }
    ]
    runner = TypeIINewOptionRunner.__new__(TypeIINewOptionRunner)
    runner.repo = repo
    return repo, runner, draft, option_sets, unresolved


def test_qualified_new_option_builds_nonrecursive_public_option_set(tmp_path):
    repo, runner, draft, option_sets, unresolved = _fixture(tmp_path)
    qualified = [
        {
            "proposal_id": "CP2-OS-1-001",
            "proposer_id": "R-TEST",
            "option_set_id": "OS-1",
            "text": "Qualified option.",
            "reason": "Distinct executable alternative.",
            "substantive_gain": "Adds a third bounded choice.",
        }
    ]

    path, reconstructed = runner._freeze_effective_option_sets(
        draft_path=draft,
        option_sets_path=option_sets,
        unresolved=unresolved,
        qualified=qualified,
        raw_affected_ids={"OS-1"},
    )

    record = json.loads(path.read_text())
    rebuilt = reconstructed[0]
    assert record["reconstruction_policy"] == "TYPE_II_NEW_OPTION_RECONSTRUCTION_V1"
    assert rebuilt["option_set_type"] == "TYPE_III"
    assert rebuilt["recursive_new_options_allowed"] is False
    assert rebuilt["proposal_ids"] == [
        "CP-A",
        "CP-B",
        "CP2-OS-1-001",
        "STATUS_QUO-OS-1",
    ]
    assert all("proposer_id" not in option for option in rebuilt["options"])
    assert repo.events.verify()


def test_all_new_options_removed_causes_fresh_original_binary_reballot(tmp_path):
    _repo, runner, draft, option_sets, unresolved = _fixture(tmp_path)

    _path, reconstructed = runner._freeze_effective_option_sets(
        draft_path=draft,
        option_sets_path=option_sets,
        unresolved=unresolved,
        qualified=[],
        raw_affected_ids={"OS-1"},
    )

    rebuilt = reconstructed[0]
    assert rebuilt["proposal_ids"] == ["CP-A", "CP-B"]
    assert rebuilt["reballot_reason"] == "ALL_NEW_OPTIONS_WITHDRAWN_OR_EXCLUDED"
    assert rebuilt["recursive_new_options_allowed"] is False


def test_status_quo_is_a_terminal_outcome_without_an_applied_proposal(tmp_path):
    repo, _runner, _draft, option_sets, _unresolved = _fixture(tmp_path)
    initial = repo.docs.write_once(
        "public/detailed_clauses/initial.json",
        json.dumps(
            {
                "outcomes": [
                    {
                        "option_set_id": "OS-1",
                        "adopted_proposal_id": None,
                        "status": "TYPE_II_EXPLANATION_ROUND_REQUIRED",
                    }
                ]
            }
        ),
    )
    runoff = repo.docs.write_once(
        "public/detailed_clauses/runoff.json", json.dumps({"outcomes": []})
    )
    second = repo.docs.write_once(
        "public/detailed_clauses/second.json",
        json.dumps(
            {
                "outcomes": [
                    {
                        "option_set_id": "OS-1",
                        "adopted_proposal_id": None,
                        "status": "STATUS_QUO_RETAINED",
                    }
                ]
            }
        ),
    )
    application = ClauseApplicationRunner.__new__(ClauseApplicationRunner)
    application.repo = repo

    adopted = application._adopted_proposals(
        option_sets_path=option_sets,
        initial_outcomes_path=initial,
        runoff_outcomes_path=runoff,
        second_outcomes_path=second,
        type_i_revisions_path=None,
        type_i_second_outcomes_path=None,
    )

    assert adopted == []


def test_new_option_public_identifier_does_not_encode_proposer_identity():
    options = ClauseExplanationRunner._new_options(
        frozen_ballots={
            "submissions": [
                {
                    "representative_id": "R-SECRET",
                    "votes": [
                        {
                            "option_set_id": "OS-1",
                            "choice": None,
                            "new_option": {
                                "text": "New text.",
                                "reason": "A distinct option.",
                                "substantive_gain": "Adds a bounded alternative.",
                            },
                        }
                    ],
                }
            ]
        },
        unresolved=[{"option_set_id": "OS-1", "proposal_ids": ["CP-A", "CP-B"]}],
    )

    assert options[0]["proposal_id"] == "CP2-OS-1-001"
    assert "R-SECRET" not in options[0]["proposal_id"]
    assert options[0]["proposer_id"] == "R-SECRET"
