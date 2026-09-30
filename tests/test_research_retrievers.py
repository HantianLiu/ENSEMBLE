import threading

import httpx
import pytest

from project_ensemble.errors import TransientProviderError
from project_ensemble.research.models import NormalizedClaim
from project_ensemble.research.retrievers import CompositeRetriever, ResearchRetrievalResult, TavilyRetriever


def test_tavily_extract_reads_selected_url_and_not_search_snippet(monkeypatch):
    requested = []

    def handler(request):
        requested.append((str(request.url), request.content))
        return httpx.Response(200, json={
            "results": [{
                "url": "https://example.test/standard",
                "raw_content": "Section 3 defines the test method and its scope.",
            }],
            "failed_results": [],
            "request_id": "extract-1",
        })

    transport = httpx.MockTransport(handler)
    client_class = httpx.Client
    monkeypatch.setattr(
        "project_ensemble.research.retrievers.httpx.Client",
        lambda **kwargs: client_class(transport=transport),
    )
    result = TavilyRetriever(api_key="secret").extract_url("https://example.test/standard")
    assert result["raw_content"].startswith("Section 3")
    assert requested[0][0].endswith("/extract")
    assert b'"extract_depth":"advanced"' in requested[0][1]


def test_tavily_extract_does_not_accept_other_url_or_empty_result(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "results": [{"url": "https://other.test/standard", "raw_content": "Unrelated"}],
            "failed_results": [{"url": "https://example.test/standard", "error": "unavailable"}],
        })

    transport = httpx.MockTransport(handler)
    client_class = httpx.Client
    monkeypatch.setattr(
        "project_ensemble.research.retrievers.httpx.Client",
        lambda **kwargs: client_class(transport=transport),
    )
    with pytest.raises(ValueError, match="no readable content"):
        TavilyRetriever(api_key="secret").extract_url("https://example.test/standard")


def _claim(source_domain="ACADEMIC"):
    return NormalizedClaim(
        is_researchable=True,
        normalized_claim="A bounded claim.",
        verification_question="Is the bounded claim supported?",
        supporting_query="bounded claim evidence",
        contradictory_query="bounded claim contradiction",
        limitations_query="bounded claim limitations",
        alternatives_query="bounded claim alternatives",
        scope_terms=["bounded"],
        source_domain=source_domain,
        source_domain_rationale="Fixture domain.",
        freshness_class="STABLE",
        freshness_rationale="Fixture freshness.",
    )


def test_tavily_executes_all_adversarial_queries_and_keeps_provenance(monkeypatch):
    retriever = TavilyRetriever(api_key="secret", max_results_per_query=4)
    calls = []
    request_ids = {
        "bounded claim evidence": "request-supporting",
        "bounded claim contradiction": "request-contradictory",
        "bounded claim limitations": "request-limitations",
        "bounded claim alternatives": "request-alternatives",
    }

    def fake_search(query, *, topic):
        calls.append((query, topic))
        return {
            "results": [
                {
                    "title": "Official bounded source",
                    "url": "https://example.test/source",
                    "content": f"Source excerpt for {query}",
                    "score": 0.9,
                    "published_date": "Tue, 11 Mar 2025 17:00:00 GMT",
                }
            ],
            "request_id": request_ids[query],
            "response_time": "0.5",
            "usage": {"credits": 2},
        }

    monkeypatch.setattr(retriever, "_search", fake_search)
    candidates, trace = retriever.retrieve(_claim())

    assert len(calls) == 4
    assert {item[0] for item in calls} == {
        "bounded claim evidence",
        "bounded claim contradiction",
        "bounded claim limitations",
        "bounded claim alternatives",
    }
    assert {item[1] for item in calls} == {"general"}
    assert len(candidates) == 1
    assert candidates[0]["publication_year"] == 2025
    assert candidates[0]["retrieval_purposes"] == [
        "supporting",
        "contradictory",
        "limitations",
        "alternatives",
    ]
    assert {item["backend_id"] for item in trace} == {"tavily"}
    assert [item["provider_request_id"] for item in trace] == [
        "request-supporting",
        "request-contradictory",
        "request-limitations",
        "request-alternatives",
    ]


def test_tavily_runs_independent_queries_concurrently_but_records_policy_order(monkeypatch):
    retriever = TavilyRetriever(api_key="secret")
    all_queries_started = threading.Barrier(4)

    def fake_search(query, *, topic):
        all_queries_started.wait(timeout=5)
        return {"results": [], "request_id": query}

    monkeypatch.setattr(retriever, "_search", fake_search)
    _, trace = retriever.retrieve(_claim())

    assert [item["purpose"] for item in trace] == [
        "supporting",
        "contradictory",
        "limitations",
        "alternatives",
    ]
    assert [item["provider_request_id"] for item in trace] == [
        "bounded claim evidence",
        "bounded claim contradiction",
        "bounded claim limitations",
        "bounded claim alternatives",
    ]


