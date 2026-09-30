import httpx
import pytest

from project_ensemble.errors import ProviderContentRejectedError, TransientProviderError
from project_ensemble.providers.http import checked_json


def test_http_429_retry_after_header_is_exposed_to_retry_policy():
    response = httpx.Response(
        429,
        headers={"Retry-After": "75"},
        json={"error": {"message": "rate limited"}},
        request=httpx.Request("GET", "https://provider.invalid/models"),
    )
    with pytest.raises(TransientProviderError) as captured:
        checked_json(response)
    assert captured.value.retry_after_seconds == 75


def test_google_retry_delay_body_is_exposed_to_retry_policy():
    response = httpx.Response(
        429,
        json={"error": {"details": [{"retryDelay": "12.5s"}]}},
        request=httpx.Request("POST", "https://provider.invalid/generate"),
    )
    with pytest.raises(TransientProviderError) as captured:
        checked_json(response)
    assert captured.value.retry_after_seconds == 12.5


def test_content_risk_rejection_keeps_provider_request_id_for_human_fallback():
    response = httpx.Response(
        400,
        json={"error": {"message": "Content Exists Risk (request_id: abc-123)"}},
        request=httpx.Request("POST", "https://provider.invalid/chat/completions"),
    )
    with pytest.raises(ProviderContentRejectedError) as captured:
        checked_json(response)
    assert captured.value.status_code == 400
    assert captured.value.provider_request_id == "abc-123"
