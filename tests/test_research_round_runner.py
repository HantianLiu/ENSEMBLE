import json
import threading
import time
from pathlib import Path

import pytest

from project_ensemble.domain import Persona, ReasoningEffort
from project_ensemble.errors import RepresentativeUnavailableError, TransientProviderError
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.research.models import ResearchRoundSubmission, ResearchStage
from project_ensemble.research.rounds import ResearchRoundRunner
from project_ensemble.runtime.context import RepresentativeContextAssembler
from project_ensemble.runtime.documents import GovernanceDocumentResolver
from project_ensemble.storage.meeting import MeetingRepository


class _Notifier:
    def send_escalation(self, **kwargs):
        return True


class _Retriever:
    backend_ids = ("fixture",)

    def retrieve(self, claim):
        candidate = {
            "source_id": "https://example.test/source",
            "title": "Bounded source",
            "authors": ["Researcher"],
            "publication_year": 2025,
            "doi": None,
            "url": "https://example.test/source",
            "venue": "Fixture Journal",
            "source_type": "article",
            "is_primary_source": True,
            "abstract": "A bounded result under condition C.",
            "full_text_url": None,
            "full_text_is_public": False,
            "license": None,
            "retrieval_purposes": ["supporting", "limitations"],
        }
        trace = [
            {
                "backend_id": "fixture",
                "purpose": purpose,
                "query": f"query {purpose}",
                "returned_source_ids": (
                    [candidate["source_id"]] if purpose in {"supporting", "limitations"} else []
                ),
            }
            for purpose in ("supporting", "contradictory", "limitations", "alternatives")
        ]
        return [candidate], trace


class _NeverFetch:
    def fetch(self, url):
        raise AssertionError("fixture has no public full-text URL")


def _normalization(claim="The bounded result holds under condition C."):
    return json.dumps(
        {
            "is_researchable": True,
            "normalized_claim": claim,
            "verification_question": "Does the bounded result hold under condition C?",
            "supporting_query": "bounded result condition C",
            "contradictory_query": "bounded result contradiction condition C",
            "limitations_query": "bounded result limitations condition C",
            "alternatives_query": "bounded result alternatives condition C",
            "scope_terms": ["condition C"],
            "source_domain": "ACADEMIC",
            "source_domain_rationale": "Scholarly result.",
            "freshness_class": "STABLE",
            "freshness_rationale": "Slow-changing evidence.",
            "rejection_reason": None,
        }
    )


def test_round_state_summary_distinguishes_incomplete_and_released_rounds(tmp_path):
    repo = MeetingRepository(tmp_path / "meeting")
    incomplete = repo.root / "governance_private/research/rounds/round-a"
    incomplete.mkdir(parents=True)
    released = repo.root / "public/research/rounds/round-b/evidence_snapshot.json"
    released.parent.mkdir(parents=True)
    released.write_text(
        json.dumps(
            {
                "status": "RELEASED",
                "packet_ids": ["EP-1", "EP-2", "EP-1"],
            }
        ),
        encoding="utf-8",
    )

    assert ResearchRoundRunner.incomplete_round_ids(repo) == ["round-a"]
    assert ResearchRoundRunner.released_summary(repo) == (1, 2)


def _synthesis():
    finding = {
        "source_id": "https://example.test/source",
        "direction": "SUPPORTING",
        "evidence_summary": "The source reports a bounded result.",
        "applicability": "Condition C.",
        "limitations": "Single source.",
    }
    return json.dumps(
        {
            "packet": {
                "packet_id": "PENDING",
                "claim_fingerprint": "0" * 64,
                "original_claim": "placeholder",
                "normalized_claim": "placeholder",
                "verification_question": "Does the bounded result hold under condition C?",
                "scope_terms": ["condition C"],
                "source_domain": "ACADEMIC",
                "retrieval_backend_ids": ["fixture"],
                "retrieved_at": "2026-09-18T00:00:00Z",
                "search_scope": "Fixture retrieval.",
                "sources": [
                    {
                        "source_id": "https://example.test/source",
                        "title": "Bounded source",
                        "authors": ["Researcher"],
                        "publication_year": 2025,
                        "doi": None,
                        "url": "https://example.test/source",
                        "venue": "Fixture Journal",
                        "source_type": "article",
                        "is_primary_source": True,
                        "evidence_use_class": "PRIMARY",
                    }
                ],
                "supporting_evidence": [finding],
                "contradictory_evidence": [],
                "scope_limitations": [],
                "canonical_alternatives": [],
                "counter_search_summary": "All four searches ran; no same-scope contradiction found.",
                "evidence_conflict_assessment": "No same-scope conflict in the fixture.",
                "consensus": "INSUFFICIENT",
                "unresolved_questions": ["Whether the result generalizes."],
                "confidence": {
                    "coverage": "LOW",
                    "source_quality": "MEDIUM",
                    "literature_consistency": "LOW",
                    "rationale": "One bounded source.",
                },
                "knowledge_status": "QUALIFIED",
            },
            "screening_decisions": [
                {
                    "source_id": "https://example.test/source",
                    "included": True,
                    "rationale": "Directly relevant.",
                }
            ],
        }
    )


