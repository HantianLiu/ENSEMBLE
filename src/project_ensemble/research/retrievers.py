from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from project_ensemble.errors import TransientProviderError
from project_ensemble.errors import (
    OpenAlexConnectionUnavailable, OpenAlexDailyQuotaExhausted, OpenAlexRateLimited,
)
from project_ensemble.providers.http import checked_json
from project_ensemble.research.models import ClaimSourceDomain, NormalizedClaim


@dataclass(frozen=True)
class ResearchRetrievalResult:
    """One call's retrieval data, including call-local backend health.

    ``__iter__`` preserves the historical two-value unpacking interface while
    keeping success/failure metadata off shared retriever instances, which is
    required when independent claim groups run concurrently.
    """

    candidates: list[dict[str, Any]]
    query_trace: list[dict[str, Any]]
    effective_backend_ids: tuple[str, ...]
    failed_backend_ids: tuple[str, ...] = ()

    def __iter__(self):
        yield self.candidates
        yield self.query_trace


def openalex_unavailable_reason(query_trace: Sequence[dict[str, Any]]) -> str | None:
    """Return the call-local OpenAlex error saved in a retrieval audit trace."""
    for item in query_trace:
        if (item.get("backend_id") == "openalex"
                and item.get("status") == "BACKEND_UNAVAILABLE"):
            reason = str(item.get("error_summary") or item.get("error_type") or "unknown error")
            return reason.removeprefix("OpenAlex retrieval failed: ").strip()[:500]
    return None


def coerce_retrieval_result(
    value: ResearchRetrievalResult | tuple[list[dict[str, Any]], list[dict[str, Any]]],
    *,
    default_backend_ids: Sequence[str],
) -> ResearchRetrievalResult:
    """Accept legacy test/plugin retrievers without reintroducing shared state."""

    if isinstance(value, ResearchRetrievalResult):
        return value
    candidates, query_trace = value
    return ResearchRetrievalResult(
        candidates=candidates,
        query_trace=query_trace,
        effective_backend_ids=tuple(default_backend_ids),
    )


class ResearchRetriever(Protocol):
    backend_ids: tuple[str, ...]

    def retrieve(
        self, claim: NormalizedClaim
    ) -> ResearchRetrievalResult: ...

    def retrieve_exploratory(self, query: str) -> ResearchRetrievalResult: ...


