import json
import re
import threading
import time

import pytest

from project_ensemble.errors import PolicyNotConfiguredError
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.general_principle import GeneralPrincipleRunner
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.domain import GenerationResponse, Persona
from project_ensemble.storage.meeting import MeetingRepository


class CapturingNotifier:
    def __init__(self):
        self.calls = []

    def send_escalation(self, **kwargs):
        self.calls.append(kwargs)
        return True


class SupersedingChairAdapter(ScriptedProviderAdapter):
    def generate(self, request):
        if "REMAINING AMENDMENTS:" not in request.user_text:
            return super().generate(request)
        ids = list(dict.fromkeys(re.findall(r'"amendment_id": "(A-[^"]+)"', request.user_text)))
        dispositions = [
            {
                "amendment_id": amendment_id,
                "final_type": "SUPPLEMENTARY",
                "final_impact_scope": ["test scope"],
                "status": "SUPERSEDED",
                "conflicts_with": [],
                "dependencies": [],
                "reason": "test procedural disposition",
                "procedural_effect": "no ballot",
            }
            for amendment_id in ids
        ]
        return GenerationResponse(
            text=json.dumps({"dispositions": dispositions}),
            provider_id=self.provider_id,
            model_id=request.model_id,
            raw={"scripted": True},
        )


class RecordingResearchRoundRunner:
    def __init__(self):
        self.round_ids = []
        self.claim_limits = []

    def run(self, *, round_id, max_claims_per_representative=4, **kwargs):
        self.round_ids.append(round_id)
        self.claim_limits.append(max_claims_per_representative)


def make_repo(tmp_path):
    return MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Define a bounded operating principle.",
        escalation_email="human@example.test",
    )


def test_general_principle_runs_to_randomized_frozen_docket(tmp_path):
    repo = make_repo(tmp_path)
    notifier = CapturingNotifier()
    responses = [
        "# D0\nThe system shall preserve evidence.",
        '{"action":"SUPPORT"}',
        '{"action":"OPPOSE","amendment":{"text":"Add a verification requirement.",'
        '"declared_type":"SUPPLEMENTARY","declared_impact_scope":["verification"]}}',
        '{"action":"SUPPORT"}',
        '{"action":"OPPOSE","amendment":{"text":"Narrow the evidence claim.",'
        '"declared_type":"OBJECTION","declared_impact_scope":["evidence"]}}',
        '{"cosponsor_item_ids":["D0"]}',
        '{"cosponsor_item_ids":[]}',
        '{"cosponsor_item_ids":["D0"]}',
        '{"cosponsor_item_ids":[]}',
        *(['{"action":"SUPPORT"}'] * 4),
        *(['{"cosponsor_item_ids":[]}'] * 4),
        *(['{"action":"SUPPORT"}'] * 4),
        *(['{"cosponsor_item_ids":[]}'] * 4),
        *(['{"choice":"YES"}'] * 4),
        "# C0\n1. Execute the verified protocol.",
    ]
    adapter = SupersedingChairAdapter("fake", ["m"], responses)
    engine = MeetingEngine(repo=repo, adapters={"fake": adapter}, notifier=notifier)
    research_rounds = RecordingResearchRoundRunner()
    result = GeneralPrincipleRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        max_output_tokens=512,
        continue_into_detailed=True,
        research_round_runner=research_rounds,
    ).run()
    assert result.position_count == 12
    assert result.amendment_count == 2
    assert len(result.amendment_order) == 2
    docket = json.loads((repo.root / "public/general_principle/amendment_docket_window_001.json").read_text())
    assert docket["window_status"] == "FROZEN"
    assert len(docket["random_order"]["seed"]) == 64
    assert docket["random_order"]["order"] == result.amendment_order
    assert set(result.amendment_order) == {x["amendment_id"] for x in docket["amendments"]}
    assert result.cosponsorship_submission_count == 12
    assert result.deferred_amendment_count == 2
    assert result.completed_window_count == 3
    assert result.next_phase.value == "PAUSED"
    assert result.paused_reason == "DETAILED_CLAUSE_DOCKET_POLICY_NOT_CONFIGURED"
    assert result.ratified is True
    assert result.ratification_yes_votes == 4
    assert result.ratification_required_yes_votes == 3
    assert result.current_draft_path == "public/general_principle/D3.md"
    assert result.atomic_item_count == 1
    assert result.retained_atomic_item_count == 1
    assert result.primary_drafter_id == result.initial_drafter_id
    assert result.primary_drafter_candidate_count == 1
    assert result.consultative_count in {0, 1}
    assert result.atomic_item_policy_status == "TRIAL"
    assert result.atomic_item_review_status == "PENDING_THINK_TANK_REVIEW"
    assert result.detailed_draft_path == "public/detailed_clauses/C0.md"
    assert (repo.root / result.detailed_draft_path).read_text().startswith("# C0")
    ledger = json.loads(
        (repo.root / "governance_private/general_principle/cosponsorship/frozen_ledger.json").read_text()
    )
    assert ledger["status"] == "FROZEN"
    assert ledger["support"]["D0"]
    assert not list((repo.root / "public").rglob("*cosponsor*"))
    assert [call["reason_code"] for call in notifier.calls] == [
        "DETAILED_CLAUSE_DOCKET_POLICY_NOT_CONFIGURED"
    ]
    atomic_items = json.loads(
        (repo.root / "governance_private/drafting_alignment/atomic_items.json").read_text()
    )
    assert atomic_items["policy_status"] == "TRIAL"
    assert atomic_items["review_status"] == "PENDING_THINK_TANK_REVIEW"
    assert atomic_items["items"][0]["item_id"] == "D0:FALLBACK_DOCUMENT"
    assert not list((repo.root / "public").rglob("*scores*"))
    assert "MEETING_PHASE_CHANGED" in repo.events.path.read_text()
    assert repo.events.verify()
    assert research_rounds.round_ids == [
        "general-position-001-pre",
        "general-position-001-post",
        "general-ratification-001",
    ]
    assert research_rounds.claim_limits == [4, 2, 2]

    # All frozen artifacts make a resume idempotent: no additional provider response is needed.
    resumed = GeneralPrincipleRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        max_output_tokens=512,
        continue_into_detailed=True,
        research_round_runner=research_rounds,
    ).run()
    assert resumed.amendment_order == result.amendment_order


