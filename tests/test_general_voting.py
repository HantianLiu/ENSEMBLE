import json
import re

import pytest

from project_ensemble.domain import GenerationResponse
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.consultations import HumanConsultationService
from project_ensemble.orchestration.general_voting import GeneralRatificationRunner, GeneralVotingRunner
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


class CapturingNotifier:
    def __init__(self):
        self.calls = []

    def send_escalation(self, **kwargs):
        self.calls.append(kwargs)
        return True


class VotingAdapter(ScriptedProviderAdapter):
    def generate(self, request):
        if "REMAINING AMENDMENTS:" in request.user_text:
            ids = list(dict.fromkeys(re.findall(r'"amendment_id": "(A-[^"]+)"', request.user_text)))
            text = json.dumps(
                {
                    "dispositions": [
                        {
                            "amendment_id": amendment_id,
                            "final_type": "SUPPLEMENTARY",
                            "final_impact_scope": ["base"],
                            "status": "READY",
                            "conflicts_with": [],
                            "dependencies": [],
                            "reason": "ready for the test ballot",
                            "procedural_effect": "open binary ballot",
                        }
                        for amendment_id in ids
                    ]
                }
            )
            return GenerationResponse(
                text=text,
                provider_id=self.provider_id,
                model_id=request.model_id,
                raw={"scripted": True},
            )
        if "APPLY_ADOPTED_AMENDMENT" in request.user_text:
            amendment_id = re.search(r"AMENDMENT ID: (A-[^\n]+)", request.user_text).group(1)
            base_sha = re.search(r"BASE SHA-256: ([0-9a-f]{64})", request.user_text).group(1)
            text = json.dumps(
                {
                    "action": "APPLY_ADOPTED_AMENDMENT",
                    "amendment_id": amendment_id,
                    "base_sha256": base_sha,
                    "updated_text": "# D0\nBase principle.\n\nAdded verification.\n",
                    "changes": [
                        {
                            "operation": "ADD",
                            "target": "end of draft",
                            "description": "add the adopted verification requirement",
                        }
                    ],
                    "reason": "mechanically applied adopted amendment",
                }
            )
            return GenerationResponse(
                text=text,
                provider_id=self.provider_id,
                model_id=request.model_id,
                raw={"scripted": True},
            )
        return super().generate(request)


class ConflictChairAdapter(VotingAdapter):
    def __init__(self, *args, decomposition="PARTIAL_CONFLICT", **kwargs):
        super().__init__(*args, **kwargs)
        self.decomposition = decomposition

    def generate(self, request):
        if "confirmed conflict-set priority rule" in request.user_text:
            ids = list(dict.fromkeys(re.findall(r'"amendment_id": "(A-[^"]+)"', request.user_text)))
            if self.decomposition == "NO_CONFLICT":
                payload = {
                    "affected_amendment_ids": ids,
                    "relationship": "NO_CONFLICT",
                    "compatible_fragments": [],
                    "choice_sets": [],
                    "reason": "The amendments overlap in scope but their normative content can coexist.",
                }
            else:
                payload = {
                    "affected_amendment_ids": ids,
                    "relationship": "PARTIAL_CONFLICT",
                    "compatible_fragments": [
                        {
                            "fragment_id": "COMMON_CHECK",
                            "source_amendment_ids": ids,
                            "text": "Add the common verification requirement.",
                            "impact_scope": ["base"],
                            "reason": "Both amendments require verification.",
                        }
                    ],
                    "choice_sets": [
                        {
                            "choice_set_id": "METHOD",
                            "question": "Which incompatible verification method should govern?",
                            "options": [
                                {
                                    "fragment_id": "METHOD_A",
                                    "source_amendment_ids": [ids[0]],
                                    "text": "Use verification method A.",
                                    "impact_scope": ["base"],
                                    "reason": "Method A cannot coexist with method B.",
                                },
                                {
                                    "fragment_id": "METHOD_B",
                                    "source_amendment_ids": [ids[1]],
                                    "text": "Use verification method B.",
                                    "impact_scope": ["base"],
                                    "reason": "Method B cannot coexist with method A.",
                                },
                            ],
                        }
                    ],
                    "reason": "The common requirement is compatible, while the implementation methods conflict.",
                }
            return GenerationResponse(
                text=json.dumps(payload),
                provider_id=self.provider_id,
                model_id=request.model_id,
                raw={"scripted": True},
            )
        if "requested reconsideration" in request.user_text:
            ids = list(dict.fromkeys(re.findall(r'"amendment_id": "(A-[^"]+)"', request.user_text)))
            return GenerationResponse(
                text=json.dumps(
                    {
                        "affected_amendment_ids": ids,
                        "relationship": "OVERLAPPING_COMPATIBLE",
                        "reason": "The amendments overlap but can be processed sequentially.",
                        "procedural_effect": "Process the first, then reassess the second against current text.",
                    }
                ),
                provider_id=self.provider_id,
                model_id=request.model_id,
                raw={"scripted": True},
            )
        if "REMAINING AMENDMENTS:" not in request.user_text:
            if "APPLY_ADOPTED_AMENDMENT" in request.user_text and "AMENDMENT ID: CG-" in request.user_text:
                amendment_id = re.search(r"AMENDMENT ID: ([^\n]+)", request.user_text).group(1)
                base_sha = re.search(r"BASE SHA-256: ([0-9a-f]{64})", request.user_text).group(1)
                base_text = request.user_text.split("CURRENT DRAFT:\n", 1)[1].split(
                    "\n\nADOPTED AMENDMENT:\n", 1
                )[0]
                return GenerationResponse(
                    text=json.dumps(
                        {
                            "action": "APPLY_ADOPTED_AMENDMENT",
                            "amendment_id": amendment_id,
                            "base_sha256": base_sha,
                            "updated_text": base_text.rstrip() + f"\n\nApplied {amendment_id}.\n",
                            "changes": [
                                {
                                    "operation": "ADD",
                                    "target": "end of draft",
                                    "description": f"apply {amendment_id}",
                                }
                            ],
                            "reason": "Mechanically apply the adopted conflict fragments.",
                        }
                    ),
                    provider_id=self.provider_id,
                    model_id=request.model_id,
                    raw={"scripted": True},
                )
            return super().generate(request)
        ids = list(dict.fromkeys(re.findall(r'"amendment_id": "(A-[^"]+)"', request.user_text)))
        dispositions = []
        for amendment_id in ids:
            other_ids = [item for item in ids if item != amendment_id]
            dispositions.append(
                {
                    "amendment_id": amendment_id,
                    "final_type": "SUPPLEMENTARY",
                    "final_impact_scope": ["base"],
                    "status": "READY" if other_ids else "SUPERSEDED",
                    "conflicts_with": other_ids,
                    "dependencies": [],
                    "reason": "mutually exclusive in test" if other_ids else "already covered",
                    "procedural_effect": "consult Human" if other_ids else "no ballot",
                }
            )
        return GenerationResponse(
            text=json.dumps({"dispositions": dispositions}),
            provider_id=self.provider_id,
            model_id=request.model_id,
            raw={"scripted": True},
        )


