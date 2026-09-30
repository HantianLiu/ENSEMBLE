import json
import threading
import time
from pathlib import Path

from project_ensemble.orchestration.clause_application import ClauseApplicationRunner
from project_ensemble.orchestration.clause_type_i_followup import ClauseTypeIFollowupRunner
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


class _Notifier:
    def send_escalation(self, **_kwargs):
        return True


def _fixture(tmp_path: Path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m1"), ("fake", "m2")],
        chair_model=("fake", "m1"),
        governance_docs="docs/governance",
        task_description="Resolve a Type I follow-up.",
    )
    registry = json.loads(
        repo.docs.read_text("identity_private/representative_registry.json")
    )
    active = registry[:6]
    statuses = {
        item["representative_id"]: (
            "ACTIVE" if item in active else "CONSULTATIVE"
        )
        for item in registry
    }
    repo.docs.write_once(
        "identity_private/general_principle/status_transition.json",
        json.dumps(
            {
                "primary_drafter_id": active[0]["representative_id"],
                "representative_statuses": statuses,
            }
        ),
    )
    for item in registry:
        repo.docs.write_once(
            f"representatives/{item['representative_id']}/status_transition_001.json",
            json.dumps(
                {
                    "representative_id": item["representative_id"],
                    "status": statuses[item["representative_id"]],
                }
            ),
        )
    draft = repo.docs.write_once("public/detailed_clauses/C0.md", "1.1 Existing text.\n")
    proposal = {
        "proposal_id": "CP-ONE",
        "proposer_id": active[0]["representative_id"],
        "target_clause_id": "1.1",
        "proposal_type": "SUPPLEMENT",
        "text": "Add one safeguard.",
        "reason": "Needed for bounded execution.",
    }
    option_sets = repo.docs.write_once(
        "public/detailed_clauses/option_sets.json",
        json.dumps(
            {
                "option_sets": [
                    {
                        "option_set_id": "OS-ONE",
                        "target_clause_id": "1.1",
                        "option_set_type": "TYPE_I",
                        "proposal_ids": ["CP-ONE"],
                        "options": [proposal],
                    }
                ]
            }
        ),
    )
    submissions = []
    for index, item in enumerate(active):
        support = index < 4
        submissions.append(
            {
                "representative_id": item["representative_id"],
                "votes": [
                    {
                        "option_set_id": "OS-ONE",
                        "choice": "SUPPORT" if support else "OPPOSE",
                        "reason": None if support else f"Decisive defect {index}.",
                    }
                ],
            }
        )
    repo.docs.write_once(
        "governance_private/detailed_clauses/initial_ballots/frozen.json",
        json.dumps({"status": "FROZEN", "submissions": submissions}),
    )
    initial = repo.docs.write_once(
        "public/detailed_clauses/initial_ballot_outcomes.json",
        json.dumps(
            {
                "outcomes": [
                    {
                        "option_set_id": "OS-ONE",
                        "option_set_type": "TYPE_I",
                        "eligible_count": 6,
                        "supermajority_required": 5,
                        "tally": {"SUPPORT": 4, "OPPOSE": 2},
                        "status": "TYPE_I_SECOND_ROUND_REQUIRED",
                    }
                ]
            }
        ),
    )
    return repo, active, draft, option_sets, initial, proposal


def test_type_i_followup_collects_proposer_decision_and_uses_protective_majority(
    tmp_path,
):
    repo, active, draft, option_sets, initial, proposal = _fixture(tmp_path)
    responses = [
        json.dumps(
            {
                "decisions": [
                    {
                        "option_set_id": "OS-ONE",
                        "proposal_id": "CP-ONE",
                        "action": "RETAIN",
                        "final_text": proposal["text"],
                        "revision_summary": "Retained after reviewing both anonymous objections.",
                    }
                ]
            }
        ),
        *[
            json.dumps(
                {
                    "votes": [
                        {
                            "option_set_id": "OS-ONE",
                            "choice": "SUPPORT" if index < 4 else "OPPOSE",
                        }
                    ]
                }
            )
            for index in range(len(active))
        ],
    ]
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m1", "m2"], responses)},
        notifier=_Notifier(),
    )

    result = ClauseTypeIFollowupRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
    ).run(
        draft_path=draft,
        option_sets_path=option_sets,
        initial_outcomes_path=initial,
    )

    assert result.adopted_count == 1
    assert result.contested_count == 1
    assert result.rejected_count == 0
    outcome = json.loads((repo.root / result.outcomes_path).read_text())["outcomes"][0]
    assert outcome["protective_majority_required"] == 4
    assert outcome["supermajority_required"] == 5
    assert outcome["tally"] == {"SUPPORT": 4, "OPPOSE": 2}
    assert outcome["status"] == "TYPE_I_ADOPTED_SECOND_ROUND_CONTESTED"

    proposer_input = json.loads(
        (
            repo.root
            / "governance_private/detailed_clauses/type_i_revision_inputs"
            / f"{active[0]['representative_id']}.json"
        ).read_text()
    )
    assert [item["reason"] for item in proposer_input["items"][0]["opposition_reasons"]] == [
        "Decisive defect 4.",
        "Decisive defect 5.",
    ]
    assert all("voter_id" not in item for item in proposer_input["items"][0]["opposition_reasons"])
    assert repo.events.verify()