def test_schema_invalid_position_pauses_and_requests_human(tmp_path):
    repo = make_repo(tmp_path)
    notifier = CapturingNotifier()
    adapter = ScriptedProviderAdapter(
        "fake", ["m"], ["D0", "not JSON", "still not JSON", "still not JSON twice"]
    )
    engine = MeetingEngine(repo=repo, adapters={"fake": adapter}, notifier=notifier)
    with pytest.raises(PolicyNotConfiguredError, match="SCHEMA_INVALID"):
        GeneralPrincipleRunner(
            repo=repo,
            engine=engine,
            governance_docs="docs/governance",
            max_output_tokens=512,
        ).run()
    assert engine.status.phase.value == "PAUSED"
    assert notifier.calls[0]["reason_code"] == "SCHEMA_INVALID_MODEL_OUTPUT_AFTER_REPAIR"
    public_positions = repo.root / "public/general_principle/general_positions.json"
    assert not public_positions.exists()
    assert "HUMAN_INTERVENTION_REQUIRED" in repo.events.path.read_text()
    assert repo.events.verify()


def test_progress_reveals_positions_only_after_public_window_freeze(tmp_path):
    repo = make_repo(tmp_path)

    class CheckingProgress:
        def __init__(self):
            self.entries = []

        def status(self, phase, message):
            self.entries.append(("status", phase.value, message))

        def call_started(self, participant_id, stage):
            self.entries.append(("started", participant_id, stage))

        def call_completed(self, participant_id, stage):
            self.entries.append(("completed", participant_id, stage))

        def speech(self, participant_id, label, content):
            if "总则立场" in label:
                assert (repo.root / "public/general_principle/general_positions.json").exists()
            self.entries.append(("speech", participant_id, label, content))

        def info(self, message):
            self.entries.append(("info", message))

        def paused(self, reason_code, participant_id):
            self.entries.append(("paused", reason_code, participant_id))

    progress = CheckingProgress()
    responses = (
        ["Public D0"]
        + (['{"action":"SUPPORT"}'] * 4 + ['{"cosponsor_item_ids":[]}'] * 4) * 3
        + ['{"choice":"YES"}'] * 4
    )
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], responses)},
        notifier=CapturingNotifier(),
        progress=progress,
    )
    GeneralPrincipleRunner(repo=repo, engine=engine, governance_docs="docs/governance").run()
    speeches = [entry for entry in progress.entries if entry[0] == "speech"]
    assert speeches[0][2] == "D0 初始草案"
    assert len([entry for entry in speeches if "总则立场" in entry[2]]) == 12
    statuses = [entry[1] for entry in progress.entries if entry[0] == "status"]
    assert statuses == ["INITIAL_DRAFT"] + [
        phase
        for _ in range(3)
        for phase in ("GENERAL_POSITION", "COSPONSORSHIP", "BALLOT", "AMENDMENT_SUBMISSION")
    ] + ["GENERAL_RATIFICATION", "STATUS_TRANSITION", "DETAILED_DRAFTING"]