class PolicyResearchRetriever:
    """Route academic search to OpenAlex; paid fallback needs authorization.

    General-web claims use the selected web engine, not as an OpenAlex
    substitute. The distinction preserves official-document discovery while
    preventing an OpenAlex quota event from silently downgrading scholarship.
    """

    backend_ids = ("openalex", "tavily")

    def __init__(self, openalex: ResearchRetriever, tavily: ResearchRetriever,
                 *, quota_policy: str = "wait"):
        if quota_policy not in {"wait", "tavily", "parallel"}:
            raise ValueError("unknown OpenAlex quota policy")
        self.openalex = openalex
        self.tavily = tavily  # Legacy attribute; the selected backend can be Parallel.
        self.general_backend_id = getattr(tavily, "backend_ids", ("tavily",))[0]
        self.backend_ids = ("openalex", self.general_backend_id)
        self.retrievers = (openalex, tavily)
        self.quota_policy = quota_policy
        self._openalex_suspended_until = 0.0

    def suspend_openalex(self, reset_seconds: float) -> None:
        """Use the Human-approved backup until the confirmed daily reset."""
        self._openalex_suspended_until = max(
            self._openalex_suspended_until,
            time.monotonic() + max(0.0, reset_seconds),
        )

    def openalex_suspended(self) -> bool:
        return time.monotonic() < self._openalex_suspended_until

    def retrieve(self, claim: NormalizedClaim) -> ResearchRetrievalResult:
        if claim.source_domain != ClaimSourceDomain.ACADEMIC:
            return self._with_fallback(self.tavily, self.openalex, claim)
        if self.quota_policy in {"tavily", "parallel"} and self.openalex_suspended():
            return self._fallback(
                self.tavily.retrieve(claim), "openalex",
                OpenAlexDailyQuotaExhausted(
                    "OpenAlex HTTP 429 daily quota previously confirmed; "
                    "Human authorized selected general search until reset",
                    reset_seconds=max(0.0, self._openalex_suspended_until - time.monotonic()),
                ),
            )
        return self._with_fallback(self.openalex, self.tavily, claim)

    def retrieve_exploratory(self, query: str, *, academic_only: bool = False) -> ResearchRetrievalResult:
        # Broad exploration needs both scholarly and web coverage when both
        # backends are healthy; quota-wait policy still forbids silently
        # publishing a Tavily-only response for an OpenAlex search.
        if self.quota_policy in {"tavily", "parallel"} and self.openalex_suspended():
            return self._fallback(
                self.tavily.retrieve_exploratory(query), "openalex",
                OpenAlexDailyQuotaExhausted(
                    "OpenAlex HTTP 429 daily quota previously confirmed; "
                    "Human authorized selected general search until reset",
                    reset_seconds=max(0.0, self._openalex_suspended_until - time.monotonic()),
                ),
            )
        try:
            academic = self._retry_openalex_connection(
                lambda: self.openalex.retrieve_exploratory(query)
            )
        except OpenAlexDailyQuotaExhausted as exc:
            if self.quota_policy == "wait":
                raise
            return self._fallback(self.tavily.retrieve_exploratory(query), "openalex", exc)
        except OpenAlexRateLimited as exc:
            if self.quota_policy == "wait":
                raise
            return self._fallback(self.tavily.retrieve_exploratory(query), "openalex", exc)
        except OpenAlexConnectionUnavailable as exc:
            if self.quota_policy == "wait":
                raise  # No automatic paid academic fallback without authorization.
            return self._fallback(self.tavily.retrieve_exploratory(query), "openalex", exc)
        if academic_only:
            return academic
        try:
            web = self.tavily.retrieve_exploratory(query)
        except TransientProviderError as exc:
            return self._fallback(academic, self.general_backend_id, exc)
        return self._merge(academic, web)

    def _with_fallback(self, primary: ResearchRetriever, fallback: ResearchRetriever,
                       claim: NormalizedClaim) -> ResearchRetrievalResult:
        try:
            if primary is self.openalex:
                return self._retry_openalex_connection(lambda: primary.retrieve(claim))
            return primary.retrieve(claim)
        except OpenAlexDailyQuotaExhausted as exc:
            if self.quota_policy == "wait":
                raise
            return self._fallback(fallback.retrieve(claim), "openalex", exc)
        except OpenAlexRateLimited as exc:
            if self.quota_policy == "wait":
                raise
            return self._fallback(fallback.retrieve(claim), "openalex", exc)
        except OpenAlexConnectionUnavailable as exc:
            if self.quota_policy == "wait":
                raise  # No automatic paid academic fallback without authorization.
            return self._fallback(fallback.retrieve(claim), "openalex", exc)
        except TransientProviderError as exc:
            if primary is self.openalex:
                raise  # A transient 429 is not a connectivity failure.
            return self._fallback(fallback.retrieve(claim), self.general_backend_id, exc)

    @staticmethod
    def _retry_openalex_connection(call):
        import time

        for attempt in range(2):
            try:
                return call()
            except OpenAlexConnectionUnavailable:
                if attempt:
                    raise
                time.sleep(1.0)
        raise AssertionError("unreachable")

    @staticmethod
    def _fallback(result: ResearchRetrievalResult, failed_backend: str,
                  error: Exception) -> ResearchRetrievalResult:
        summary = str(error).strip().replace("\n", " ")[:500]
        return ResearchRetrievalResult(
            candidates=result.candidates,
            query_trace=[{
                "backend_id": failed_backend,
                "status": "BACKEND_UNAVAILABLE",
                "error_type": type(error).__name__,
                "error_summary": summary,
                "returned_source_ids": [],
            }, *result.query_trace],
            effective_backend_ids=result.effective_backend_ids,
            failed_backend_ids=(failed_backend, *result.failed_backend_ids),
        )

    @staticmethod
    def _merge(first: ResearchRetrievalResult,
               second: ResearchRetrievalResult) -> ResearchRetrievalResult:
        candidates: dict[str, dict[str, Any]] = {}
        for item in [*first.candidates, *second.candidates]:
            source_id = str(item["source_id"])
            if source_id not in candidates:
                candidates[source_id] = item
            else:
                purposes = candidates[source_id].setdefault("retrieval_purposes", [])
                for purpose in item.get("retrieval_purposes", []):
                    if purpose not in purposes:
                        purposes.append(purpose)
        return ResearchRetrievalResult(
            candidates=list(candidates.values()),
            query_trace=[*first.query_trace, *second.query_trace],
            effective_backend_ids=tuple(dict.fromkeys(
                [*first.effective_backend_ids, *second.effective_backend_ids]
            )),
            failed_backend_ids=tuple(dict.fromkeys(
                [*first.failed_backend_ids, *second.failed_backend_ids]
            )),
        )


