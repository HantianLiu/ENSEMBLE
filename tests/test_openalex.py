import httpx
import pytest
import time
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

from project_ensemble.errors import OpenAlexDailyQuotaExhausted, OpenAlexRateLimited, TransientProviderError
from project_ensemble.errors import OpenAlexQueryRejected
from project_ensemble.research.models import NormalizedClaim
from project_ensemble.research.openalex import OpenAlexRetriever


def _claim():
    return NormalizedClaim(
        is_researchable=True,
        normalized_claim="A bounded claim.",
        verification_question="Is the bounded claim supported?",
        supporting_query="bounded evidence",
        contradictory_query="bounded contradiction",
        limitations_query="bounded limitations",
        alternatives_query="bounded alternatives",
        scope_terms=["bounded"],
        source_domain="ACADEMIC",
        source_domain_rationale="Scholarly evidence.",
        freshness_class="STABLE",
        freshness_rationale="Stable literature.",
    )


def test_openalex_uses_bearer_key_selects_fields_and_records_budget(monkeypatch):
    observed = []

    def fake_get(_client, url, *, params, headers):
        observed.append((url, params, headers))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            headers={
                "X-RateLimit-Limit": "10000",
                "X-RateLimit-Remaining": "9980",
                "X-RateLimit-Credits-Used": "10",
                "X-RateLimit-Reset": "3600",
            },
            json={"results": []},
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    retriever = OpenAlexRetriever(api_key="private-key", max_results_per_query=8)

    _candidates, trace = retriever.retrieve(_claim())

    assert len(observed) == 4
    assert all(call[2] == {"Authorization": "Bearer private-key"} for call in observed)
    assert all("api_key" not in call[1] for call in observed)
    assert all(call[1]["per_page"] == 8 for call in observed)
    assert all("select" in call[1] for call in observed)
    assert all(call[1]["search"] and "search.exact" not in call[1] for call in observed)
    assert trace[0]["provider_usage"] == {
        "daily_credit_limit": 10000,
        "daily_credits_remaining": 9980,
        "request_credits_used": 10,
        "reset_seconds": 3600,
    }


def test_openalex_wildcard_uses_exact_search_without_flattening_boolean_query(monkeypatch):
    observed = []

    def fake_get(_client, url, *, params, headers):
        observed.append(dict(params))
        return httpx.Response(200, request=httpx.Request("GET", url), json={"results": []})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    retriever = OpenAlexRetriever()
    wildcard_query = '("cumulant expansion" OR fail*) AND "free energy"'
    claim = _claim().model_copy(update={"contradictory_query": wildcard_query})
    result = retriever.retrieve(claim)

    assert observed[0]["search"] == "bounded evidence"
    assert observed[1]["search.exact"] == wildcard_query
    assert "search" not in observed[1]
    assert result.query_trace[1]["query"] == wildcard_query
    assert result.query_trace[1]["search_parameter"] == "search.exact"
    assert result.query_trace[1]["query_repair_reason"] is None

    retriever.retrieve_exploratory("wom?n AND health")
    assert observed[-1]["search.exact"] == "wom?n AND health"


def test_openalex_interrupt_stops_remaining_queries_after_current_request(monkeypatch):
    cancelled = threading.Event()
    retriever = OpenAlexRetriever()
    retriever.set_cancellation_event(cancelled)
    queries = []

    def search(query):
        queries.append(query)
        cancelled.set()
        return [], {}

    monkeypatch.setattr(retriever, "_search", search)
    with pytest.raises(CancelledError):
        retriever.retrieve(_claim())
    assert queries == ["bounded evidence"]