def make_voting_repo(tmp_path, *, include_conflict=False):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Decide a bounded principle.",
        escalation_email="human@example.test",
    )
    repo.docs.write_once("public/general_principle/D0.md", "# D0\nBase principle.\n")
    amendments = [
        {
            "amendment_id": "A-TEST0001",
            "proposer_id": "R-PROPOSER",
            "text": "Add a verification requirement.",
            "declared_type": "SUPPLEMENTARY",
            "declared_impact_scope": ["base"],
        }
    ]
    if include_conflict:
        amendments.append(
            {
                "amendment_id": "A-TEST0002",
                "proposer_id": "R-OTHER",
                "text": "Use an incompatible verification rule.",
                "declared_type": "SUPPLEMENTARY",
                "declared_impact_scope": ["base"],
            }
        )
    docket = {
        "window_id": "general-principle-001",
        "window_status": "FROZEN",
        "amendments": amendments,
        "random_order": {
            "seed": "0" * 64,
            "algorithm": "test",
            "order": [item["amendment_id"] for item in amendments],
        },
    }
    repo.docs.write_once(
        "public/general_principle/amendment_docket_window_001.json",
        json.dumps(docket),
    )
    return repo


def run_voting(repo, responses):
    notifier = CapturingNotifier()
    adapter = VotingAdapter("fake", ["m"], responses)
    engine = MeetingEngine(repo=repo, adapters={"fake": adapter}, notifier=notifier)
    result = GeneralVotingRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        max_output_tokens=512,
    ).run(
        draft_path=repo.root / "public/general_principle/D0.md",
        docket_path=repo.root / "public/general_principle/amendment_docket_window_001.json",
    )
    return result, notifier


def test_sequential_binary_supermajority_adopts_and_freezes_d1(tmp_path):
    repo = make_voting_repo(tmp_path)
    result, notifier = run_voting(
        repo,
        [
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"STATUS_QUO"}',
        ],
    )
    assert result.ballot_count == 1
    assert result.adopted_amendment_count == 1
    assert result.draft_path == "public/general_principle/D1.md"
    assert result.next_phase.value == "AMENDMENT_SUBMISSION"
    assert "Added verification" in (repo.root / result.draft_path).read_text()
    decision = json.loads(
        next((repo.root / "public/general_principle/decisions").rglob("*.json")).read_text()
    )
    assert decision["source"] == "SUPERMAJORITY_PASS"
    assert not notifier.calls
    assert repo.events.verify()


