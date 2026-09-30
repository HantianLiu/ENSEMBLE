import json

import pytest

from project_ensemble.domain import DecisionRigor
from project_ensemble.errors import RepresentativeUnavailableError, TransientProviderError
from project_ensemble.orchestration.clause_ballots import (
    ClauseInitialBallotAction,
    ClauseInitialBallotRunner,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


class CapturingNotifier:
    def send_escalation(self, **kwargs):
        return True


class ExhaustingProvider(ScriptedProviderAdapter):
    def generate(self, request):
        if not self._responses:
            raise TransientProviderError("fixture timeout")
        return super().generate(request)


def test_relaxed_high_threshold_changes_first_round_clause_outcome(tmp_path):
    for mode, expected_status in (
        (DecisionRigor.STRICT, "TYPE_I_SECOND_ROUND_REQUIRED"),
        (DecisionRigor.RELAXED, "TYPE_I_ADOPTED_FIRST_ROUND"),
    ):
        repo = MeetingRepository.create(
            tmp_path / mode.value,
            selected_models=[("fake", "m")],
            chair_model=("fake", "m"),
            governance_docs="docs/governance",
            task_description="task",
            decision_rigor=mode,
        )
        engine = MeetingEngine(repo=repo, adapters={}, notifier=CapturingNotifier())
        runner = ClauseInitialBallotRunner(
            repo=repo, engine=engine, governance_docs="docs/governance"
        )
        frozen = {
            "submissions": [
                {"votes": [{"option_set_id": "OS-001", "choice": choice}]}
                for choice in ("SUPPORT", "SUPPORT", "OPPOSE")
            ]
        }
        option_sets = [{
            "option_set_id": "OS-001", "option_set_type": "TYPE_I",
            "proposal_ids": ["CP-001"],
        }]
        record = runner._freeze_outcomes(
            frozen=frozen, option_sets=option_sets, eligible_count=3
        )
        assert record["outcomes"][0]["status"] == expected_status
        assert record["supermajority_required"] == (3 if mode == DecisionRigor.STRICT else 2)


def test_initial_clause_ballot_freezes_complete_window_before_tally(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="task",
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
                    representative_id: "ACTIVE" for representative_id in representative_ids
                },
            }
        ),
    )
    for representative_id in representative_ids:
        repo.docs.write_once(
            f"representatives/{representative_id}/status_transition_001.json",
            json.dumps({"representative_id": representative_id, "status": "ACTIVE"}),
        )
    draft = repo.docs.write_once("public/detailed_clauses/C0.md", "1.1 Clause\n")
    option_sets = repo.docs.write_once(
        "public/detailed_clauses/option_sets.json",
        json.dumps(
            {
                "option_sets": [
                    {
                        "option_set_id": "OS-001",
                        "option_set_type": "TYPE_I",
                        "proposal_ids": ["CP-001"],
                        "options": [{"proposal_id": "CP-001", "text": "Add safeguard"}],
                    }
                ]
            }
        ),
    )
    responses = [
        json.dumps(
            {
                "votes": [
                    {"option_set_id": "OS-001", "choice": choice, "reason": reason}
                ]
            }
        )
        for choice, reason in [
            ("SUPPORT", None),
            ("SUPPORT", None),
            ("SUPPORT", None),
            ("OPPOSE", "The safeguard is underspecified."),
        ]
    ]
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], responses)},
        notifier=CapturingNotifier(),
    )

    result = ClauseInitialBallotRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        max_output_tokens=512,
    ).run(draft_path=draft, option_sets_path=option_sets)

    assert result.ballot_count == 1
    assert result.type_i_direct_adoption_count == 1
    outcomes = json.loads(
        (repo.root / "public/detailed_clauses/initial_ballot_outcomes.json").read_text()
    )
    assert outcomes["eligible_count"] == 4
    assert outcomes["supermajority_required"] == 3
    assert outcomes["outcomes"][0]["tally"] == {"SUPPORT": 3, "OPPOSE": 1}
    assert outcomes["outcomes"][0]["adopted_proposal_id"] == "CP-001"
    assert repo.events.verify()