def test_openalex_interrupt_while_waiting_for_global_gate(monkeypatch):
    cancelled = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    first = OpenAlexRetriever()
    second = OpenAlexRetriever()
    second.set_cancellation_event(cancelled)

    def blocking_search(_query):
        entered.set()
        assert release.wait(timeout=3)
        return [], {}

    monkeypatch.setattr(first, "_search", blocking_search)
    monkeypatch.setattr(second, "_search", lambda _query: pytest.fail("cancelled query started"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(first.retrieve_exploratory, "first")
        assert entered.wait(timeout=1)
        queued = pool.submit(second.retrieve_exploratory, "second")
        cancelled.set()
        with pytest.raises(CancelledError):
            queued.result(timeout=1)
        release.set()
        active.result(timeout=1)


def test_openalex_http_400_retries_simpler_query_and_records_both(monkeypatch):
    queries = []
    retriever = OpenAlexRetriever(api_key="secret-key")

    def fake_search(query):
        queries.append(query)
        if " AND " in query:
            raise OpenAlexQueryRejected("OpenAlex search HTTP 400; server detail: invalid query")
        return [], {}

    monkeypatch.setattr(retriever, "_search", fake_search)
    result = retriever.retrieve(_claim())
    assert result.query_trace[0]["query"] == "bounded evidence"
    assert result.query_trace[0]["original_query"] is None

    queries.clear()
    claim = _claim().model_copy(update={"supporting_query": '("cumulant expansion" OR "free energy") AND convergence'})
    result = retriever.retrieve(claim)
    assert queries[0] == claim.supporting_query
    assert queries[1] == "cumulant expansion free energy convergence"
    assert result.query_trace[0]["original_query"] == claim.supporting_query
    assert result.query_trace[0]["query_repair_reason"].startswith("OpenAlex search HTTP 400")


def test_openalex_http_400_reports_safe_server_detail(monkeypatch):
    def fake_get(_client, url, *, params, headers):
        return httpx.Response(400, request=httpx.Request("GET", url),
                              text="invalid expression; secret-key")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(OpenAlexQueryRejected) as caught:
        OpenAlexRetriever(api_key="secret-key")._search("bad query")
    assert "invalid expression" in str(caught.value)
    assert "secret-key" not in str(caught.value)


def test_openalex_429_reports_budget_without_exposing_key(monkeypatch):
    def fake_get(_client, url, *, params, headers):
        return httpx.Response(
            429,
            request=httpx.Request("GET", url),
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "120",
                "Retry-After": "30",
            },
            json={"error": "Rate limit exceeded"},
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    retriever = OpenAlexRetriever(api_key="must-not-appear")

    with pytest.raises(OpenAlexDailyQuotaExhausted) as caught:
        retriever._search("bounded query")

    assert caught.value.reset_seconds == 120
    assert "daily_remaining=0" in str(caught.value)
    assert "reset_seconds=120" in str(caught.value)
    assert "must-not-appear" not in str(caught.value)


def test_openalex_search_waits_when_remaining_daily_credits_cannot_pay_for_search(monkeypatch):
    def fake_get(_client, url, *, params, headers):
        return httpx.Response(
            429, request=httpx.Request("GET", url),
            headers={"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "45"},
            json={"error": "daily budget exceeded"},
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(OpenAlexDailyQuotaExhausted) as caught:
        OpenAlexRetriever()._search("query")
    assert caught.value.reset_seconds == 45


def test_openalex_429_without_limit_headers_is_not_misclassified_as_daily_quota(monkeypatch):
    def fake_get(_client, url, *, params, headers):
        return httpx.Response(
            429, request=httpx.Request("GET", url),
            json={"error": "rate limited"},
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(OpenAlexRateLimited) as caught:
        OpenAlexRetriever()._search("query")
    assert caught.value.retry_after_seconds == 2.0
    assert "daily_remaining=unreported" in str(caught.value)


def test_openalex_429_probes_official_balance_before_classifying_limit(monkeypatch):
    requested = []

    def fake_get(_client, url, *, headers, params=None):
        requested.append(url)
        if url.endswith("/rate-limit"):
            return httpx.Response(200, request=httpx.Request("GET", url), json={
                "rate_limit": {
                    "credits_limit": 10000, "credits_remaining": 8760,
                    "resets_in_seconds": 80000,
                    "credit_costs": {"search": 10},
                },
            })
        return httpx.Response(429, request=httpx.Request("GET", url),
                              json={"error": "rate limited"})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    retriever = OpenAlexRetriever(api_key="must-not-appear")
    with pytest.raises(OpenAlexRateLimited) as caught:
        retriever._search("query")
    with pytest.raises(OpenAlexRateLimited):
        retriever._search("query again")
    assert requested == [
        "https://api.openalex.org/works", "https://api.openalex.org/rate-limit",
        "https://api.openalex.org/works",
    ]
    assert "daily_remaining=8760" in str(caught.value)
    assert "rate_limit_kind=NOT_DAILY" in str(caught.value)
    assert "must-not-appear" not in str(caught.value)


def test_openalex_429_probe_confirms_real_daily_exhaustion(monkeypatch):
    def fake_get(_client, url, *, headers, params=None):
        if url.endswith("/rate-limit"):
            return httpx.Response(200, request=httpx.Request("GET", url), json={
                "rate_limit": {
                    "credits_limit": 10000, "credits_remaining": 5,
                    "resets_in_seconds": 120,
                    "credit_costs": {"search": 10},
                },
            })
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(OpenAlexDailyQuotaExhausted) as caught:
        OpenAlexRetriever()._search("query")
    assert caught.value.reset_seconds == 120
    assert "rate_limit_kind=DAILY" in str(caught.value)


def test_openalex_spaces_request_starts_even_with_multiple_query_purposes(monkeypatch):
    started_at = []

    def fake_get(_client, url, *, params, headers):
        started_at.append(time.monotonic())
        return httpx.Response(200, request=httpx.Request("GET", url), json={"results": []})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    retriever = OpenAlexRetriever(
        max_concurrent_requests=2,
        min_request_interval_seconds=0.05,
    )

    retriever.retrieve(_claim())

    assert len(started_at) == 4
    assert all(later - earlier >= 0.04 for earlier, later in zip(started_at, started_at[1:]))


def test_openalex_http_gate_is_shared_across_retrievers_after_route_refresh(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    calls = 0

    def fake_get(_client, url, *, params, headers):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            assert release_first.wait(timeout=3)
        else:
            second_entered.set()
        return httpx.Response(200, request=httpx.Request("GET", url), json={"results": []})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    first = OpenAlexRetriever(max_concurrent_requests=4)
    second = OpenAlexRetriever(max_concurrent_requests=4)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.retrieve_exploratory, "query one")
        assert first_entered.wait(timeout=3)
        b = pool.submit(second.retrieve_exploratory, "query two")
        assert not second_entered.wait(timeout=0.05)
        release_first.set()
        a.result(timeout=3)
        b.result(timeout=3)
    assert second_entered.is_set()
