from __future__ import annotations

from collections import deque
from project_ensemble.domain import GenerationRequest, GenerationResponse, ModelDescriptor
from project_ensemble.providers.base import ProviderAdapter


class ScriptedProviderAdapter(ProviderAdapter):
    """Deterministic provider for dry runs and integration tests."""

    def __init__(self, provider_id: str, model_ids: list[str], responses: list[str] | None = None):
        self.provider_id = provider_id
        self.model_ids = list(model_ids)
        self._responses = deque(responses or [])

    def list_models(self) -> list[ModelDescriptor]:
        return [ModelDescriptor(provider_id=self.provider_id, model_id=m, owned_by=self.provider_id) for m in self.model_ids]

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if request.model_id not in self.model_ids:
            raise ValueError(f"unknown fake model {request.model_id}")
        if not self._responses:
            raise RuntimeError("scripted provider has no queued response")
        text = self._responses.popleft()
        return GenerationResponse(text=text, provider_id=self.provider_id, model_id=request.model_id, raw={"scripted": True})
