"""Read-only API diagnostics; never search unless a paid probe is authorized."""
import httpx


def probe_search_backend(research, backend: str, *, allow_paid_probe: bool = False) -> dict:
    if backend not in {"openalex", "tavily", "parallel"}:
        raise ValueError("unknown search backend")
    owner = research if backend == "openalex" else getattr(research, backend)
    try:
        key = research.openalex_api_key() if backend == "openalex" else owner.api_key()
    except (OSError, ValueError):
        return {"status": "NO_CREDENTIAL"}
    if not key:
        return {"status": "NO_CREDENTIAL"}
    if backend == "parallel" and not allow_paid_probe:
        return {"status": "NOT_TESTED", "reason": "Paid search probe requires explicit consent"}
    try:
        with httpx.Client(timeout=min(research.request_timeout_seconds, 15.0)) as client:
            if backend == "parallel":
                response = client.post(owner.base_url.rstrip("/") + "/v1/search",
                    headers={"x-api-key": key}, json={
                        "mode": "fast", "objective": "API connectivity check",
                        "search_queries": ["OpenAlex"],
                        "advanced_settings": {"max_results": 1}, "max_chars_total": 1000})
            else:
                base = research.openalex_base_url if backend == "openalex" else owner.base_url
                response = client.get(base.rstrip("/") + ("/rate-limit" if backend == "openalex" else "/usage"),
                                      headers={"Authorization": "Bearer " + key})
        if response.status_code in {401, 403}:
            return {"status": "AUTH_FAILED", "http_status": response.status_code}
        if response.status_code == 429:
            return {"status": "RATE_LIMITED", "http_status": 429}
        if not response.is_success:
            return {"status": "UNAVAILABLE", "http_status": response.status_code}
        data = response.json()
        expected = "rate_limit" if backend == "openalex" else "key" if backend == "tavily" else "results"
        expected_type = list if backend == "parallel" else dict
        if not isinstance(data, dict) or not isinstance(data.get(expected), expected_type):
            return {"status": "UNEXPECTED_RESPONSE"}
        result = {"status": "AVAILABLE"}
        if backend == "openalex":
            remaining = data["rate_limit"].get("credits_remaining")
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
                result["remaining"] = remaining
        elif backend == "tavily":
            for field in ("usage", "limit"):
                value = data["key"].get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    result[field] = value
        return result
    except (httpx.HTTPError, ValueError):
        # Do not echo response bodies, request headers, or credential-bearing exception messages.
        return {"status": "UNAVAILABLE"}