def test_initial_clause_ballot_resumes_from_durable_ten_item_fragments(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="task",
    )
    registry = json.loads(
        repo.docs.read_text("identity_private/representative_registry.json")
    )
    representative_id = registry[0]["representative_id"]
    statuses = {
        item["representative_id"]: (
            "ACTIVE" if item["representative_id"] == representative_id else "CONSULTATIVE"
        )
        for item in registry
    }
    repo.docs.write_once(
        "identity_private/general_principle/status_transition.json",
        json.dumps(
            {
                "primary_drafter_id": representative_id,
                "representative_statuses": statuses,
            }
        ),
    )
    repo.docs.write_once(
        f"representatives/{representative_id}/status_transition_001.json",
        json.dumps({"representative_id": representative_id, "status": "ACTIVE"}),
    )
    draft = repo.docs.write_once("public/detailed_clauses/C0.md", "1.1 Clause\n")
    records = [
        {
            "option_set_id": f"OS-{number:03d}",
            "option_set_type": "TYPE_I",
            "proposal_ids": [f"CP-{number:03d}"],
            "options": [
                {"proposal_id": f"CP-{number:03d}", "text": f"Proposal {number}"}
            ],
        }
        for number in range(1, 13)
    ]
    option_sets = repo.docs.write_once(
        "public/detailed_clauses/option_sets.json",
        json.dumps({"option_sets": records}),
    )

    def response_for(members):
        return json.dumps(
            {
                "votes": [
                    {
                        "option_set_id": item["option_set_id"],
                        "choice": "SUPPORT",
                        "reason": None,
                    }
                    for item in members
                ]
            }
        )

    first_engine = MeetingEngine(
        repo=repo,
        adapters={
            "fake": ExhaustingProvider("fake", ["m"], [response_for(records[:10])])
        },
        notifier=CapturingNotifier(),
        max_retries=0,
    )
    with pytest.raises(RepresentativeUnavailableError):
        ClauseInitialBallotRunner(
            repo=repo,
            engine=first_engine,
            governance_docs="docs/governance",
            max_output_tokens=512,
        ).run(draft_path=draft, option_sets_path=option_sets)

    partial_root = (
        repo.root
        / "governance_private/detailed_clauses/initial_ballots/partials"
        / representative_id
    )
    assert (partial_root / "IB-001.json").exists()
    assert not (partial_root / "IB-002.json").exists()
    assert not (
        repo.root
        / "governance_private/detailed_clauses/initial_ballots/submissions"
        / f"{representative_id}.json"
    ).exists()

    second_engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], [response_for(records[10:])])},
        notifier=CapturingNotifier(),
    )
    result = ClauseInitialBallotRunner(
        repo=repo,
        engine=second_engine,
        governance_docs="docs/governance",
        max_output_tokens=512,
    ).run(draft_path=draft, option_sets_path=option_sets)

    manifest = json.loads(
        (
            repo.root
            / "public/detailed_clauses/initial_ballot_batches/manifest.json"
        ).read_text()
    )
    complete = ClauseInitialBallotAction.model_validate_json(
        (
            repo.root
            / "governance_private/detailed_clauses/initial_ballots/submissions"
            / f"{representative_id}.json"
        ).read_text()
    )
    assert [len(item["option_set_ids"]) for item in manifest["batches"]] == [10, 2]
    assert len(complete.votes) == 12
    assert result.completed_option_set_count == 12
    assert repo.events.verify()


def test_new_ballot_fragments_enforce_concise_reasons():
    option_sets = [
        {
            "option_set_id": "OS-001",
            "option_set_type": "TYPE_I",
            "proposal_ids": ["CP-001"],
        }
    ]
    with pytest.raises(ValueError, match="must be null"):
        ClauseInitialBallotRunner._validate_concise_reasons(
            ClauseInitialBallotAction.model_validate(
                {
                    "votes": [
                        {
                            "option_set_id": "OS-001",
                            "choice": "SUPPORT",
                            "reason": "Unnecessary essay.",
                        }
                    ]
                }
            ),
            option_sets,
        )
    with pytest.raises(ValueError, match="character limit"):
        ClauseInitialBallotRunner._validate_concise_reasons(
            ClauseInitialBallotAction.model_validate(
                {
                    "votes": [
                        {
                            "option_set_id": "OS-001",
                            "choice": "OPPOSE",
                            "reason": "x" * 241,
                        }
                    ]
                }
            ),
            option_sets,
        )