def test_tavily_uses_news_topic_for_current_facts(monkeypatch):
    retriever = TavilyRetriever(api_key="secret")
    topics = []

    def fake_search(query, *, topic):
        topics.append(topic)
        return {"results": []}

    monkeypatch.setattr(retriever, "_search", fake_search)
    retriever.retrieve(_claim("CURRENT_FACT"))
    assert topics == ["news"] * 4


class _FixtureRetriever:
    def __init__(self, backend_id, purposes):
        self.backend_ids = (backend_id,)
        self.purposes = purposes

    def retrieve(self, claim):
        candidate = {
            "source_id": "https://example.test/shared",
            "title": "Shared",
            "retrieval_purposes": list(self.purposes),
        }
        return [candidate], [
            {
                "backend_id": self.backend_ids[0],
                "purpose": purpose,
                "query": purpose,
                "returned_source_ids": [candidate["source_id"]],
            }
            for purpose in self.purposes
        ]


class _UnavailableRetriever:
    def __init__(self, backend_id):
        self.backend_ids = (backend_id,)

    def retrieve(self, claim):
        raise TransientProviderError(f"{self.backend_ids[0]} returned HTTP 429")


class _BarrierRetriever(_FixtureRetriever):
    def __init__(self, backend_id, barrier):
        super().__init__(backend_id, ["supporting"])
        self.barrier = barrier

    def retrieve(self, claim):
        self.barrier.wait(timeout=5)
        return super().retrieve(claim)


def test_composite_retriever_merges_duplicate_candidates_without_hiding_backends():
    retriever = CompositeRetriever(
        [
            _FixtureRetriever("openalex", ["supporting", "limitations"]),
            _FixtureRetriever("tavily", ["contradictory", "alternatives"]),
        ]
    )

    candidates, trace = retriever.retrieve(_claim())

    assert retriever.backend_ids == ("openalex", "tavily")
    assert len(candidates) == 1
    assert candidates[0]["retrieval_purposes"] == [
        "supporting",
        "limitations",
        "contradictory",
        "alternatives",
    ]
    assert {item["backend_id"] for item in trace} == {"openalex", "tavily"}


def test_composite_retriever_runs_backends_concurrently_but_merges_configured_order():
    both_backends_started = threading.Barrier(2)
    retriever = CompositeRetriever(
        [
            _BarrierRetriever("openalex", both_backends_started),
            _BarrierRetriever("tavily", both_backends_started),
        ]
    )

    candidates, trace = retriever.retrieve(_claim())

    assert len(candidates) == 1
    assert [item["backend_id"] for item in trace] == ["openalex", "tavily"]


def test_composite_retriever_quarantines_one_unavailable_backend():
    retriever = CompositeRetriever(
        [
            _UnavailableRetriever("openalex"),
            _FixtureRetriever("tavily", ["supporting", "contradictory", "limitations", "alternatives"]),
        ]
    )

    result = retriever.retrieve(_claim())
    candidates, trace = result

    assert len(candidates) == 1
    assert result.effective_backend_ids == ("tavily",)
    assert result.failed_backend_ids == ("openalex",)
    failure = next(item for item in trace if item.get("status") == "BACKEND_UNAVAILABLE")
    assert failure["backend_id"] == "openalex"
    assert "429" in failure["error_summary"]
    from project_ensemble.research.retrievers import openalex_unavailable_reason
    assert "429" in openalex_unavailable_reason(trace)


def test_policy_retriever_waits_for_daily_openalex_reset_without_tavily_fallback():
    from project_ensemble.errors import OpenAlexDailyQuotaExhausted
    from project_ensemble.research.retrievers import PolicyResearchRetriever

    class QuotaOpenAlex:
        backend_ids = ("openalex",)
        def retrieve(self, _claim):
            raise OpenAlexDailyQuotaExhausted("daily quota exhausted", reset_seconds=90)

    class CountingTavily:
        backend_ids = ("tavily",)
        calls = 0
        def retrieve(self, _claim):
            self.calls += 1
            return ResearchRetrievalResult([], [], ("tavily",))

    tavily = CountingTavily()
    retriever = PolicyResearchRetriever(QuotaOpenAlex(), tavily, quota_policy="wait")
    with pytest.raises(OpenAlexDailyQuotaExhausted) as caught:
        retriever.retrieve(_claim())
    assert caught.value.reset_seconds == 90
    assert tavily.calls == 0


def test_new_meeting_without_tavily_does_not_mistake_human_references_for_quota_fallback():
    from project_ensemble.errors import OpenAlexDailyQuotaExhausted

    class QuotaOpenAlex:
        backend_ids = ("openalex",)
        def retrieve(self, _claim):
            raise OpenAlexDailyQuotaExhausted("daily quota exhausted", reset_seconds=90)

    class HumanReferences:
        backend_ids = ("human_references",)
        def retrieve(self, _claim):
            return ResearchRetrievalResult([], [], self.backend_ids)

    retriever = CompositeRetriever(
        [QuotaOpenAlex(), HumanReferences()], preserve_openalex_quota=True,
    )
    with pytest.raises(OpenAlexDailyQuotaExhausted):
        retriever.retrieve(_claim())


