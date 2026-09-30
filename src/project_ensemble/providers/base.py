from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from project_ensemble.domain import GenerationRequest, GenerationResponse, ModelDescriptor


StreamProgressCallback = Callable[[str, int, int], None]


class ProviderAdapter(ABC):
    provider_id: str

    @abstractmethod
    def list_models(self) -> list[ModelDescriptor]: ...

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResponse: ...

    def generate_with_progress(
        self,
        request: GenerationRequest,
        on_progress: StreamProgressCallback | None = None,
    ) -> GenerationResponse:
        """Generate with optional transport progress; non-streaming adapters fall back safely."""

        if on_progress is not None:
            on_progress("unsupported", 0, 0)
        return self.generate(request)

    def reported_concurrency_limit(self, model_id: str) -> int | None:
        """Return an explicit provider-reported concurrent-request limit.

        Rate limits such as RPM and TPM are intentionally not accepted here:
        throughput per time window is not the same quantity as in-flight calls.
        """

        return None
