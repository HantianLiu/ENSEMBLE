import json

from project_ensemble.orchestration.clause_options import ClauseOptionSetRunner
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


class CapturingNotifier:
    def __init__(self):
        self.calls = []

    def send_escalation(self, **kwargs):
        self.calls.append(kwargs)
        return True


def test_chair_partition_freezes_type_i_and_type_ii_sets(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="task",
    )
    draft = repo.docs.write_once("public/detailed_clauses/C0.md", "1.1 Clause\n")
    proposals = [
        {
            "proposal_id": "CP-R-A-001",
            "proposer_id": "R-A",
            "target_clause_id": "1.1",
            "proposal_type": "SUPPLEMENT",
            "text": "Independent addition",
            "reason": "test",
        },
        {
            "proposal_id": "CP-R-B-001",
            "proposer_id": "R-B",
            "target_clause_id": "1.1",
            "proposal_type": "REPLACE",
            "text": "Alternative B",
            "reason": "test",
        },
        {
            "proposal_id": "CP-R-C-001",
            "proposer_id": "R-C",
            "target_clause_id": "1.1",
            "proposal_type": "REPLACE",
            "text": "Alternative C",
            "reason": "test",
        },
    ]
    reviews = repo.docs.write_once(
        "public/detailed_clauses/active_reviews.json",
        json.dumps({"proposals": proposals}),
    )
    response = json.dumps(
        {
            "groupings": [
                {
                    "target_clause_id": "1.1",
                    "relationship": "INDEPENDENT",
                    "proposal_ids": ["CP-R-A-001"],
                    "reason": "compatible standalone addition",
                },
                {
                    "target_clause_id": "1.1",
                    "relationship": "MUTUALLY_EXCLUSIVE",
                    "proposal_ids": ["CP-R-B-001", "CP-R-C-001"],
                    "reason": "alternative replacements",
                },
            ]
        }
    )
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], [response])},
        notifier=CapturingNotifier(),
    )

    result = ClauseOptionSetRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        max_output_tokens=1024,
    ).run(draft_path=draft, active_reviews_path=reviews)

    assert result.option_set_count == 2
    assert result.type_i_count == 1
    assert result.type_ii_count == 1
    assert result.type_iii_count == 0
    frozen = json.loads((repo.root / result.record_path).read_text())
    assert frozen["proposal_count"] == 3
    assert {item["option_set_type"] for item in frozen["option_sets"]} == {
        "TYPE_I",
        "TYPE_II",
    }
    assert (
        repo.root / "chair_private/detailed_clauses/option_plans/1_1.json"
    ).exists()
    assert result.paused_reason == "DETAILED_CLAUSE_BALLOT_EXECUTION_NOT_IMPLEMENTED"
    assert repo.events.verify()


def test_single_proposal_target_is_partitioned_without_chair_call(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="task",
    )
    draft = repo.docs.write_once("public/detailed_clauses/C0.md", "1.1 Clause\n")
    proposal = {
        "proposal_id": "CP-R-A-001",
        "proposer_id": "R-A",
        "target_clause_id": "1.1",
        "proposal_type": "SUPPLEMENT",
        "text": "Only proposal",
        "reason": "test",
    }
    reviews = repo.docs.write_once(
        "public/detailed_clauses/active_reviews.json",
        json.dumps({"proposals": [proposal]}),
    )
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], [])},
        notifier=CapturingNotifier(),
    )

    result = ClauseOptionSetRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        max_output_tokens=1024,
    ).run(draft_path=draft, active_reviews_path=reviews)

    frozen = json.loads((repo.root / result.record_path).read_text())
    assert result.option_set_count == 1
    assert result.type_i_count == 1
    assert frozen["option_sets"][0]["proposal_ids"] == ["CP-R-A-001"]
    assert not (repo.root / "chair_private/detailed_clauses/option_plans").exists()
    assert repo.events.verify()
