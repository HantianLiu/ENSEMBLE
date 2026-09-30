import json
import zipfile

import pytest
from pydantic import ValidationError

from project_ensemble.domain import GenerationResponse, ReasoningEffort
from project_ensemble.errors import (
    ResearchQualityControlError,
    ResearchRequestRejectedError,
    TransientProviderError,
)
from project_ensemble.research.desk import ResearchDesk
from project_ensemble.research.citations import validate_evidence_citations
from project_ensemble.research.documents import DownloadedDocument
from project_ensemble.research.models import (
    CacheInvalidationAuthority,
    CacheInvalidationReason,
    ClaimReuseAssessment,
    EvidenceCitation,
    EvidencePacket,
    KnowledgeStatus,
    LiteratureConsensus,
    ResearchRequest,
    ResearchStage,
)
from project_ensemble.research.retrievers import CompositeRetriever
from project_ensemble.storage.meeting import MeetingRepository


class FakeEngine:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.requests = []

    def invoke_participant(self, participant_id, **kwargs):
        self.calls.append((participant_id, kwargs["stage"]))
        self.requests.append(kwargs)
        return GenerationResponse(
            text=self.responses.pop(0),
            provider_id="fake",
            model_id="research-model",
        )

    def validate_structured_response(self, participant_id, *, response, schema_model, **kwargs):
        return schema_model.model_validate_json(response.text)


def test_configured_academic_writer_may_request_research_but_other_meetings_may_not(tmp_path):
    identity = tmp_path / "identity_private"
    identity.mkdir()
    desk = ResearchDesk.__new__(ResearchDesk)
    desk.repo = type("Repo", (), {"root": tmp_path})()
    manifest = {
        "meeting_type": "deliberation", "deliverable_type": "literature_review",
        "literature_writing_policy": "v071", "writer_model": ["fake", "writer"],
    }
    path = identity / "meeting_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    desk._assert_requester("WRITER")
    manifest["literature_writing_policy"] = "legacy"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="not a registered meeting participant"):
        desk._assert_requester("WRITER")
    manifest["literature_writing_policy"] = "v071"
    manifest["writer_model"] = None
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="not a registered meeting participant"):
        desk._assert_requester("WRITER")


class FakeRetriever:
    backend_ids = ("fake",)

    def __init__(self):
        self.calls = 0

    def retrieve(self, claim):
        self.calls += 1
        candidate = {
            "source_id": "https://openalex.org/W1",
            "title": "A bounded test paper",
            "authors": ["Researcher"],
            "publication_year": 2025,
            "doi": "https://doi.org/10.1/test",
            "url": "https://doi.org/10.1/test",
            "venue": "Test Journal",
            "source_type": "article",
            "is_primary_source": True,
            "abstract": "The bounded claim was tested under one stated condition.",
            "cited_by_count": 1,
            "is_retracted": False,
            "retrieval_purposes": ["supporting", "limitations"],
        }
        return [candidate], [
            {
                "purpose": purpose,
                "query": f"bounded claim {purpose}",
                "returned_source_ids": [candidate["source_id"]] if purpose == "supporting" else [],
            }
            for purpose in ("supporting", "contradictory", "limitations", "alternatives")
        ]


class FakePublicDocumentRetriever(FakeRetriever):
    def retrieve(self, claim):
        candidates, trace = super().retrieve(claim)
        candidates[0].update(
            {
                "full_text_url": "https://example.test/paper.pdf",
                "full_text_is_public": True,
                "license": "cc-by",
            }
        )
        return candidates, trace


class TwoCandidateRetriever(FakeRetriever):
    def retrieve(self, claim):
        candidates, trace = super().retrieve(claim)
        second = dict(candidates[0])
        second.update(
            {
                "source_id": "https://openalex.org/W2",
                "title": "A screened but uncited comparison paper",
                "doi": "https://doi.org/10.1/comparison",
                "url": "https://doi.org/10.1/comparison",
                "abstract": "The comparison uses a materially different condition.",
            }
        )
        candidates.append(second)
        trace[0]["returned_source_ids"].append(second["source_id"])
        return candidates, trace