def _repo(tmp_path):
    return MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "representative")],
        chair_model=("fake", "representative"),
        governance_docs="docs/governance",
        task_description="Assess a bounded claim.",
        personas=[Persona.SYSTEMS_INTEGRATOR],
        research_enabled=True,
        research_model=("fake", "research"),
        research_reasoning_effort=ReasoningEffort.LOW,
    )


def _runner(repo, responses, adapter_type=ScriptedProviderAdapter):
    adapter = adapter_type("fake", ["representative", "research"], responses)
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": adapter},
        notifier=_Notifier(),
        max_retries=0,
    )
    return ResearchRoundRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        retriever=_Retriever(),
        document_fetcher=_NeverFetch(),
        max_output_tokens=1024,
    )


def test_round_releases_one_snapshot_and_injects_it_into_later_context(tmp_path):
    repo = _repo(tmp_path)
    runner = _runner(
        repo,
        [
            json.dumps({"claims": ["The bounded result holds under condition C."]}),
            _normalization(),
            _synthesis(),
        ],
    )

    result = runner.run(
        round_id="general-position-001-pre",
        stage=ResearchStage.PROPOSAL,
        subject_files=(repo.root / "public/task.json",),
    )

    snapshot = repo.root / result.evidence_snapshot_path
    assert result.packet_count == 1
    assert snapshot.exists()
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    assert data["status"] == "RELEASED"
    assert data["packet_ids"] == result.packet_ids
    assert data["packets"][0]["normalized_claim"].startswith("The bounded result")
    assert (repo.root / "public/research/literature_bundle.zip").exists()
    assert repo.events.verify()

    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    spec = GovernanceDocumentResolver("docs/governance").representative_context_spec(
        persona=Persona(registry[0]["runtime"]["persona"]),
        stage="general_position",
        representative_id=registry[0]["representative_id"],
        public_state_files=(repo.root / "public/task.json",),
    )
    context = RepresentativeContextAssembler().assemble(spec)
    assert "general-position-001-pre" in context
    assert "The bounded result holds under condition C." in context

    resumed = runner.run(
        round_id="general-position-001-pre",
        stage=ResearchStage.PROPOSAL,
        subject_files=(repo.root / "public/task.json",),
    )
    assert resumed.packet_ids == result.packet_ids


def test_round_enforces_and_publishes_stage_specific_two_claim_limit(tmp_path):
    repo = _repo(tmp_path)
    runner = _runner(
        repo,
        [
            json.dumps({"claims": ["claim one", "claim two", "claim three"]}),
            json.dumps({"claims": ["claim one", "claim two"]}),
            _normalization("Normalized claim one."),
            _normalization("Normalized claim two."),
            _synthesis(),
            _synthesis(),
        ],
    )

    result = runner.run(
        round_id="two-claim-stage",
        stage=ResearchStage.CHALLENGE,
        subject_files=(repo.root / "public/task.json",),
        max_claims_per_representative=2,
    )

    frozen = json.loads(
        (
            repo.root
            / "governance_private/research/rounds/two-claim-stage/frozen_requests.json"
        ).read_text()
    )
    snapshot = json.loads((repo.root / result.evidence_snapshot_path).read_text())
    assert len(frozen["claims"]) == 2
    assert frozen["max_claims_per_representative"] == 2
    assert snapshot["max_claims_per_representative"] == 2


