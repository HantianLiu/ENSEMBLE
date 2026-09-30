import json

from project_ensemble.orchestration.clause_runoffs import ClauseTypeIIIRunoffRunner
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


class _Notifier:
    def send_escalation(self, **_kwargs):
        return True


def _fixture(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Resolve a Type III cutoff tie.",
    )
    registry = json.loads(
        repo.docs.read_text("identity_private/representative_registry.json")
    )
    representative_ids = [item["representative_id"] for item in registry]
    repo.docs.write_once(
        "identity_private/general_principle/status_transition.json",
        json.dumps(
            {
                "primary_drafter_id": representative_ids[0],
                "representative_statuses": {
                    representative_id: "ACTIVE"
                    for representative_id in representative_ids
                },
            }
        ),
    )
    for representative_id in representative_ids:
        repo.docs.write_once(
            f"representatives/{representative_id}/status_transition_001.json",
            json.dumps(
                {"representative_id": representative_id, "status": "ACTIVE"}
            ),
        )
    draft = repo.docs.write_once("public/detailed_clauses/C0.md", "1.1 Clause\n")
    options = [
        {
            "proposal_id": proposal_id,
            "target_clause_id": "1.1",
            "proposal_type": "REPLACE",
            "text": text,
        }
        for proposal_id, text in [
            ("CP-A", "Established first-place proposal."),
            ("CP-B", "First tied proposal."),
            ("CP-C", "Second tied proposal."),
        ]
    ]
    option_sets = repo.docs.write_once(
        "public/detailed_clauses/option_sets.json",
        json.dumps(
            {
                "option_sets": [
                    {
                        "option_set_id": "OS-TIE",
                        "target_clause_id": "1.1",
                        "option_set_type": "TYPE_III",
                        "proposal_ids": ["CP-A", "CP-B", "CP-C"],
                        "options": options,
                    }
                ]
            }
        ),
    )
    initial = repo.docs.write_once(
        "public/detailed_clauses/initial_ballot_outcomes.json",
        json.dumps(
            {
                "outcomes": [
                    {
                        "option_set_id": "OS-TIE",
                        "option_set_type": "TYPE_III",
                        "eligible_count": 4,
                        "supermajority_required": 3,
                        "tally": {"CP-A": 2, "CP-B": 1, "CP-C": 1},
                        "top_two_proposal_ids": [],
                        "tied_proposal_ids": ["CP-B", "CP-C"],
                        "status": "TYPE_III_TOP_TWO_TIE_REQUIRES_HUMAN",
                    }
                ]
            }
        ),
    )
    return repo, representative_ids, draft, option_sets, initial


def _vote(option_set_id, choice):
    return json.dumps(
        {"votes": [{"option_set_id": option_set_id, "choice": choice}]}
    )


def test_type_iii_second_place_tie_uses_sealed_majority_tiebreak_then_runoff(
    tmp_path,
):
    repo, representative_ids, draft, option_sets, initial = _fixture(tmp_path)
    responses = [
        *[_vote("OS-TIE", choice) for choice in ["CP-B", "CP-B", "CP-B", "CP-C"]],
        *[_vote("OS-TIE", choice) for choice in ["CP-A", "CP-A", "CP-A", "CP-B"]],
    ]
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], responses)},
        notifier=_Notifier(),
    )

    result = ClauseTypeIIIRunoffRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
    ).run(
        draft_path=draft,
        option_sets_path=option_sets,
        initial_outcomes_path=initial,
    )

    assert result.runoff_ballot_count == 1
    assert result.direct_adoption_count == 1
    assert result.explanation_round_count == 0
    cutoff = json.loads(
        (
            repo.root
            / "public/detailed_clauses/type_iii_cutoff_tiebreak_outcomes.json"
        ).read_text()
    )
    cutoff_outcome = cutoff["outcomes"][0]
    assert cutoff["protective_majority_required"] == 3
    assert cutoff_outcome["secured_first_proposal_id"] == "CP-A"
    assert cutoff_outcome["tiebreak_winner_proposal_id"] == "CP-B"
    assert cutoff_outcome["top_two_proposal_ids"] == ["CP-A", "CP-B"]
    runoff = json.loads(
        (repo.root / "public/detailed_clauses/type_iii_runoff_outcomes.json").read_text()
    )
    assert runoff["supermajority_required"] == 3
    assert runoff["outcomes"][0]["adopted_proposal_id"] == "CP-A"
    assert (
        repo.root
        / "governance_private/detailed_clauses/type_iii_cutoff_tiebreak_policy.json"
    ).exists()
    assert repo.events.verify()