def test_clause_application_accepts_terminal_type_i_rejection(tmp_path):
    repo = MeetingRepository(tmp_path / "M-APP")
    repo.docs.write_once("public/meeting_manifest.json", '{"meeting_id":"M-APP"}')
    option_sets = repo.docs.write_once(
        "public/detailed_clauses/option_sets.json",
        json.dumps(
            {
                "option_sets": [
                    {
                        "option_set_id": "OS-ADOPT",
                        "options": [
                            {
                                "proposal_id": "CP-ADOPT",
                                "target_clause_id": "1.1",
                                "proposal_type": "SUPPLEMENT",
                                "text": "Adopted text.",
                            }
                        ],
                    },
                    {
                        "option_set_id": "OS-REJECT",
                        "options": [
                            {
                                "proposal_id": "CP-REJECT",
                                "target_clause_id": "1.2",
                                "proposal_type": "SUPPLEMENT",
                                "text": "Rejected text.",
                            }
                        ],
                    },
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
                        "option_set_id": "OS-ADOPT",
                        "status": "TYPE_I_ADOPTED_FIRST_ROUND",
                        "adopted_proposal_id": "CP-ADOPT",
                    },
                    {
                        "option_set_id": "OS-REJECT",
                        "status": "TYPE_I_SECOND_ROUND_REQUIRED",
                    },
                ]
            }
        ),
    )
    runoff = repo.docs.write_once(
        "public/detailed_clauses/type_iii_runoff_outcomes.json",
        '{"outcomes":[]}',
    )
    second = repo.docs.write_once(
        "public/detailed_clauses/type_i_second_ballot_outcomes.json",
        json.dumps(
            {
                "outcomes": [
                    {
                        "option_set_id": "OS-REJECT",
                        "status": "TYPE_I_REJECTED_SECOND_ROUND",
                        "adopted_proposal_id": None,
                    }
                ]
            }
        ),
    )
    runner = ClauseApplicationRunner.__new__(ClauseApplicationRunner)
    runner.repo = repo

    adopted = runner._adopted_proposals(
        option_sets_path=option_sets,
        initial_outcomes_path=initial,
        runoff_outcomes_path=runoff,
        second_outcomes_path=None,
        type_i_revisions_path=None,
        type_i_second_outcomes_path=second,
    )

    assert [item["proposal_id"] for item in adopted] == ["CP-ADOPT"]


def test_type_i_followup_scheduler_parallelizes_models_but_serializes_personas():
    work = [
        {
            "participant_id": participant_id,
            "record": {
                "runtime": {
                    "provider_id": "provider",
                    "model_id": model_id,
                    "persona": persona,
                }
            },
        }
        for participant_id, model_id, persona in (
            ("R-A", "model-a", "systems_integrator"),
            ("R-B", "model-a", "pragmatic_minimalist"),
            ("R-C", "model-b", "systems_integrator"),
            ("R-D", "model-b", "pragmatic_minimalist"),
        )
    ]
    guard = threading.Lock()
    active_models: set[str] = set()
    overlaps = []
    order = {"model-a": [], "model-b": []}
    peak = 0

    def worker(item):
        nonlocal peak
        model_id = item["record"]["runtime"]["model_id"]
        with guard:
            if model_id in active_models:
                overlaps.append(model_id)
            active_models.add(model_id)
            order[model_id].append(item["record"]["runtime"]["persona"])
            peak = max(peak, len(active_models))
        time.sleep(0.03)
        with guard:
            active_models.remove(model_id)
        return item["participant_id"]

    results = ClauseTypeIFollowupRunner._run_model_lanes(work, worker)

    assert results == {"R-A": "R-A", "R-B": "R-B", "R-C": "R-C", "R-D": "R-D"}
    assert not overlaps
    assert peak == 2
    assert order == {
        "model-a": ["systems_integrator", "pragmatic_minimalist"],
        "model-b": ["systems_integrator", "pragmatic_minimalist"],
    }
