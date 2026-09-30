import json

from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.primary_drafter_selection import (
    PrimaryDrafterSelectionRunner,
)
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


class _Notifier:
    def send_escalation(self, **_kwargs):
        return True


def _fixture(tmp_path, responses):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Select a Primary Drafter.",
    )
    records = json.loads(
        repo.docs.read_text("identity_private/representative_registry.json")
    )
    draft = repo.docs.write_once("public/general_principle/D3.md", "Approved text.\n")
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], responses)},
        notifier=_Notifier(),
    )
    return repo, records, draft, engine


def _ranking(*candidate_ids):
    return json.dumps({"candidate_ids": list(candidate_ids)})


def _choice(candidate_id):
    return json.dumps({"candidate_id": candidate_id})


def test_two_way_primary_drafter_tie_uses_full_binary_ballot(tmp_path):
    # MeetingRepository creates four Representative offices for one base model.
    provisional = MeetingRepository.create(
        tmp_path / "ids",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="ids",
    )
    ids = [
        item["representative_id"]
        for item in json.loads(
            provisional.docs.read_text("identity_private/representative_registry.json")
        )
    ]
    candidates = ids[:2]
    responses = [
        *[_ranking(*candidates) for _ in range(4)],
        *[_choice(choice) for choice in [candidates[0]] * 3 + [candidates[1]]],
    ]
    repo, records, draft, engine = _fixture(tmp_path / "actual", responses)
    actual_ids = [item["representative_id"] for item in records]
    actual_candidates = actual_ids[:2]
    # Replace the provisional IDs embedded in scripted responses.
    adapter = engine.adapters["fake"]
    adapter._responses.clear()
    adapter._responses.extend(
        [
            *[_ranking(*actual_candidates) for _ in range(4)],
            *[_choice(choice) for choice in [actual_candidates[0]] * 3 + [actual_candidates[1]]],
        ]
    )

    winner = PrimaryDrafterSelectionRunner(repo=repo, engine=engine).run(
        candidates=actual_candidates,
        representative_records=records,
        final_draft=draft,
    )

    assert winner == actual_candidates[0]
    frozen = json.loads(
        (repo.root / "governance_private/drafting_alignment/primary_drafter_selection.json").read_text()
    )
    assert frozen["final_binary_tally"][winner] == 3
    assert not frozen["chair_tiebreak_used"]
    assert repo.events.verify()


def test_multiway_tie_runs_cutoff_ballot_before_final_binary(tmp_path):
    repo, records, draft, engine = _fixture(tmp_path, [])
    candidates = [item["representative_id"] for item in records[:3]]
    engine.adapters["fake"]._responses.extend(
        [
            _ranking(candidates[0], candidates[1], candidates[2]),
            _ranking(candidates[0], candidates[2], candidates[1]),
            _ranking(candidates[1], candidates[0], candidates[2]),
            _ranking(candidates[2], candidates[0], candidates[1]),
            *[_ranking(candidates[1], candidates[2]) for _ in range(3)],
            _ranking(candidates[2], candidates[1]),
            *[_choice(candidates[0]) for _ in range(3)],
            _choice(candidates[1]),
        ]
    )

    winner = PrimaryDrafterSelectionRunner(repo=repo, engine=engine).run(
        candidates=candidates,
        representative_records=records,
        final_draft=draft,
    )

    assert winner == candidates[0]
    frozen = json.loads(
        (repo.root / "governance_private/drafting_alignment/primary_drafter_selection.json").read_text()
    )
    assert frozen["finalist_ids"] == [candidates[0], candidates[1]]
    assert repo.events.verify()