class FakeOpenAlexOnlyRetriever(FakeRetriever):
    backend_ids = ("openalex",)


class FlakyRetriever(FakeRetriever):
    def __init__(self):
        super().__init__()
        self.failures_remaining = 1

    def retrieve(self, claim):
        if self.failures_remaining:
            self.failures_remaining -= 1
            from project_ensemble.errors import TransientProviderError

            raise TransientProviderError("temporary fixture failure")
        return super().retrieve(claim)


class UnavailableOpenAlexRetriever:
    backend_ids = ("openalex",)

    def retrieve(self, claim):
        raise TransientProviderError("OpenAlex returned HTTP 429")


class FakeDocumentFetcher:
    def __init__(self):
        self.calls = []

    def fetch(self, url):
        from io import BytesIO
        from reportlab.pdfgen import canvas

        self.calls.append(url)
        buffer = BytesIO()
        document = canvas.Canvas(buffer)
        document.drawString(50, 750, "The bounded claim holds under condition C.")
        document.save()
        return DownloadedDocument(
            content=buffer.getvalue(),
            media_type="application/pdf",
            final_url=url,
        )


def _normalization_json():
    return json.dumps(
        {
            "is_researchable": True,
            "normalized_claim": "The bounded claim holds under condition C.",
            "verification_question": "Does the bounded claim hold under condition C?",
            "supporting_query": "bounded claim condition C",
            "contradictory_query": "bounded claim failure condition C",
            "limitations_query": "bounded claim limitations condition C",
            "alternatives_query": "bounded claim canonical alternatives",
            "scope_terms": ["condition C"],
            "source_domain": "ACADEMIC",
            "source_domain_rationale": "The claim concerns a scholarly experimental result.",
            "freshness_class": "STABLE",
            "freshness_rationale": "This is a slowly changing scholarly claim.",
            "rejection_reason": None,
        }
    )


def _rejected_normalization_json():
    return json.dumps(
        {
            "is_researchable": False,
            "normalized_claim": "Internal coverage should be reassessed.",
            "verification_question": "Is the internal packet list complete?",
            "supporting_query": "internal packet list",
            "contradictory_query": "internal packet list",
            "limitations_query": "internal packet list",
            "alternatives_query": "internal packet list",
            "scope_terms": ["internal coverage"],
            "source_domain": "GENERAL",
            "source_domain_rationale": "This is an internal administrative request.",
            "freshness_class": "STABLE",
            "freshness_rationale": "Not applicable to external evidence.",
            "rejection_reason": "The request is administrative rather than an external factual claim.",
        }
    )


