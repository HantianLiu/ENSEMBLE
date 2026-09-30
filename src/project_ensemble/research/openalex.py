from __future__ import annotations

import threading
import time
import re
from concurrent.futures import CancelledError
from contextlib import contextmanager
from typing import Any

import httpx

from project_ensemble.errors import (
    OpenAlexConnectionUnavailable, OpenAlexDailyQuotaExhausted, OpenAlexQueryRejected,
    OpenAlexRateLimited,
    TransientProviderError,
)
from project_ensemble.research.models import NormalizedClaim
from project_ensemble.research.retrievers import ResearchRetrievalResult


_OPENALEX_HTTP_GATE = threading.Lock()


class OpenAlexRetriever:
    """Metadata/abstract retriever. It does not interpret or decide claims."""

    backend_ids = ("openalex",)
    search_cost_credits = 10

    def __init__(
        self,
        *,
        base_url: str = "https://api.openalex.org",
        contact_email: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        max_results_per_query: int = 12,
        max_concurrent_requests: int = 1,
        min_request_interval_seconds: float = 0.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.contact_email = contact_email
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_results_per_query = max_results_per_query
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be positive")
        # A Ctrl+R route change may briefly leave old and new retriever objects
        # alive together. Serializing each object separately would let their
        # HTTP calls overlap, so the API gate is process-wide.
        self._request_gate = _OPENALEX_HTTP_GATE
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must be nonnegative")
        self.min_request_interval_seconds = min_request_interval_seconds
        self._pacing_lock = threading.Lock()
        self._next_request_at = 0.0
        self._quota_probe_lock = threading.Lock()
        self._quota_probe_at = 0.0
        self._quota_probe_cache: dict[str, int | None] | None = None
        self._cancellation_event: threading.Event | None = None

    def set_cancellation_event(self, event: threading.Event | None) -> None:
        """Stop starting new searches after a batch receives Ctrl+C."""
        self._cancellation_event = event

    def _check_cancelled(self) -> None:
        if self._cancellation_event is not None and self._cancellation_event.is_set():
            raise CancelledError("OpenAlex batch cancelled before next HTTP request")

    @contextmanager
    def _request_slot(self):
        # The gate is process-wide. Waiting on another request must also be
        # interruptible; otherwise a queued query can delay shutdown by the
        # full length of every preceding query.
        self._check_cancelled()
        while not self._request_gate.acquire(timeout=0.2):
            self._check_cancelled()
        try:
            self._check_cancelled()
            yield
        finally:
            self._request_gate.release()

    @staticmethod
    def _simpler_search_query(query: str) -> str:
        """Drop Boolean syntax after a rejected query, keeping topical terms."""
        words = [word for word in re.findall(r"[\w-]+", query, re.UNICODE)
                 if word.upper() not in {"AND", "OR", "NOT"}]
        return " ".join(words[:12])

    @staticmethod
    def _search_parameter(query: str) -> str:
        """OpenAlex rejects wildcard terms on its default stemmed search."""
        return "search.exact" if re.search(r"\b[\w-]+[*?]", query) else "search"

    def _search_with_query_repair(self, query: str):
        try:
            return self._search(query), query, None
        except OpenAlexQueryRejected as first_error:
            simpler = self._simpler_search_query(query)
            if not simpler or simpler == query:
                raise
            self._check_cancelled()
            try:
                return self._search(simpler), simpler, str(first_error)
            except OpenAlexQueryRejected as second_error:
                raise OpenAlexQueryRejected(
                    f"OpenAlex rejected both original and simplified searches: "
                    f"original={first_error}; simplified={second_error}"
                ) from second_error

    def retrieve(self, claim: NormalizedClaim) -> ResearchRetrievalResult:
        query_map = {
            "supporting": claim.supporting_query,
            "contradictory": claim.contradictory_query,
            "limitations": claim.limitations_query,
            "alternatives": claim.alternatives_query,
        }
        candidates: dict[str, dict[str, Any]] = {}
        query_trace: list[dict[str, Any]] = []
        for purpose, query in query_map.items():
            with self._request_slot():
                (works, provider_usage), actual_query, repair_reason = (
                    self._search_with_query_repair(query)
                )
            query_trace.append(
                {
                    "backend_id": "openalex",
                    "purpose": purpose,
                    "query": actual_query,
                    "search_parameter": self._search_parameter(actual_query),
                    "original_query": query if repair_reason else None,
                    "query_repair_reason": repair_reason,
                    "returned_source_ids": [x["source_id"] for x in works],
                    "provider_usage": provider_usage,
                }
            )
            for work in works:
                existing = candidates.get(work["source_id"])
                if existing is None:
                    work["retrieval_purposes"] = [purpose]
                    candidates[work["source_id"]] = work
                elif purpose not in existing["retrieval_purposes"]:
                    existing["retrieval_purposes"].append(purpose)
        return ResearchRetrievalResult(
            candidates=list(candidates.values()),
            query_trace=query_trace,
            effective_backend_ids=self.backend_ids,
        )

    def retrieve_exploratory(self, query: str) -> ResearchRetrievalResult:
        with self._request_slot():
            (works, provider_usage), actual_query, repair_reason = (
                self._search_with_query_repair(query)
            )
        for work in works:
            work["retrieval_purposes"] = ["exploratory"]
        return ResearchRetrievalResult(
            candidates=works,
            query_trace=[{
                "backend_id": "openalex",
                "purpose": "exploratory",
                "query": actual_query,
                "search_parameter": self._search_parameter(actual_query),
                "original_query": query if repair_reason else None,
                "query_repair_reason": repair_reason,
                "returned_source_ids": [work["source_id"] for work in works],
                "provider_usage": provider_usage,
            }],
            effective_backend_ids=self.backend_ids,
        )

    @property
    def headers(self) -> dict[str, str]:
        return (
            {"Authorization": f"Bearer {self.api_key}"}
            if self.api_key
            else {}
        )

    def _search(self, query: str) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
        params: dict[str, str | int] = {
            self._search_parameter(query): query,
            "per_page": self.max_results_per_query,
            "select": (
                "id,display_name,authorships,publication_year,doi,primary_location,"
                "open_access,best_oa_location,type,abstract_inverted_index,"
                "cited_by_count,is_retracted"
            ),
        }
        if self.contact_email:
            params["mailto"] = self.contact_email
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                self._pace_request()
                response = client.get(
                    f"{self.base_url}/works",
                    params=params,
                    headers=self.headers,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            retry_after = None
            retry_after_header = exc.response.headers.get("Retry-After")
            if retry_after_header:
                try:
                    retry_after = max(0.0, float(retry_after_header))
                except ValueError:
                    retry_after = None
            usage = self._rate_limit_usage(exc.response)
            detail = ""
            if exc.response.status_code == 429:
                # OpenAlex sometimes omits rate-limit headers on a 429. Ask
                # its authenticated, read-only quota endpoint before calling
                # the event a daily-budget exhaustion.
                probe = self._probe_rate_limit()
                if probe is not None:
                    usage["daily_credit_limit"] = probe["daily_credit_limit"]
                    usage["daily_credits_remaining"] = probe["daily_credits_remaining"]
                    usage["reset_seconds"] = probe["reset_seconds"]
                remaining = usage["daily_credits_remaining"]
                search_cost = (probe.get("search_cost_credits") if probe is not None
                               else None) or self.search_cost_credits
                daily_exhausted = (
                    remaining is not None and remaining < search_cost
                    and usage["reset_seconds"] is not None
                )
                detail = (
                    "; daily_remaining="
                    + self._render_usage_value(remaining)
                    + "; daily_limit="
                    + self._render_usage_value(usage["daily_credit_limit"])
                    + "; reset_seconds="
                    + self._render_usage_value(usage["reset_seconds"])
                    + "; rate_limit_kind="
                    + ("DAILY" if daily_exhausted else
                       "NOT_DAILY" if remaining is not None else "UNKNOWN")
                )
                if daily_exhausted:
                    raise OpenAlexDailyQuotaExhausted(
                        f"OpenAlex retrieval failed: HTTP 429{detail}",
                        reset_seconds=float(usage["reset_seconds"]),
                    ) from exc
                raise OpenAlexRateLimited(
                    f"OpenAlex retrieval failed: HTTP 429{detail}; "
                    "rate-limit type unconfirmed",
                    retry_after_seconds=retry_after if retry_after is not None else 2.0,
                ) from exc
            if exc.response.status_code in {502, 503, 504}:
                raise OpenAlexConnectionUnavailable(
                    f"OpenAlex retrieval failed: HTTP {exc.response.status_code}"
                ) from exc
            if exc.response.status_code == 400:
                body = exc.response.text
                if self.api_key:
                    body = body.replace(self.api_key, "[REDACTED]")
                body = " ".join(body.split())[:350]
                raise OpenAlexQueryRejected(
                    f"OpenAlex search HTTP 400; server detail: {body or 'not provided'}"
                ) from exc
            raise TransientProviderError(
                f"OpenAlex retrieval failed: HTTP {exc.response.status_code}{detail}",
                retry_after_seconds=retry_after,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise OpenAlexConnectionUnavailable(f"OpenAlex retrieval failed: {exc}") from exc
        results = data.get("results", []) if isinstance(data, dict) else []
        works = [
            self._candidate(item)
            for item in results
            if isinstance(item, dict) and item.get("id")
        ]
        return works, self._rate_limit_usage(response)

    def _probe_rate_limit(self) -> dict[str, int | None] | None:
        """Read the authenticated official balance after 429; never infer it."""
        with self._quota_probe_lock:
            now = time.monotonic()
            if now - self._quota_probe_at < 10.0:
                return self._quota_probe_cache
            self._quota_probe_at = now
            self._quota_probe_cache = self._fetch_rate_limit_status()
            return self._quota_probe_cache

    def _fetch_rate_limit_status(self) -> dict[str, int | None] | None:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(f"{self.base_url}/rate-limit", headers=self.headers)
                response.raise_for_status()
                payload = response.json()
            quota = payload.get("rate_limit") if isinstance(payload, dict) else None
            if not isinstance(quota, dict):
                return None
            costs = quota.get("credit_costs")
            search_cost = costs.get("search") if isinstance(costs, dict) else None
            remaining = quota.get("credits_remaining")
            limit = quota.get("credits_limit")
            reset = quota.get("resets_in_seconds")
            if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
                return None
            return {
                "daily_credit_limit": int(limit) if isinstance(limit, (int, float)) else None,
                "daily_credits_remaining": int(remaining),
                "reset_seconds": int(reset) if isinstance(reset, (int, float)) else None,
                "search_cost_credits": (
                    int(search_cost) if isinstance(search_cost, (int, float))
                    and not isinstance(search_cost, bool) else None
                ),
            }
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    def _pace_request(self) -> None:
        """Space request starts across concurrent claim workers sharing this retriever."""
        if not self.min_request_interval_seconds:
            return
        with self._pacing_lock:
            now = time.monotonic()
            if now < self._next_request_at:
                time.sleep(self._next_request_at - now)
                now = time.monotonic()
            self._next_request_at = now + self.min_request_interval_seconds

    @staticmethod
    def _rate_limit_usage(response: httpx.Response) -> dict[str, int | None]:
        def integer_header(name: str) -> int | None:
            value = response.headers.get(name)
            if value is None:
                return None
            try:
                return int(float(value))
            except ValueError:
                return None

        return {
            "daily_credit_limit": integer_header("X-RateLimit-Limit"),
            "daily_credits_remaining": integer_header("X-RateLimit-Remaining"),
            "request_credits_used": integer_header("X-RateLimit-Credits-Used"),
            "reset_seconds": integer_header("X-RateLimit-Reset"),
        }

    @staticmethod
    def _render_usage_value(value: int | None) -> str:
        return "unreported" if value is None else str(value)

    @staticmethod
    def _candidate(item: dict[str, Any]) -> dict[str, Any]:
        authorships = item.get("authorships") or []
        authors = [
            str(entry.get("author", {}).get("display_name"))
            for entry in authorships
            if isinstance(entry, dict) and entry.get("author", {}).get("display_name")
        ]
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        open_access = item.get("open_access") or {}
        best_open_access = item.get("best_oa_location") or {}
        full_text_url = best_open_access.get("pdf_url")
        if not full_text_url and primary.get("is_oa"):
            full_text_url = primary.get("pdf_url")
        return {
            "source_id": str(item["id"]),
            "title": str(item.get("display_name") or item.get("title") or "Untitled work"),
            "authors": authors,
            "publication_year": item.get("publication_year"),
            "doi": item.get("doi"),
            "url": str(primary.get("landing_page_url") or item.get("doi") or item["id"]),
            "venue": source.get("display_name"),
            "source_type": str(item.get("type") or "scholarly_work"),
            "is_primary_source": OpenAlexRetriever._is_primary(item),
            "abstract": OpenAlexRetriever._reconstruct_abstract(item.get("abstract_inverted_index")),
            "cited_by_count": item.get("cited_by_count"),
            "is_retracted": bool(item.get("is_retracted", False)),
            "full_text_url": full_text_url,
            "full_text_is_public": bool(open_access.get("is_oa") and full_text_url),
            "license": best_open_access.get("license") or primary.get("license"),
        }

    @staticmethod
    def _is_primary(item: dict[str, Any]) -> bool | None:
        work_type = str(item.get("type") or "").lower()
        if work_type in {"review", "editorial", "letter"}:
            return False
        if work_type in {"article", "preprint", "dataset", "dissertation"}:
            return True
        return None

    @staticmethod
    def _reconstruct_abstract(index: object) -> str | None:
        if not isinstance(index, dict):
            return None
        positions: dict[int, str] = {}
        for word, indexes in index.items():
            if not isinstance(indexes, list):
                continue
            for position in indexes:
                if isinstance(position, int):
                    positions[position] = str(word)
        return " ".join(positions[i] for i in sorted(positions)) or None