def test_started_legacy_round_retains_four_claim_compatibility_limit(tmp_path):
    repo = _repo(tmp_path)
    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    representative_id = registry[0]["representative_id"]
    repo.docs.write_once(
        Path("governance_private/research/rounds/legacy-round/submissions")
        / f"{representative_id}.json",
        ResearchRoundSubmission(
            claims=["legacy claim one", "legacy claim two", "legacy claim three"]
        ).model_dump_json(indent=2),
    )
    runner = _runner(
        repo,
        [
            _normalization("Normalized legacy claim one."),
            _normalization("Normalized legacy claim two."),
            _normalization("Normalized legacy claim three."),
            _synthesis(),
            _synthesis(),
            _synthesis(),
        ],
    )

    result = runner.run(
        round_id="legacy-round",
        stage=ResearchStage.CHALLENGE,
        subject_files=(repo.root / "public/task.json",),
        max_claims_per_representative=2,
    )

    policy = json.loads(
        (
            repo.root
            / "governance_private/research/rounds/legacy-round/round_policy.json"
        ).read_text()
    )
    snapshot = json.loads((repo.root / result.evidence_snapshot_path).read_text())
    assert policy["max_claims_per_representative"] == 4
    assert policy["compatibility_disposition"] == (
        "LEGACY_STARTED_ROUND_RETAINS_FOUR"
    )
    assert snapshot["max_claims_per_representative"] == 4