def test_protective_vote_after_failed_supermajority(tmp_path):
    repo = make_voting_repo(tmp_path)
    result, _ = run_voting(
        repo,
        [
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"STATUS_QUO"}',
            '{"choice":"STATUS_QUO"}',
            '{"action":"PASS"}',
            '{"action":"STATE","reason":"Verification is necessary."}',
            '{"action":"PASS"}',
            '{"action":"STATE","reason":"The scope is bounded."}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"STATUS_QUO"}',
        ],
    )
    assert result.ballot_count == 2
    decision = json.loads(
        next((repo.root / "public/general_principle/decisions").rglob("*.json")).read_text()
    )
    assert decision["source"] == "PROTECTIVE_PASS"
    assert (repo.root / "public/general_principle/reasons/window_001/step_001.json").exists()


def test_resume_retains_complete_votes_and_collects_only_missing_votes(tmp_path):
    repo = make_voting_repo(tmp_path)
    ballot_id = "B-W001-001-A-TEST0001-SUPERMAJORITY"
    registry = json.loads(
        repo.docs.read_text("identity_private/representative_registry.json")
    )
    first_representative = registry[0]["representative_id"]
    vote_relative = (
        f"governance_private/general_principle/ballots/{ballot_id}/votes/"
        f"{first_representative}.json"
    )
    repo.docs.write_once(
        vote_relative,
        json.dumps(
            {
                "ballot_id": ballot_id,
                "representative_id": first_representative,
                "choice": "AMENDMENT",
            }
        ),
    )
    resumed, notifier = run_voting(
        repo,
        [
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
        ],
    )
    assert resumed.next_phase.value == "AMENDMENT_SUBMISSION"
    assert not notifier.calls
    assert HumanConsultationService(repo).open_issues() == []
    frozen = json.loads(
        (
            repo.root
            / "governance_private/general_principle/ballots"
            / ballot_id
            / "frozen_ballot.json"
        ).read_text()
    )
    assert frozen["attempt_number"] == 1
    assert frozen["tally"] == {"AMENDMENT": 4, "STATUS_QUO": 0}
    review = json.loads(
        next(
            (
                repo.root
                / "governance_private/general_principle/ballots"
                / ballot_id
                / "recovery_reviews"
            ).glob("review_*.json")
        ).read_text()
    )
    assert len(review["retained_record_paths"]) == 1
    assert len(review["representatives_to_recollect"]) == 3
    assert review["partial_tally_disclosed"] is False
    assert repo.events.verify()


def test_incomplete_vote_is_preserved_and_recollected_to_a_new_record(tmp_path):
    repo = make_voting_repo(tmp_path)
    ballot_id = "B-W001-001-A-TEST0001-SUPERMAJORITY"
    registry = json.loads(repo.docs.read_text("identity_private/representative_registry.json"))
    first_representative = registry[0]["representative_id"]
    first_vote = (
        f"governance_private/general_principle/ballots/{ballot_id}/votes/"
        f"{first_representative}.json"
    )
    repo.docs.write_once(
        first_vote,
        json.dumps(
            {
                "ballot_id": ballot_id,
                "representative_id": first_representative,
            }
        ),
    )
    original_bytes = (repo.root / first_vote).read_bytes()
    resumed, _ = run_voting(repo, ['{"choice":"AMENDMENT"}'] * 4)
    assert resumed.next_phase.value == "AMENDMENT_SUBMISSION"
    base_vote_path = repo.root / first_vote
    assert base_vote_path.read_bytes() == original_bytes
    recovered_votes = (
        repo.root
        / "governance_private/general_principle/ballots"
        / ballot_id
        / "recovered_votes"
        / first_representative
    )
    assert len(list(recovered_votes.glob("*.json"))) == 1
    frozen = json.loads(
        (
            repo.root
            / "governance_private/general_principle/ballots"
            / ballot_id
            / "frozen_ballot.json"
        ).read_text()
    )
    assert frozen["attempt_number"] == 1
    assert frozen["tally"] == {"AMENDMENT": 4, "STATUS_QUO": 0}
    review = json.loads(
        next(
            (
                repo.root
                / "governance_private/general_principle/ballots"
                / ballot_id
                / "recovery_reviews"
            ).glob("review_*.json")
        ).read_text()
    )
    assert review["invalid_records"][0]["record_path"] == first_vote
    assert repo.events.verify()


