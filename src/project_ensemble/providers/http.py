from __future__ import annotations

import re

import httpx
from project_ensemble.errors import (
    PermanentProviderError, ProviderContentRejectedError, TransientProviderError,
)

TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    try:
        details = response.json().get("error", {}).get("details", [])
    except Exception:
        return None
    for detail in details:
        if not isinstance(detail, dict):
            continue
        value = detail.get("retryDelay")
        if isinstance(value, str):
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", value.strip())
            if match:
                return float(match.group(1))
    return None


def checked_json(response: httpx.Response) -> dict:
    checked_status(response)
    try:
        data = response.json()
    except Exception as exc:
        raise TransientProviderError("provider returned non-JSON response") from exc
    if not isinstance(data, dict):
        raise TransientProviderError("provider returned unexpected JSON type")
    return data


def checked_status(response: httpx.Response) -> None:
    """Validate an HTTP response without forcing a successful stream into memory."""

    if response.status_code in TRANSIENT_STATUS:
        raise TransientProviderError(
            f"provider returned HTTP {response.status_code}",
            retry_after_seconds=_retry_after_seconds(response),
        )
    if response.status_code >= 400:
        if not response.is_closed:
            response.read()
        body = response.text[:500]
        if response.status_code == 400:
            try:
                error = response.json().get("error", {})
                message = str(error.get("message", "")) if isinstance(error, dict) else ""
            except (ValueError, TypeError):
                message = ""
            if "content exists risk" in message.casefold():
                match = re.search(r"request_id:\s*([a-zA-Z0-9-]+)", message)
                raise ProviderContentRejectedError(
                    f"provider returned HTTP 400: {message[:250]}",
                    status_code=400,
                    provider_request_id=match.group(1) if match else None,
                )
        raise PermanentProviderError(f"provider returned HTTP {response.status_code}: {body}")