def test_research_round_retry_rebuilds_generated_bundle_metadata_idempotently(tmp_path):
    """A later release must not conflict with an older generated manifest.

    The staging tree starts as a copy of the public bundle.  Each research
    call regenerates ``manifest.json`` with a new timestamp, so comparing that
    generated file as if it were an immutable record used to make the second
    round fail before the public bundle could be rebuilt.
    """
    repo = _repo(tmp_path)
    runner = _runner(
        repo,
        [
            json.dumps({"claims": ["The bounded result holds under condition C."]}),
            _normalization(),
            _synthesis(),
            json.dumps({"claims": ["The bounded result holds under condition C."]}),
            _normalization(),
            _synthesis(),
        ],
    )

    runner.run(
        round_id="general-position-001-pre",
        stage=ResearchStage.PROPOSAL,
        subject_files=(repo.root / "public/task.json",),
    )
    resumed = runner.run(
        round_id="general-position-002-pre",
        stage=ResearchStage.PROPOSAL,
        subject_files=(repo.root / "public/task.json",),
    )

    assert resumed.packet_count == 1
    manifest = json.loads(
        (repo.root / "public/research/literature_bundle/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["meeting_id"] == repo.meeting_id


def test_interrupted_round_resume_preserves_newer_staged_bundle_manifest(tmp_path):
    """Resume must not replace a staging manifest with the older public view."""
    repo = _repo(tmp_path)
    runner = _runner(repo, [])
    public_bundle = repo.root / "public/research/literature_bundle"
    public_bundle.mkdir(parents=True)
    (public_bundle / "manifest.json").write_text(
        '{"record_count": 1}', encoding="utf-8"
    )
    (public_bundle / "README.md").write_text("generated public view", encoding="utf-8")

    stage_repo = runner._staging_repo("interrupted-round")
    staged_manifest = (
        stage_repo.root / "public/research/literature_bundle/manifest.json"
    )
    staged_readme = stage_repo.root / "public/research/literature_bundle/README.md"
    staged_manifest.parent.mkdir(parents=True, exist_ok=True)
    staged_manifest.write_text('{"record_count": 2}', encoding="utf-8")
    staged_readme.write_text("generated staged view", encoding="utf-8")

    resumed_stage_repo = runner._staging_repo("interrupted-round")

    assert resumed_stage_repo.root == stage_repo.root
    assert staged_manifest.read_text(encoding="utf-8") == '{"record_count": 2}'
    assert staged_readme.read_text(encoding="utf-8") == "generated staged view"


class _FailsWhenExhausted(ScriptedProviderAdapter):
    def generate(self, request):
        if not self._responses:
            raise TransientProviderError("fixture outage")
        return super().generate(request)


def test_failed_round_keeps_completed_packet_private(tmp_path):
    repo = _repo(tmp_path)
    runner = _runner(
        repo,
        [
            json.dumps({"claims": ["Claim one.", "Claim two."]}),
            _normalization("Normalized claim one."),
            _normalization("Normalized claim two."),
            _synthesis(),
        ],
        adapter_type=_FailsWhenExhausted,
    )

    with pytest.raises(Exception, match="provider unavailable"):
        runner.run(
            round_id="failure-boundary",
            stage=ResearchStage.PROPOSAL,
            subject_files=(repo.root / "public/task.json",),
        )

    assert not (repo.root / "public/research/rounds/failure-boundary/evidence_snapshot.json").exists()
    assert not (repo.root / "public/research/evidence_packets").exists()
    staged_packets = list(
        (
            repo.root
            / "governance_private/research/rounds/failure-boundary/staging_meeting"
            / "public/research/evidence_packets"
        ).glob("RP-*.json")
    )
    assert len(staged_packets) == 1

    resumed = _runner(repo, [_synthesis()]).run(
        round_id="failure-boundary",
        stage=ResearchStage.PROPOSAL,
        subject_files=(repo.root / "public/task.json",),
    )
    assert resumed.packet_count == 2
    assert (
        repo.root / "public/research/rounds/failure-boundary/evidence_snapshot.json"
    ).exists()


def test_synthesis_qc_failure_is_released_as_unresolved_and_does_not_pause_round(tmp_path):
    repo = _repo(tmp_path)
    runner = _runner(
        repo,
        [
            json.dumps({"claims": ["The bounded result holds under condition C."]}),
            _normalization(),
            '{"not":"a research synthesis"}',
            '{"still":"not a research synthesis"}',
            '{"still":"not a research synthesis after two repairs"}',
        ],
    )

    result = runner.run(
        round_id="qc-nonblocking",
        stage=ResearchStage.PROPOSAL,
        subject_files=(repo.root / "public/task.json",),
    )

    assert result.packet_count == 0
    assert result.qc_failed_claim_count == 1
    assert result.release_disposition == "PARTIAL_WITH_QC_EXCLUSIONS"
    snapshot = json.loads((repo.root / result.evidence_snapshot_path).read_text())
    assert snapshot["status"] == "RELEASED"
    assert snapshot["release_disposition"] == "PARTIAL_WITH_QC_EXCLUSIONS"
    assert snapshot["claim_resolutions"][0]["status"] == "QC_FAILED"
    assert snapshot["claim_resolutions"][0]["failure_code"] == (
        "RESEARCH_SYNTHESIS_SCHEMA_INVALID"
    )
    assert "The bounded result holds" in snapshot["claim_resolutions"][0][
        "normalized_claim"
    ]
    assert runner.engine.status.paused_reason is None
    assert list(
        (repo.root / "audit_private/research/qc_failures/qc-nonblocking").glob("*.json")
    )
    events = repo.events.path.read_text()
    assert "RESEARCH_CLAIM_QC_FAILED_NONBLOCKING" in events
    assert "MODEL_OUTPUT_SCHEMA_REPAIR_FAILED_NONBLOCKING" in events
    assert repo.events.verify()


def test_normalization_qc_failure_does_not_prevent_round_release(tmp_path):
    repo = _repo(tmp_path)
    runner = _runner(
        repo,
        [
            json.dumps({"claims": ["An externally verifiable but malformed claim request."]}),
            '{"not":"a normalized claim"}',
            '{"still":"not a normalized claim"}',
            '{"still":"not a normalized claim after two repairs"}',
        ],
    )

    result = runner.run(
        round_id="normalization-qc-nonblocking",
        stage=ResearchStage.PROPOSAL,
        subject_files=(repo.root / "public/task.json",),
    )

    snapshot = json.loads((repo.root / result.evidence_snapshot_path).read_text())
    resolution = snapshot["claim_resolutions"][0]
    assert result.packet_count == 0
    assert resolution["status"] == "QC_FAILED"
    assert resolution["failure_code"] == "RESEARCH_NORMALIZATION_SCHEMA_INVALID"
    assert resolution["normalized_claim"] == (
        "An externally verifiable but malformed claim request."
    )
    assert runner.engine.status.paused_reason is None
    assert repo.events.verify()


def test_submission_scheduler_serializes_personas_per_model_and_parallelizes_models(
    tmp_path, monkeypatch
):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "model-a"), ("fake", "model-b")],
        chair_model=("fake", "model-a"),
        governance_docs="docs/governance",
        task_description="Assess scheduling.",
        personas=[Persona.SYSTEMS_INTEGRATOR, Persona.PRAGMATIC_MINIMALIST],
        research_enabled=True,
        research_model=("fake", "research"),
        research_reasoning_effort=ReasoningEffort.LOW,
    )
    runner = _runner(repo, [])
    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    guard = threading.Lock()
    active_models = set()
    peak_active_models = 0
    same_model_overlap = []
    observed_order = {"model-a": [], "model-b": []}

    def fake_collect_one_submission(
        *, round_id, record, subject_files, max_claims_per_representative
    ):
        nonlocal peak_active_models
        model_id = record["runtime"]["model_id"]
        persona = record["runtime"]["persona"]
        with guard:
            if model_id in active_models:
                same_model_overlap.append(model_id)
            active_models.add(model_id)
            observed_order[model_id].append(persona)
            peak_active_models = max(peak_active_models, len(active_models))
        time.sleep(0.03)
        with guard:
            active_models.remove(model_id)
        return record["representative_id"], ResearchRoundSubmission()

    monkeypatch.setattr(runner, "_collect_one_submission", fake_collect_one_submission)
    submissions = runner._collect_submissions(
        round_id="lane-scheduling",
        records=registry,
        subject_files=(repo.root / "public/task.json",),
        max_claims_per_representative=4,
    )

    assert len(submissions) == 4
    assert not same_model_overlap
    assert peak_active_models == 2
    assert observed_order == {
        "model-a": ["systems_integrator", "pragmatic_minimalist"],
        "model-b": ["systems_integrator", "pragmatic_minimalist"],
    }