def test_general_positions_parallelize_model_lanes_and_preserve_registry_order(
    tmp_path, monkeypatch
):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "model-a"), ("fake", "model-b")],
        chair_model=("fake", "model-a"),
        governance_docs="docs/governance",
        task_description="Assess position scheduling.",
        personas=[Persona.SYSTEMS_INTEGRATOR, Persona.PRAGMATIC_MINIMALIST],
    )
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["model-a", "model-b"])},
        notifier=CapturingNotifier(),
    )
    runner = GeneralPrincipleRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
    )
    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    draft_path = repo.docs.write_once("public/general_principle/D0.md", "# D0\n")
    task_path = repo.root / "public/task.json"
    guard = threading.Lock()
    active_by_model = {"model-a": 0, "model-b": 0}
    peak_total = 0
    total_active = 0
    same_model_overlap = []
    observed_order = {"model-a": [], "model-b": []}

    def fake_collect_one_position(*, record, task_path, draft_path, window_number):
        nonlocal peak_total, total_active
        model_id = record["runtime"]["model_id"]
        persona = record["runtime"]["persona"]
        with guard:
            if active_by_model[model_id]:
                same_model_overlap.append(model_id)
            active_by_model[model_id] += 1
            total_active += 1
            peak_total = max(peak_total, total_active)
            observed_order[model_id].append(persona)
        time.sleep(0.03)
        with guard:
            active_by_model[model_id] -= 1
            total_active -= 1
        representative_id = record["representative_id"]
        position = {
            "representative_id": representative_id,
            "action": "SUPPORT",
            "amendment": None,
        }
        return (
            representative_id,
            position,
            runner._position_relative(window_number, representative_id),
        )

    monkeypatch.setattr(runner, "_collect_one_position", fake_collect_one_position)
    positions = runner._collect_positions(
        registry,
        task_path,
        draft_path,
        window_number=1,
    )

    assert not same_model_overlap
    assert peak_total == 2
    assert observed_order == {
        "model-a": ["systems_integrator", "pragmatic_minimalist"],
        "model-b": ["systems_integrator", "pragmatic_minimalist"],
    }
    assert [item["representative_id"] for item in positions] == [
        item["representative_id"] for item in registry
    ]
    assert len(
        list(
            (
                repo.root
                / "governance_private/general_principle/positions"
            ).glob("*.json")
        )
    ) == 4
    public_positions = json.loads(
        (
            repo.root / "public/general_principle/general_positions.json"
        ).read_text()
    )
    assert public_positions == positions
    assert repo.events.verify()


def test_schema_invalid_cosponsorship_pauses_without_publishing_partial_support(tmp_path):
    repo = make_repo(tmp_path)
    notifier = CapturingNotifier()
    responses = (
        ["D0"]
        + ['{"action":"SUPPORT"}'] * 4
        + ['{"cosponsor_item_ids":["NOT-AN-ITEM"]}']
    )
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["m"], responses)},
        notifier=notifier,
    )
    with pytest.raises(PolicyNotConfiguredError, match="SCHEMA_INVALID"):
        GeneralPrincipleRunner(
            repo=repo,
            engine=engine,
            governance_docs="docs/governance",
            max_output_tokens=512,
        ).run()
    assert not (repo.root / "governance_private/general_principle/cosponsorship/frozen_ledger.json").exists()
    assert not list((repo.root / "public").rglob("*cosponsor*"))
    assert notifier.calls[0]["reason_code"] == "SCHEMA_INVALID_MODEL_OUTPUT_POLICY_NOT_CONFIGURED"