class CompositeRetriever:
    """Run all configured retrieval backends and merge candidates without hiding provenance."""

    def __init__(self, retrievers: Sequence[ResearchRetriever], *,
                 preserve_openalex_quota: bool = False):
        if not retrievers:
            raise ValueError("a composite retriever requires at least one backend")
        self.retrievers = tuple(retrievers)
        self.preserve_openalex_quota = preserve_openalex_quota
        self.backend_ids = tuple(
            backend_id
            for retriever in self.retrievers
            for backend_id in retriever.backend_ids
        )
        if len(self.backend_ids) != len(set(self.backend_ids)):
            raise ValueError("composite retrieval backend IDs must be unique")

    def retrieve(
        self, claim: NormalizedClaim
    ) -> ResearchRetrievalResult:
        candidates: dict[str, dict[str, Any]] = {}
        query_trace: list[dict[str, Any]] = []
        successful_backend_ids: list[str] = []
        failed_backend_ids: list[str] = []
        failure_summaries: list[str] = []
        # Backends have independent quotas and network paths. Execute them together,
        # then consume results in configured order so candidate/audit ordering remains stable.
        with ThreadPoolExecutor(max_workers=len(self.retrievers)) as executor:
            futures = [
                executor.submit(retriever.retrieve, claim)
                for retriever in self.retrievers
            ]
        for retriever, future in zip(self.retrievers, futures, strict=True):
            try:
                result = coerce_retrieval_result(
                    future.result(), default_backend_ids=retriever.backend_ids
                )
            except TransientProviderError as exc:
                if (isinstance(exc, OpenAlexDailyQuotaExhausted)
                        and (self.preserve_openalex_quota
                             or isinstance(retriever, PolicyResearchRetriever))):
                    raise
                summary = str(exc).strip().replace("\n", " ")[:500]
                failed_backend_ids.extend(retriever.backend_ids)
                failure_summaries.append(summary)
                query_trace.append(
                    {
                        "backend_id": "+".join(retriever.backend_ids),
                        "status": "BACKEND_UNAVAILABLE",
                        "error_type": type(exc).__name__,
                        "error_summary": summary,
                        "returned_source_ids": [],
                    }
                )
                continue
            successful_backend_ids.extend(result.effective_backend_ids)
            failed_backend_ids.extend(result.failed_backend_ids)
            query_trace.extend(result.query_trace)
            for candidate in result.candidates:
                source_id = str(candidate["source_id"])
                existing = candidates.get(source_id)
                if existing is None:
                    candidates[source_id] = candidate
                    continue
                purposes = existing.setdefault("retrieval_purposes", [])
                for purpose in candidate.get("retrieval_purposes", []):
                    if purpose not in purposes:
                        purposes.append(purpose)
        if not successful_backend_ids:
            raise TransientProviderError(
                "all configured research retrieval backends were unavailable: "
                + "; ".join(failure_summaries)
            )
        return ResearchRetrievalResult(
            candidates=list(candidates.values()),
            query_trace=query_trace,
            effective_backend_ids=tuple(dict.fromkeys(successful_backend_ids)),
            failed_backend_ids=tuple(dict.fromkeys(failed_backend_ids)),
        )

    def retrieve_exploratory(self, query: str, *, academic_only: bool = False) -> ResearchRetrievalResult:
        """Run one broad query without the four-claim adversarial-query contract."""
        candidates: dict[str, dict[str, Any]] = {}
        trace: list[dict[str, Any]] = []
        successful: list[str] = []
        failed: list[str] = []
        retrievers = tuple(
            retriever for retriever in self.retrievers
            if not (academic_only and set(getattr(retriever, "backend_ids", ())) <= {"tavily", "parallel"})
        )
        if not retrievers:
            raise TransientProviderError("academic exploratory search has no scholarly backend")
        with ThreadPoolExecutor(max_workers=len(retrievers)) as executor:
            futures = [
                executor.submit(
                    retriever.retrieve_exploratory, query, academic_only=True,
                ) if academic_only and isinstance(retriever, PolicyResearchRetriever)
                else executor.submit(retriever.retrieve_exploratory, query)
                for retriever in retrievers
            ]
        for retriever, future in zip(retrievers, futures, strict=True):
            try:
                result = coerce_retrieval_result(
                    future.result(), default_backend_ids=retriever.backend_ids
                )
            except (TransientProviderError, AttributeError) as exc:
                if (isinstance(exc, OpenAlexDailyQuotaExhausted)
                        and (self.preserve_openalex_quota
                             or isinstance(retriever, PolicyResearchRetriever))):
                    raise
                failed.extend(retriever.backend_ids)
                trace.append({
                    "backend_id": "+".join(retriever.backend_ids),
                    "purpose": "exploratory",
                    "query": query,
                    "status": "BACKEND_UNAVAILABLE",
                    "error_type": type(exc).__name__,
                    "returned_source_ids": [],
                })
                continue
            successful.extend(result.effective_backend_ids)
            failed.extend(result.failed_backend_ids)
            trace.extend(result.query_trace)
            for candidate in result.candidates:
                source_id = str(candidate["source_id"])
                candidates.setdefault(source_id, candidate)
        if not successful:
            raise TransientProviderError("all exploratory retrieval backends were unavailable")
        return ResearchRetrievalResult(
            candidates=list(candidates.values()),
            query_trace=trace,
            effective_backend_ids=tuple(dict.fromkeys(successful)),
            failed_backend_ids=tuple(dict.fromkeys(failed)),
        )


