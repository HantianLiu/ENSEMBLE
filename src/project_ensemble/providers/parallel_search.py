"""Parallel v1 search transport. Search excerpts are discovery, not verified originals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
from urllib.parse import urlparse

import httpx

from project_ensemble.errors import PermanentProviderError, TransientProviderError
from project_ensemble.providers.http import checked_json
from project_ensemble.research.retrievers import ResearchRetrievalResult


class ParallelRetriever:
    backend_ids = ("parallel",)

    def __init__(self, *, api_key: str, base_url: str = "https://api.parallel.ai",
                 timeout_seconds: float = 30.0, mode: str = "fast",
                 max_results_per_query: int = 10, max_chars_total: int = 20000,
                 max_concurrent_requests: int = 4):
        if not api_key:
            raise ValueError("Parallel API key is required")
        if mode not in {"fast", "turbo"}:
            raise ValueError("Parallel search mode must be fast or turbo")
        if not 1 <= max_results_per_query <= 10:
            raise ValueError("Parallel search allows only 1–10 results per request")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.mode = mode
        self.max_results_per_query = max_results_per_query
        self.max_chars_total = max_chars_total
        self._request_gate = threading.BoundedSemaphore(max_concurrent_requests)

    def _search(self, query: str) -> dict:
        # max_results is nested in v1, not a top-level field. Passing the cap
        # to the server avoids paying for results later discarded by the client.
        payload = {
            "mode": self.mode,
            "objective": query,
            "search_queries": [query],
            "max_chars_total": self.max_chars_total,
            "advanced_settings": {"max_results": self.max_results_per_query},
        }
        try:
            with self._request_gate, httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}/v1/search",
                    headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientProviderError(f"Parallel retrieval failed: {exc}") from exc
        data = checked_json(response)
        if not isinstance(data.get("results"), list):
            raise PermanentProviderError("Parallel search returned no results array")
        return data

    def retrieve(self, claim) -> ResearchRetrievalResult:
        # Preserve the four independent scientific checks; do not turn a
        # provider-generated answer or a cheaper one-sided search into a verdict.
        return self._retrieve({
            "supporting": claim.supporting_query,
            "contradictory": claim.contradictory_query,
            "limitations": claim.limitations_query,
            "alternatives": claim.alternatives_query,
        })

    def retrieve_exploratory(self, query: str) -> ResearchRetrievalResult:
        return self._retrieve({"exploratory": query})

    def _retrieve(self, queries: dict[str, str]) -> ResearchRetrievalResult:
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            futures = {purpose: executor.submit(self._search, query)
                       for purpose, query in queries.items()}
        candidates = {}
        trace = []
        for purpose, query in queries.items():
            data = futures[purpose].result()
            returned = []
            for item in data["results"][:self.max_results_per_query]:
                candidate = self._candidate(item)
                if candidate is None:
                    continue
                source_id = candidate["source_id"]
                returned.append(source_id)
                existing = candidates.setdefault(source_id, {**candidate, "retrieval_purposes": []})
                if purpose not in existing["retrieval_purposes"]:
                    existing["retrieval_purposes"].append(purpose)
            trace.append({
                "backend_id": "parallel", "purpose": purpose, "query": query,
                "mode": self.mode, "max_results": self.max_results_per_query,
                "returned_source_ids": returned, "provider_request_id": data.get("search_id"),
                "provider_usage": data.get("usage"), "provider_warnings": data.get("warnings"),
            })
        return ResearchRetrievalResult(list(candidates.values()), trace, self.backend_ids)

    @staticmethod
    def _candidate(item) -> dict | None:
        if not isinstance(item, dict):
            return None
        url = str(item.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        published = str(item.get("publish_date") or "")
        year = int(published[:4]) if len(published) >= 4 and published[:4].isdigit() else None
        excerpts = item.get("excerpts") or []
        return {
            "source_id": url, "title": str(item.get("title") or parsed.hostname),
            "authors": [], "publication_year": year, "doi": None, "url": url,
            "venue": parsed.hostname, "source_type": "web_page", "is_primary_source": None,
            "abstract": "\n".join(text for text in excerpts if isinstance(text, str)) or None,
            "relevance_score": None, "published_date": item.get("publish_date"),
            "full_text_url": url, "full_text_is_public": True, "license": None,
            "retrieval_backend_id": "parallel",
        }