def test_deduplicated_claim_groups_retrieve_in_parallel_and_release_in_frozen_order(
    tmp_path
):
    repo = _repo(tmp_path)

    class ParallelRetriever(_Retriever):
        def __init__(self):
            self.barrier = threading.Barrier(2)
            self.guard = threading.Lock()
            self.active = 0
            self.peak = 0

        def retrieve(self, claim):
            with self.guard:
                self.active += 1
                self.peak = max(self.peak, self.active)
            self.barrier.wait(timeout=1)
            try:
                return super().retrieve(claim)
            finally:
                with self.guard:
                    self.active -= 1

    retriever = ParallelRetriever()
    adapter = ScriptedProviderAdapter(
        "fake",
        ["representative", "research"],
        [
            json.dumps(
                {
                    "claims": [
                        "The first bounded result holds under condition C.",
                        "The second bounded result holds under condition D.",
                    ]
                }
            ),
            _normalization("The first bounded result holds under condition C."),
            _normalization("The second bounded result holds under condition D."),
            _synthesis(),
            _synthesis(),
        ],
    )
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": adapter},
        notifier=_Notifier(),
        max_retries=0,
    )
    runner = ResearchRoundRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        retriever=retriever,
        document_fetcher=_NeverFetch(),
        max_output_tokens=1024,
        max_concurrent_claim_groups=2,
    )

    result = runner.run(
        round_id="parallel-claim-groups",
        stage=ResearchStage.PROPOSAL,
        subject_files=(repo.root / "public/task.json",),
    )

    assert retriever.peak == 2
    assert result.packet_count == 2
    snapshot = json.loads((repo.root / result.evidence_snapshot_path).read_text())
    assert [item["claim_id"] for item in snapshot["claim_resolutions"]] == sorted(
        item["claim_id"] for item in snapshot["claim_resolutions"]
    )
    assert repo.events.verify()


def test_parallel_claim_group_failure_preserves_other_completed_resolution(tmp_path):
    repo = _repo(tmp_path)

    class PartiallyFailingRetriever(_Retriever):
        def __init__(self):
            self.barrier = threading.Barrier(2)

        def retrieve(self, claim):
            self.barrier.wait(timeout=1)
            if "first" in claim.normalized_claim.casefold():
                raise TransientProviderError("one claim-specific retrieval failed")
            return super().retrieve(claim)

    adapter = ScriptedProviderAdapter(
        "fake",
        ["representative", "research"],
        [
            json.dumps(
                {
                    "claims": [
                        "The first bounded result holds under condition C.",
                        "The second bounded result holds under condition D.",
                    ]
                }
            ),
            _normalization("The first bounded result holds under condition C."),
            _normalization("The second bounded result holds under condition D."),
            _synthesis(),
        ],
    )
    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": adapter},
        notifier=_Notifier(),
        max_retries=0,
    )
    runner = ResearchRoundRunner(
        repo=repo,
        engine=engine,
        governance_docs="docs/governance",
        retriever=PartiallyFailingRetriever(),
        document_fetcher=_NeverFetch(),
        max_output_tokens=1024,
        max_concurrent_claim_groups=2,
    )

    with pytest.raises(RepresentativeUnavailableError, match="claim-specific"):
        runner.run(
            round_id="partial-parallel-failure",
            stage=ResearchStage.PROPOSAL,
            subject_files=(repo.root / "public/task.json",),
        )

    resolutions = list(
        (
            repo.root
            / "governance_private/research/rounds/partial-parallel-failure/resolutions"
        ).glob("*.json")
    )
    assert len(resolutions) == 1
    assert not (
        repo.root
        / "public/research/rounds/partial-parallel-failure/evidence_snapshot.json"
    ).exists()
    assert repo.events.verify()