class TavilyRetriever:
    """General-Web discovery backend; returned URLs remain evidence candidates, not verdicts."""

    backend_ids = ("tavily",)

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        timeout_seconds: float = 30.0,
        search_depth: str = "basic",
        extract_enabled: bool = False,
        max_results_per_query: int = 8,
        chunks_per_source: int = 3,
        max_concurrent_requests: int = 4,
    ):
        if not api_key:
            raise ValueError("Tavily API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.search_depth = search_depth
        self.extract_enabled = extract_enabled
        self.max_results_per_query = max_results_per_query
        self.chunks_per_source = chunks_per_source
        self._request_gate = threading.BoundedSemaphore(max_concurrent_requests)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def retrieve(
        self, claim: NormalizedClaim
    ) -> ResearchRetrievalResult:
        query_map = {
            "supporting": claim.supporting_query,
            "contradictory": claim.contradictory_query,
            "limitations": claim.limitations_query,
            "alternatives": claim.alternatives_query,
        }
        candidates: dict[str, dict[str, Any]] = {}
        query_trace: list[dict[str, Any]] = []
        topic = "news" if claim.source_domain == ClaimSourceDomain.CURRENT_FACT else "general"
        # Tavily queries are mutually independent and the API supports concurrent
        # searches. Results are consumed in policy order for deterministic recovery.
        with ThreadPoolExecutor(max_workers=len(query_map)) as executor:
            futures = {
                purpose: executor.submit(self._bounded_search, query, topic=topic)
                for purpose, query in query_map.items()
            }
        for purpose, query in query_map.items():
            data = futures[purpose].result()
            returned_ids: list[str] = []
            for item in data.get("results", []):
                if not isinstance(item, dict):
                    continue
                candidate = self._candidate(item)
                if candidate is None:
                    continue
                source_id = candidate["source_id"]
                returned_ids.append(source_id)
                existing = candidates.get(source_id)
                if existing is None:
                    candidate["retrieval_purposes"] = [purpose]
                    candidates[source_id] = candidate
                elif purpose not in existing["retrieval_purposes"]:
                    existing["retrieval_purposes"].append(purpose)
            query_trace.append(
                {
                    "backend_id": "tavily",
                    "purpose": purpose,
                    "search_depth": self.search_depth,
                    "auto_parameters": False,
                    "query": query,
                    "returned_source_ids": returned_ids,
                    "provider_request_id": data.get("request_id"),
                    "provider_response_time_seconds": data.get("response_time"),
                    "provider_usage": data.get("usage"),
                }
            )
        return ResearchRetrievalResult(
            candidates=list(candidates.values()),
            query_trace=query_trace,
            effective_backend_ids=self.backend_ids,
        )

    def retrieve_exploratory(self, query: str) -> ResearchRetrievalResult:
        data = self._bounded_search(query, topic="general")
        candidates: dict[str, dict[str, Any]] = {}
        for item in data.get("results", []):
            if not isinstance(item, dict):
                continue
            candidate = self._candidate(item)
            if candidate is not None:
                candidate["retrieval_purposes"] = ["exploratory"]
                candidates[candidate["source_id"]] = candidate
        return ResearchRetrievalResult(
            candidates=list(candidates.values()),
            query_trace=[{
                "backend_id": "tavily",
                "purpose": "exploratory",
                "search_depth": self.search_depth,
                "auto_parameters": False,
                "query": query,
                "returned_source_ids": list(candidates),
                "provider_usage": data.get("usage"),
            }],
            effective_backend_ids=self.backend_ids,
        )

    def _bounded_search(self, query: str, *, topic: str) -> dict[str, Any]:
        with self._request_gate:
            return self._search(query, topic=topic)

    def extract_url(self, url: str) -> dict[str, Any]:
        """Read one selected page; search snippets are not treated as its body."""
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Tavily extraction requires an absolute HTTP(S) URL")
        with self._request_gate:
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(
                        f"{self.base_url}/extract",
                        headers=self.headers,
                        json={
                            "urls": url,
                            "extract_depth": "advanced",
                            "format": "markdown",
                            "include_images": False,
                            "include_usage": True,
                        },
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise TransientProviderError(f"Tavily extraction failed: {exc}") from exc
        data = checked_json(response)
        # HTTP 200 can still mean this URL failed. Never substitute a search
        # snippet, a different URL, or the provider's answer for page content.
        for item in data.get("results", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("url", "")).rstrip("/") == url.rstrip("/"):
                raw = item.get("raw_content")
                if isinstance(raw, str) and raw.strip():
                    return {
                        "url": url,
                        "raw_content": raw,
                        "request_id": data.get("request_id"),
                        "usage": data.get("usage"),
                    }
        raise ValueError("Tavily Extract returned no readable content for the selected URL")

    def _search(self, query: str, *, topic: str) -> dict[str, Any]:
        payload = {
            "query": query,
            "search_depth": self.search_depth,
            "chunks_per_source": self.chunks_per_source,
            "max_results": self.max_results_per_query,
            "topic": topic,
            "include_published_date": True,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
            "include_usage": True,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}/search",
                    headers=self.headers,
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientProviderError(f"Tavily retrieval failed: {exc}") from exc
        return checked_json(response)

    @staticmethod
    def _candidate(item: dict[str, Any]) -> dict[str, Any] | None:
        url = str(item.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        published = str(item.get("published_date") or "")
        year = None
        for token in published.replace(",", " ").split():
            if len(token) == 4 and token.isdigit():
                numeric = int(token)
                if 1000 <= numeric <= 9999:
                    year = numeric
                    break
        return {
            "source_id": url,
            "title": str(item.get("title") or parsed.hostname),
            "authors": [],
            "publication_year": year,
            "doi": None,
            "url": url,
            "venue": parsed.hostname,
            "source_type": "web_page",
            "is_primary_source": None,
            "abstract": str(item.get("content") or "") or None,
            "relevance_score": item.get("score"),
            "published_date": item.get("published_date"),
            "full_text_url": url,
            "full_text_is_public": True,
            "license": None,
            "retrieval_backend_id": "tavily",
        }