def test_final_ratification_uses_the_same_partial_ballot_recovery(tmp_path):
    repo = make_voting_repo(tmp_path)
    d3_path = repo.root / "public/general_principle/D3.md"
    repo.docs.write_once("public/general_principle/D3.md", "# D3\nFinal principle.\n")
    registry = json.loads(repo.docs.read_text("identity_private/representative_registry.json"))
    first_representative = registry[0]["representative_id"]
    ballot_id = "B-GENERAL-D3-RATIFICATION"
    repo.docs.write_once(
        f"governance_private/general_principle/ratification/votes/{first_representative}.json",
        json.dumps(
            {
                "ballot_id": ballot_id,
                "representative_id": first_representative,
                "choice": "YES",
                "opposition_reason": None,
            }
        ),
    )
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": VotingAdapter("fake", ["m"], ['{"choice":"YES"}'] * 3)},
        notifier=CapturingNotifier(),
    )
    resumed = GeneralRatificationRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
    ).run(draft_path=d3_path)
    assert resumed.passed is True
    assert resumed.yes_votes == 4
    assert resumed.next_phase.value == "STATUS_TRANSITION"
    assert resumed.paused_reason is None
    assert repo.events.verify()


def test_partial_conflict_is_split_into_binary_and_multi_option_ballots(tmp_path):
    repo = make_voting_repo(tmp_path, include_conflict=True)
    notifier = CapturingNotifier()
    responses = [
        # Compatible fragment reaches the 3/4 adoption threshold.
        '{"choice":"AMENDMENT"}',
        '{"choice":"AMENDMENT"}',
        '{"choice":"AMENDMENT"}',
        '{"choice":"STATUS_QUO"}',
        # Multi-option ranking puts both substantive options in the top two.
        '{"choice":"OPTION_METHOD_A"}',
        '{"choice":"OPTION_METHOD_A"}',
        '{"choice":"OPTION_METHOD_B"}',
        '{"choice":"OPTION_METHOD_B"}',
        # Top-two Type II ballot adopts method A at the 3/4 threshold.
        '{"choice":"OPTION_METHOD_A"}',
        '{"choice":"OPTION_METHOD_A"}',
        '{"choice":"OPTION_METHOD_A"}',
        '{"choice":"OPTION_METHOD_B"}',
    ]
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ConflictChairAdapter("fake", ["m"], responses)},
        notifier=notifier,
    )
    result = GeneralVotingRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
    ).run(
        draft_path=repo.root / "public/general_principle/D0.md",
        docket_path=repo.root / "public/general_principle/amendment_docket_window_001.json",
    )
    assert result.next_phase.value == "AMENDMENT_SUBMISSION"
    assert result.ballot_count == 3
    assert result.adopted_amendment_count == 2
    assert not notifier.calls
    assert HumanConsultationService(repo).open_issues() == []
    decomposition = json.loads(
        (
            repo.root
            / "public/general_principle/conflict_decompositions/window_001/step_001.json"
        ).read_text()
    )
    assert decomposition["relationship"] == "PARTIAL_CONFLICT"
    assert decomposition["compatible_fragments"][0]["fragment_id"] == "COMMON_CHECK"
    choice_result = json.loads(
        (
            repo.root
            / "public/general_principle/conflict_choice_sets/window_001/step_001_METHOD.result.json"
        ).read_text()
    )
    assert choice_result["winner"] == "OPTION_METHOD_A"
    decisions = {
        item["amendment_id"]: item
        for item in (
            json.loads(path.read_text())
            for path in (repo.root / "public/general_principle/decisions").rglob("*.json")
        )
    }
    assert decisions["A-TEST0001"]["outcome"] == "ADOPTED"
    assert decisions["A-TEST0002"]["outcome"] == "PARTIALLY_ADOPTED"
    final_text = (repo.root / result.draft_path).read_text()
    assert "CG-W001-S001-COMPAT" in final_text
    assert "CG-W001-S001-EXCLUSIVE" in final_text
    assert repo.events.verify()


def test_chair_automatically_returns_false_positive_conflict_to_sequential_processing(tmp_path):
    repo = make_voting_repo(tmp_path, include_conflict=True)
    notifier = CapturingNotifier()
    adapter = ConflictChairAdapter(
        "fake",
        ["m"],
        [
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"AMENDMENT"}',
            '{"choice":"STATUS_QUO"}',
        ],
        decomposition="NO_CONFLICT",
    )
    engine = MeetingEngine(repo=repo, adapters={"fake": adapter}, notifier=notifier)
    result = GeneralVotingRunner(repo=repo, engine=engine, governance_docs="docs/governance").run(
        draft_path=repo.root / "public/general_principle/D0.md",
        docket_path=repo.root / "public/general_principle/amendment_docket_window_001.json",
    )
    assert result.next_phase.value == "AMENDMENT_SUBMISSION"
    assert result.ballot_count == 1
    review = json.loads(
        (
            repo.root
            / "public/general_principle/conflict_decompositions/window_001/step_001.json"
        ).read_text()
    )
    assert review["relationship"] == "NO_CONFLICT"
    assert HumanConsultationService(repo).open_issues() == []
    assert not notifier.calls