def test_policy_retriever_uses_tavily_only_for_selected_quota_fallback():
    from project_ensemble.errors import OpenAlexDailyQuotaExhausted
    from project_ensemble.research.retrievers import PolicyResearchRetriever

    class QuotaOpenAlex:
        backend_ids = ("openalex",)
        def retrieve(self, _claim):
            raise OpenAlexDailyQuotaExhausted("daily quota exhausted", reset_seconds=90)

    class CountingTavily:
        backend_ids = ("tavily",)
        calls = 0
        def retrieve(self, _claim):
            self.calls += 1
            return ResearchRetrievalResult([], [], ("tavily",))

    tavily = CountingTavily()
    result = PolicyResearchRetriever(QuotaOpenAlex(), tavily, quota_policy="tavily").retrieve(_claim())
    assert tavily.calls == 1
    assert result.effective_backend_ids == ("tavily",)
    assert result.failed_backend_ids == ("openalex",)
    assert result.query_trace[0]["status"] == "BACKEND_UNAVAILABLE"


def test_human_authorized_daily_backup_skips_repeated_openalex_calls():
    from project_ensemble.research.retrievers import PolicyResearchRetriever

    class OpenAlex:
        backend_ids = ("openalex",)
        calls = 0

        def retrieve(self, _claim):
            self.calls += 1
            raise AssertionError("confirmed daily exhaustion should not call OpenAlex again")

    class Tavily:
        backend_ids = ("tavily",)
        calls = 0

        def retrieve(self, _claim):
            self.calls += 1
            return ResearchRetrievalResult([], [], ("tavily",))

    oa, tavily = OpenAlex(), Tavily()
    retriever = PolicyResearchRetriever(oa, tavily, quota_policy="tavily")
    retriever.suspend_openalex(120)
    result = retriever.retrieve(_claim())
    assert oa.calls == 0
    assert tavily.calls == 1
    assert result.failed_backend_ids == ("openalex",)


@pytest.mark.parametrize("policy,expected_tavily_calls", [("wait", 0), ("tavily", 1)])
def test_policy_retriever_handles_openalex_429_without_quota_headers(policy, expected_tavily_calls):
    from project_ensemble.errors import OpenAlexRateLimited
    from project_ensemble.research.retrievers import PolicyResearchRetriever

    class LimitedOpenAlex:
        backend_ids = ("openalex",)
        def retrieve(self, _claim):
            raise OpenAlexRateLimited("HTTP 429; rate-limit type unconfirmed",
                                      retry_after_seconds=2)

    class CountingTavily:
        backend_ids = ("tavily",)
        calls = 0
        def retrieve(self, _claim):
            self.calls += 1
            return ResearchRetrievalResult([], [], self.backend_ids)

    tavily = CountingTavily()
    retriever = PolicyResearchRetriever(LimitedOpenAlex(), tavily, quota_policy=policy)
    if policy == "wait":
        with pytest.raises(OpenAlexRateLimited):
            retriever.retrieve(_claim())
    else:
        result = retriever.retrieve(_claim())
        assert result.failed_backend_ids == ("openalex",)
        assert result.effective_backend_ids == ("tavily",)
    assert tavily.calls == expected_tavily_calls


def test_policy_retriever_uses_tavily_after_openalex_connection_retries(monkeypatch):
    from project_ensemble.errors import OpenAlexConnectionUnavailable
    from project_ensemble.research.retrievers import PolicyResearchRetriever

    class UnreachableOpenAlex:
        backend_ids = ("openalex",)
        calls = 0
        def retrieve(self, _claim):
            self.calls += 1
            raise OpenAlexConnectionUnavailable("OpenAlex retrieval failed: connection refused")

    class AvailableTavily:
        backend_ids = ("tavily",)
        calls = 0
        def retrieve(self, _claim):
            self.calls += 1
            return ResearchRetrievalResult([], [], ("tavily",))

    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    openalex, tavily = UnreachableOpenAlex(), AvailableTavily()
    result = PolicyResearchRetriever(openalex, tavily).retrieve(_claim())
    assert openalex.calls == 2
    assert tavily.calls == 1
    assert result.failed_backend_ids == ("openalex",)


def test_policy_retriever_routes_nonacademic_claims_to_web_search():
    from project_ensemble.research.retrievers import PolicyResearchRetriever

    class OpenAlex:
        backend_ids = ("openalex",)
        def retrieve(self, _claim):
            raise AssertionError("nonacademic claim should not use OpenAlex first")

    class Tavily:
        backend_ids = ("tavily",)
        def retrieve(self, _claim):
            return ResearchRetrievalResult([], [], ("tavily",))

    result = PolicyResearchRetriever(OpenAlex(), Tavily()).retrieve(
        _claim().model_copy(update={"source_domain": "GENERAL"})
    )
    assert result.effective_backend_ids == ("tavily",)


def test_composite_retriever_fails_only_when_every_backend_is_unavailable():
    retriever = CompositeRetriever(
        [_UnavailableRetriever("openalex"), _UnavailableRetriever("tavily")]
    )

    with pytest.raises(TransientProviderError, match="all configured"):
        retriever.retrieve(_claim())