def test_non_claim_rejection_is_typed_for_caller_requeue(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    desk = ResearchDesk(
        repo=repo,
        engine=FakeEngine([_rejected_normalization_json()]),
        retriever=FakeRetriever(),
    )

    with pytest.raises(ResearchRequestRejectedError, match="administrative"):
        desk.research(
            ResearchRequest(
                requester_id=requester,
                stage=ResearchStage.PROPOSAL,
                claim="Please re-judge coverage and list packet IDs.",
            )
        )

    assert "RESEARCH_REQUEST_REJECTED" in repo.events.path.read_text()


def _synthesis_json(knowledge_status="QUALIFIED", coverage="LOW"):
    finding = {
        "source_id": "https://openalex.org/W1",
        "direction": "SUPPORTING",
        "evidence_summary": "One study reports support.",
        "applicability": "Condition C only.",
        "limitations": "Single source and narrow scope.",
    }
    return json.dumps(
        {
            "packet": {
                "packet_id": "PENDING",
                "claim_fingerprint": "0" * 64,
                "original_claim": "placeholder",
                "normalized_claim": "placeholder",
                "verification_question": "Does the bounded claim hold under condition C?",
                "scope_terms": ["condition C"],
                "source_domain": "ACADEMIC",
                "retrieval_backend_ids": ["fake"],
                "retrieved_at": "2026-09-18T00:00:00Z",
                "search_scope": "OpenAlex scholarly metadata and available abstracts.",
                "sources": [
                    {
                        "source_id": "https://openalex.org/W1",
                        "title": "A bounded test paper",
                        "authors": ["Researcher"],
                        "publication_year": 2025,
                        "doi": "https://doi.org/10.1/test",
                        "url": "https://doi.org/10.1/test",
                        "venue": "Test Journal",
                        "source_type": "article",
                        "is_primary_source": True,
                        "evidence_use_class": "PRIMARY",
                    }
                ],
                "supporting_evidence": [finding],
                "contradictory_evidence": [],
                "scope_limitations": [],
                "canonical_alternatives": [],
                "counter_search_summary": "Contradictory, limitation, and alternative queries were run; none found in this bounded fixture.",
                "evidence_conflict_assessment": "No true conflict was identified in the bounded retrieval.",
                "consensus": "INSUFFICIENT",
                "unresolved_questions": ["Whether the result generalizes beyond condition C."],
                "confidence": {
                    "coverage": coverage,
                    "source_quality": "MEDIUM",
                    "literature_consistency": "LOW",
                    "rationale": "Only one source was available.",
                },
                "knowledge_status": knowledge_status,
            },
            "screening_decisions": [
                {
                    "source_id": "https://openalex.org/W1",
                    "included": True,
                    "rationale": "Directly addresses the bounded claim.",
                }
            ],
        }
    )


def _synthesis_json_with_unretrieved_limitation():
    data = json.loads(_synthesis_json())
    source_id = "https://openalex.org/W-NOT-RETRIEVED"
    data["packet"]["sources"].append(
        {
            "source_id": source_id,
            "title": "A model-prior survey not returned by retrieval",
            "authors": ["Model Memory"],
            "publication_year": 2025,
            "doi": None,
            "url": "https://example.test/not-retrieved",
            "venue": None,
            "source_type": "article",
            "is_primary_source": False,
            "evidence_use_class": "REVIEW",
        }
    )
    data["packet"]["scope_limitations"].append(
        {
            "source_id": source_id,
            "direction": "LIMITATION",
            "evidence_summary": "A model-prior survey reports a limitation.",
            "applicability": "The source was not returned by this retrieval.",
            "limitations": "Its use is therefore not auditable.",
        }
    )
    data["screening_decisions"].append(
        {
            "source_id": source_id,
            "included": True,
            "rationale": "Improperly introduced from model memory.",
        }
    )
    return json.dumps(data)


def test_synthesis_repair_diagnostic_names_bad_citations_and_duplicate_sources():
    data = json.loads(_synthesis_json_with_unretrieved_limitation())
    data["packet"]["sources"].append(dict(data["packet"]["sources"][0]))
    data["packet"]["supporting_evidence"].append(
        {
            "source_id": "https://openalex.org/W3",
            "direction": "SUPPORTING",
            "evidence_summary": "Unsupported assertion",
            "applicability": "unknown",
            "limitations": "unknown",
        }
    )
    candidates, _ = FakeRetriever().retrieve(None)

    diagnostic = ResearchDesk._synthesis_source_diagnostic(
        json.dumps(data), candidates
    )

    assert "packet.sources[1].source_id='https://openalex.org/W-NOT-RETRIEVED'" in diagnostic
    assert "packet.sources[2].source_id='https://openalex.org/W1'" in diagnostic
    assert "packet.supporting_evidence[1].source_id='https://openalex.org/W3'" in diagnostic
    assert "screening_decisions.source_id='https://openalex.org/W-NOT-RETRIEVED'" in diagnostic


def test_research_desk_publishes_packet_hides_trace_and_reuses_fresh_cache(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    engine = FakeEngine([_normalization_json(), _synthesis_json(), _normalization_json()])
    retriever = FakeRetriever()
    desk = ResearchDesk(repo=repo, engine=engine, retriever=retriever)
    request = ResearchRequest(
        requester_id=requester,
        stage=ResearchStage.PROPOSAL,
        claim="Does the bounded claim hold under condition C?",
    )

    first = desk.research(request)
    second = desk.research(request)

    assert first.packet_id == second.packet_id
    assert first.original_claim == "Does the bounded claim hold under condition C?"
    assert first.normalized_claim == "The bounded claim holds under condition C."
    assert first.verification_question == "Does the bounded claim hold under condition C?"
    assert first.scope_terms == ["condition C"]
    assert first.source_domain.value == "ACADEMIC"
    assert first.retrieval_backend_ids == ["fake"]
    assert first.freshness_class.value == "STABLE"
    assert first.maximum_cache_reuse_age_days == 180
    assert first.cache_expires_at is not None
    assert (first.cache_expires_at - first.retrieved_at).days == 180
    assert first.sources[0].evidence_use_class.value == "PROVISIONAL"
    assert retriever.calls == 1
    assert len(engine.calls) == 3
    synthesis_request = next(
        item for item in engine.requests if item["stage"] == "research_evidence_synthesis"
    )
    assert "严格来源边界" in synthesis_request["system_text"]
    assert "允许引用的候选 source_id" in synthesis_request["user_text"]
    assert "https://openalex.org/W1" in synthesis_request["user_text"]
    public_text = next((repo.root / "public/research/evidence_packets").glob("*.json")).read_text()
    assert "screening_decisions" not in public_text
    assert requester not in public_text
    assert '"stage"' not in public_text
    trace_text = next((repo.root / "audit_private/research/traces").glob("*.json")).read_text()
    assert "screening_decisions" in trace_text
    assert "RESEARCH_EVIDENCE_PACKET_REUSED" in repo.events.path.read_text()
    assert repo.events.verify()


def test_resumed_meeting_refreshes_legacy_packet_without_rewriting_it(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs=gov,
        research_enabled=True, research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    request = ResearchRequest(
        requester_id=requester, stage=ResearchStage.PROPOSAL,
        claim="Does the bounded claim hold under condition C?",
    )
    first = ResearchDesk(
        repo=repo, engine=FakeEngine([_normalization_json(), _synthesis_json()]),
        retriever=FakeRetriever(),
    ).research(request)
    original_path = repo.root / "public/research/evidence_packets" / f"{first.packet_id}.json"
    legacy = json.loads(original_path.read_text(encoding="utf-8"))
    legacy.pop("source_reading_performed")
    original_path.write_text(json.dumps(legacy), encoding="utf-8")
    original_bytes = original_path.read_bytes()

    resumed = ResearchDesk(
        repo=MeetingRepository(repo.root),
        engine=FakeEngine([_normalization_json(), _synthesis_json()]),
        retriever=FakeRetriever(),
    ).research(request)

    assert resumed.packet_id != first.packet_id
    assert resumed.source_reading_performed is True
    assert original_path.read_bytes() == original_bytes
    assert first.packet_id in resumed.supersedes_packet_ids


def test_research_desk_continues_with_surviving_backend_and_downgrades_coverage(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    engine = FakeEngine([_normalization_json(), _synthesis_json(coverage="HIGH")])
    retriever = CompositeRetriever([UnavailableOpenAlexRetriever(), FakeRetriever()])
    desk = ResearchDesk(repo=repo, engine=engine, retriever=retriever)

    packet = desk.research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    assert packet.retrieval_backend_ids == ["fake"]
    assert packet.confidence.coverage.value == "LOW"
    assert "unavailable backend(s) openalex" in packet.confidence.rationale
    trace = json.loads(next((repo.root / "audit_private/research/traces").glob("*.json")).read_text())
    assert trace["failed_retrieval_backend_ids"] == ["openalex"]
    assert "RESEARCH_RETRIEVAL_BACKEND_DEGRADED" in repo.events.path.read_text()
    assert repo.events.verify()


def test_research_desk_removes_unretrieved_source_and_findings_without_model_rewrite(
    tmp_path,
):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    engine = FakeEngine([_normalization_json(), _synthesis_json_with_unretrieved_limitation()])
    desk = ResearchDesk(repo=repo, engine=engine, retriever=FakeRetriever())

    packet = desk.research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    assert [source.source_id for source in packet.sources] == ["https://openalex.org/W1"]
    assert packet.scope_limitations == []
    assert len(engine.calls) == 2
    events = repo.events.path.read_text()
    assert "RESEARCH_UNRETRIEVED_SOURCES_REMOVED" in events
    assert "RESEARCH_SCREENING_DECISIONS_RECONCILED" in events
    assert repo.events.verify()


def test_research_desk_places_stable_schemas_before_variable_claim_content(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    engine = FakeEngine([_normalization_json(), _synthesis_json()])
    desk = ResearchDesk(repo=repo, engine=engine, retriever=FakeRetriever())

    desk.research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    normalization, synthesis = engine.requests
    assert normalization["max_output_tokens"] is None
    assert synthesis["max_output_tokens"] is None
    assert "目标 JSON Schema" in normalization["system_text"]
    assert "目标 JSON Schema" not in normalization["user_text"]
    assert "原始主张" in normalization["user_text"]
    assert "四类对冲文献检索式" in normalization["system_text"]
    assert "目标 JSON Schema" in synthesis["system_text"]
    assert "固定输出要求" in synthesis["system_text"]
    assert "目标 JSON Schema" not in synthesis["user_text"]
    assert "候选来源" in synthesis["user_text"]
    assert "只使用给定候选来源的元数据和摘要" in synthesis["system_text"]


def test_research_desk_repairs_only_missing_screening_decisions(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    supplement = json.dumps(
        {
            "screening_decisions": [
                {
                    "source_id": "https://openalex.org/W2",
                    "included": False,
                    "rationale": "The study uses a materially different condition.",
                }
            ]
        }
    )
    engine = FakeEngine([_normalization_json(), _synthesis_json(), supplement])
    desk = ResearchDesk(
        repo=repo,
        engine=engine,
        retriever=TwoCandidateRetriever(),
        max_output_tokens=12345,
    )

    packet = desk.research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    assert packet.sources[0].source_id == "https://openalex.org/W1"
    assert engine.calls[-1] == ("RESEARCH_DESK", "research_screening_decision_repair")
    assert all(request["max_output_tokens"] == 12345 for request in engine.requests)
    trace = json.loads(next((repo.root / "audit_private/research/traces").glob("*.json")).read_text())
    assert [item["source_id"] for item in trace["screening_decisions"]] == [
        "https://openalex.org/W1",
        "https://openalex.org/W2",
    ]
    assert "RESEARCH_SCREENING_DECISIONS_RECONCILED" in repo.events.path.read_text()
    assert repo.events.verify()


def test_research_desk_quarantines_bad_screening_item_without_rejecting_packet(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    bad_supplement = json.dumps(
        {
            "screening_decisions": [
                {
                    "source_id": "https://openalex.org/NOT_REQUESTED",
                    "included": False,
                    "rationale": "Wrong source identifier.",
                }
            ]
        }
    )
    engine = FakeEngine([_normalization_json(), _synthesis_json(), bad_supplement])
    desk = ResearchDesk(repo=repo, engine=engine, retriever=TwoCandidateRetriever())

    packet = desk.research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    assert [source.source_id for source in packet.sources] == ["https://openalex.org/W1"]
    trace = json.loads(next((repo.root / "audit_private/research/traces").glob("*.json")).read_text())
    w2 = next(
        item
        for item in trace["screening_decisions"]
        if item["source_id"] == "https://openalex.org/W2"
    )
    assert w2["included"] is False
    assert "QC quarantine" in w2["rationale"]
    assert "RESEARCH_SCREENING_ITEMS_QUARANTINED" in repo.events.path.read_text()


def test_stale_exact_claim_creates_new_packet_with_supersession_link(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    engine = FakeEngine(
        [_normalization_json(), _synthesis_json(), _normalization_json(), _synthesis_json()]
    )
    retriever = FakeRetriever()
    desk = ResearchDesk(repo=repo, engine=engine, retriever=retriever)
    request = ResearchRequest(
        requester_id=requester,
        stage=ResearchStage.PROPOSAL,
        claim="Does the bounded claim hold under condition C?",
    )

    first = desk.research(request)
    first_path = next((repo.root / "public/research/evidence_packets").glob("*.json"))
    first_data = json.loads(first_path.read_text())
    first_data["retrieved_at"] = "2000-01-01T00:00:00Z"
    first_data["cache_expires_at"] = "2000-06-29T00:00:00Z"
    first_path.write_text(json.dumps(first_data))

    second = desk.research(request)

    assert second.packet_id != first.packet_id
    assert second.supersedes_packet_ids == [first.packet_id]
    assert len(list((repo.root / "public/research/evidence_packets").glob("*.json"))) == 2
    assert retriever.calls == 2


def test_forced_recheck_bypasses_fresh_cache_and_resets_expiry(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    retriever = FakeRetriever()
    desk = ResearchDesk(
        repo=repo,
        engine=FakeEngine(
            [_normalization_json(), _synthesis_json(), _normalization_json(), _synthesis_json()]
        ),
        retriever=retriever,
    )
    first = desk.research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.CHALLENGE,
            claim="Does the bounded claim hold under condition C?",
        )
    )
    second = desk.research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.CHALLENGE,
            claim="Does the bounded claim hold under condition C?",
            force_refresh=True,
            refresh_reason="Check for newly published contradictory evidence.",
        )
    )

    assert second.packet_id != first.packet_id
    assert second.supersedes_packet_ids == [first.packet_id]
    assert second.retrieved_at >= first.retrieved_at
    assert second.cache_expires_at is not None
    assert (second.cache_expires_at - second.retrieved_at).days == 180
    assert retriever.calls == 2


def test_traceable_invalidation_prevents_future_cache_reuse(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    retriever = FakeRetriever()
    desk = ResearchDesk(
        repo=repo,
        engine=FakeEngine(
            [_normalization_json(), _synthesis_json(), _normalization_json(), _synthesis_json()]
        ),
        retriever=retriever,
    )
    request = ResearchRequest(
        requester_id=requester,
        stage=ResearchStage.PROPOSAL,
        claim="Does the bounded claim hold under condition C?",
    )
    first = desk.research(request)
    desk.packet_cache.invalidate(
        packet_id=first.packet_id,
        authority=CacheInvalidationAuthority.RESEARCH_DESK,
        reason=CacheInvalidationReason.RETRACTION,
        rationale="The publisher marks the source as retracted.",
        evidence_url="https://example.test/retraction-notice",
    )
    second = desk.research(request)

    assert second.packet_id != first.packet_id
    assert second.supersedes_packet_ids == [first.packet_id]
    assert retriever.calls == 2


def test_research_desk_invalidation_requires_evidence_url():
    from datetime import datetime, timezone

    from project_ensemble.research.models import ResearchCacheInvalidation

    with pytest.raises(ValidationError, match="requires evidence_url"):
        ResearchCacheInvalidation(
            packet_id="RP-1",
            invalidated_at=datetime.now(timezone.utc),
            authority="RESEARCH_DESK",
            reason="CORRECTION",
            rationale="Correction reported without a traceable link.",
        )


def test_source_backed_rejects_discovery_only_sources():
    packet_data = json.loads(_synthesis_json())["packet"]
    packet_data["knowledge_status"] = "SOURCE_BACKED"
    packet_data["sources"][0]["evidence_use_class"] = "DISCOVERY_ONLY"

    with pytest.raises(ValidationError, match="SOURCE_BACKED requires"):
        EvidencePacket.model_validate(packet_data)


def test_knowledge_status_requires_matching_minimum_evidence():
    packet_data = json.loads(_synthesis_json())["packet"]
    packet_data["supporting_evidence"] = []
    with pytest.raises(ValidationError, match="QUALIFIED requires"):
        EvidencePacket.model_validate(packet_data)

    packet_data["knowledge_status"] = "UNRESOLVED"
    packet_data["unresolved_questions"] = []
    with pytest.raises(ValidationError, match="UNRESOLVED requires"):
        EvidencePacket.model_validate(packet_data)


def test_clear_consensus_rejects_one_primary_study():
    packet_data = json.loads(_synthesis_json())["packet"]
    packet_data["consensus"] = "CLEAR"

    with pytest.raises(ValidationError, match="CLEAR consensus requires"):
        EvidencePacket.model_validate(packet_data)


def test_final_packet_validation_is_claim_scoped_qc():
    packet_data = json.loads(_synthesis_json())["packet"]
    packet = EvidencePacket.model_validate(packet_data).model_copy(
        update={"consensus": LiteratureConsensus.CLEAR}
    )

    with pytest.raises(ResearchQualityControlError) as caught:
        ResearchDesk._validate_final_packet(packet)
    assert caught.value.code == "RESEARCH_EVIDENCE_PACKET_FINAL_VALIDATION_FAILED"


def test_clear_consensus_is_downgraded_without_discarding_source_backed_packet():
    packet = EvidencePacket.model_validate(json.loads(_synthesis_json())["packet"])
    packet = packet.model_copy(
        update={
            "consensus": LiteratureConsensus.CLEAR,
            "knowledge_status": KnowledgeStatus.SOURCE_BACKED,
        }
    )
    downgraded = ResearchDesk._downgrade_unsupported_clear_consensus(packet)
    assert downgraded.consensus == LiteratureConsensus.QUALIFIED
    assert downgraded.knowledge_status == KnowledgeStatus.SOURCE_BACKED
    assert "downgraded to QUALIFIED" in downgraded.evidence_conflict_assessment
    assert ResearchDesk._validate_final_packet(downgraded).knowledge_status == KnowledgeStatus.SOURCE_BACKED


def test_mixed_consensus_requires_both_directions():
    packet_data = json.loads(_synthesis_json())["packet"]
    packet_data["consensus"] = "MIXED"

    with pytest.raises(ValidationError, match="MIXED consensus requires"):
        EvidencePacket.model_validate(packet_data)


def test_source_backed_can_have_low_coverage_without_aggregate_confidence():
    packet_data = json.loads(_synthesis_json())["packet"]
    packet_data["knowledge_status"] = "SOURCE_BACKED"
    packet = EvidencePacket.model_validate(packet_data)
    assert packet.confidence.coverage.value == "LOW"

    packet_data["confidence"]["overall"] = "HIGH"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvidencePacket.model_validate(packet_data)


def test_only_exact_claim_equivalence_allows_automatic_cache_reuse():
    exact = ClaimReuseAssessment(
        candidate_packet_id="RP-1",
        relation="EXACT_EQUIVALENT",
        automatic_reuse_allowed=True,
        rationale="The normalized proposition and material scope are identical.",
    )
    assert exact.automatic_reuse_allowed is True

    with pytest.raises(ValidationError, match="only for EXACT_EQUIVALENT"):
        ClaimReuseAssessment(
            candidate_packet_id="RP-1",
            relation="BROADER",
            automatic_reuse_allowed=True,
            rationale="The requested claim adds another population.",
        )


def test_public_source_document_is_archived_in_human_download_bundle(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    fetcher = FakeDocumentFetcher()
    packet = ResearchDesk(
        repo=repo,
        engine=FakeEngine([_normalization_json(), _synthesis_json("SOURCE_BACKED")]),
        retriever=FakePublicDocumentRetriever(),
        document_fetcher=fetcher,
    ).research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    source = packet.sources[0]
    assert packet.knowledge_status.value == "SOURCE_BACKED"
    assert source.evidence_use_class.value == "PRIMARY"
    assert source.archive_status.value == "ARCHIVED"
    assert "综合前取得相关原文摘录 1 项" in packet.full_text_access_assessment
    read_records = list((repo.root / "audit_private/research/source_reads").glob("*.json"))
    assert len(read_records) == 1
    read_record = json.loads(read_records[0].read_text(encoding="utf-8"))
    assert read_record["status"] == "READABLE_EXCERPT"
    assert "bounded claim holds" in read_record["excerpts"][0]["text"]
    assert source.archived_path is not None
    assert (repo.root / source.archived_path).read_bytes().startswith(b"%PDF-")
    manifest = json.loads(
        (repo.root / "public/research/literature_bundle/manifest.json").read_text()
    )
    assert manifest["scope"] == "THIS_MEETING_ONLY"
    assert manifest["records"][0]["packet_id"] == packet.packet_id
    assert manifest["records"][0]["archived_sha256"] == source.archived_sha256
    citation = EvidenceCitation(
        packet_id=packet.packet_id,
        source_ids=[source.source_id],
        kind="DIRECT_SOURCE",
    )
    validate_evidence_citations(repo, [citation])
    with pytest.raises(ValueError, match="sources absent"):
        validate_evidence_citations(
            repo,
            [
                EvidenceCitation(
                    packet_id=packet.packet_id,
                    source_ids=["SOURCE-NOT-IN-PACKET"],
                    kind="DIRECT_SOURCE",
                )
            ],
        )
    with zipfile.ZipFile(repo.root / "public/research/literature_bundle.zip") as bundle:
        assert "manifest.json" in bundle.namelist()
        assert any(name.endswith(".pdf") for name in bundle.namelist())


def test_evidence_citation_preserves_packet_source_and_inference_boundary():
    citation = EvidenceCitation(
        packet_id="RP-1",
        source_ids=["SOURCE-1"],
        kind="REPRESENTATIVE_INFERENCE",
    )
    assert citation.source_ids == ["SOURCE-1"]


def test_metadata_only_source_is_provisional_and_cannot_publish_source_backed(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    packet = ResearchDesk(
        repo=repo,
        engine=FakeEngine([_normalization_json(), _synthesis_json("SOURCE_BACKED")]),
        retriever=FakeRetriever(),
    ).research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    assert packet.sources[0].archive_status.value == "NOT_AVAILABLE"
    assert packet.sources[0].evidence_use_class.value == "PROVISIONAL"
    assert packet.knowledge_status.value == "QUALIFIED"
    assert "已归档所引来源原件 0/1" in packet.full_text_access_assessment


def test_openalex_only_retrieval_forces_low_coverage(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    packet = ResearchDesk(
        repo=repo,
        engine=FakeEngine([_normalization_json(), _synthesis_json(coverage="HIGH")]),
        retriever=FakeOpenAlexOnlyRetriever(),
    ).research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    assert packet.retrieval_backend_ids == ["openalex"]
    assert packet.confidence.coverage.value == "LOW"
    assert "OpenAlex-only" in packet.confidence.rationale


def test_research_retrieval_retries_transient_backend_failure(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        research_enabled=True,
        research_model=("fake", "research-model"),
        research_reasoning_effort=ReasoningEffort.MEDIUM,
    )
    requester = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )[0]["representative_id"]
    retriever = FlakyRetriever()
    packet = ResearchDesk(
        repo=repo,
        engine=FakeEngine([_normalization_json(), _synthesis_json()]),
        retriever=retriever,
        retrieval_max_retries=1,
        retrieval_retry_base_delay_seconds=0,
    ).research(
        ResearchRequest(
            requester_id=requester,
            stage=ResearchStage.PROPOSAL,
            claim="Does the bounded claim hold under condition C?",
        )
    )

    assert packet.packet_id.startswith("RP-")
    assert retriever.calls == 1
    assert retriever.failures_remaining == 0