def test_type_iii_cutoff_tiebreak_resume_reuses_complete_ballots(tmp_path):
    repo, representative_ids, draft, option_sets, initial = _fixture(tmp_path)
    submissions = repo.root / (
        "governance_private/detailed_clauses/type_iii_cutoff_tiebreaks/submissions"
    )
    for representative_id, choice in zip(
        representative_ids, ["CP-B", "CP-B", "CP-B", "CP-C"], strict=True
    ):
        repo.docs.write_once(
            submissions.relative_to(repo.root) / f"{representative_id}.json",
            _vote("OS-TIE", choice),
        )
    responses = [
        _vote("OS-TIE", choice) for choice in ["CP-A", "CP-A", "CP-A", "CP-B"]
    ]
    adapter = ScriptedProviderAdapter("fake", ["m"], responses)
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": adapter},
        notifier=_Notifier(),
    )

    result = ClauseTypeIIIRunoffRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
    ).run(
        draft_path=draft,
        option_sets_path=option_sets,
        initial_outcomes_path=initial,
    )

    assert result.direct_adoption_count == 1
    assert len(adapter._responses) == 0
    frozen = json.loads(
        (
            repo.root
            / "governance_private/detailed_clauses/type_iii_cutoff_tiebreaks/frozen.json"
        ).read_text()
    )
    assert frozen["submission_count"] == 4
    assert repo.events.verify()


def test_complex_cutoff_tie_uses_chair_only_after_voter_runoff_remains_tied(
    tmp_path,
):
    repo = MeetingRepository.create(
        tmp_path / "ws-complex",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Resolve a four-way advancement tie.",
    )
    registry = json.loads(
        repo.docs.read_text("identity_private/representative_registry.json")
    )
    representative_ids = [item["representative_id"] for item in registry]
    repo.docs.write_once(
        "identity_private/general_principle/status_transition.json",
        json.dumps(
            {
                "primary_drafter_id": representative_ids[0],
                "representative_statuses": {
                    representative_id: "ACTIVE"
                    for representative_id in representative_ids
                },
            }
        ),
    )
    for representative_id in representative_ids:
        repo.docs.write_once(
            f"representatives/{representative_id}/status_transition_001.json",
            json.dumps(
                {"representative_id": representative_id, "status": "ACTIVE"}
            ),
        )
    draft = repo.docs.write_once("public/detailed_clauses/C0.md", "1.1 Clause\n")
    candidate_ids = ["CP-A", "CP-B", "CP-C", "CP-D"]
    option_sets = repo.docs.write_once(
        "public/detailed_clauses/option_sets.json",
        json.dumps(
            {
                "option_sets": [
                    {
                        "option_set_id": "OS-COMPLEX",
                        "target_clause_id": "1.1",
                        "option_set_type": "TYPE_III",
                        "proposal_ids": candidate_ids,
                        "options": [
                            {
                                "proposal_id": candidate_id,
                                "target_clause_id": "1.1",
                                "proposal_type": "REPLACE",
                                "text": f"Text {candidate_id}.",
                            }
                            for candidate_id in candidate_ids
                        ],
                    }
                ]
            }
        ),
    )
    initial = repo.docs.write_once(
        "public/detailed_clauses/initial_ballot_outcomes.json",
        json.dumps(
            {
                "outcomes": [
                    {
                        "option_set_id": "OS-COMPLEX",
                        "option_set_type": "TYPE_III",
                        "eligible_count": 4,
                        "supermajority_required": 3,
                        "tally": {candidate_id: 1 for candidate_id in candidate_ids},
                        "top_two_proposal_ids": [],
                        "tied_proposal_ids": candidate_ids,
                        "status": "TYPE_III_TOP_TWO_TIE_REQUIRES_HUMAN",
                    }
                ]
            }
        ),
    )
    responses = [
        *[
            _vote("OS-COMPLEX", choice)
            for choice in ["CP-A", "CP-A", "CP-B", "CP-C"]
        ],
        json.dumps(
            {
                "candidate_id": "CP-B",
                "reason": "CP-B receives the procedural deciding vote.",
            }
        ),
        *[
            _vote("OS-COMPLEX", choice)
            for choice in ["CP-A", "CP-A", "CP-A", "CP-B"]
        ],
    ]
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], responses)},
        notifier=_Notifier(),
    )

    result = ClauseTypeIIIRunoffRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
    ).run(
        draft_path=draft,
        option_sets_path=option_sets,
        initial_outcomes_path=initial,
    )

    assert result.direct_adoption_count == 1
    cutoff = json.loads(
        (
            repo.root
            / "public/detailed_clauses/type_iii_cutoff_tiebreak_outcomes.json"
        ).read_text()
    )["outcomes"][0]
    assert cutoff["top_two_proposal_ids"] == ["CP-A", "CP-B"]
    assert cutoff["chair_decisions"] == [
        {
            "candidate_id": "CP-B",
            "reason": "CP-B receives the procedural deciding vote.",
        }
    ]
    assert repo.events.verify()
