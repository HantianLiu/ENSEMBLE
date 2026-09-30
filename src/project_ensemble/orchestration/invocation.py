from __future__ import annotations

import time
from collections.abc import Callable

from project_ensemble.domain import GenerationRequest, GenerationResponse
from project_ensemble.errors import RepresentativeUnavailableError
from project_ensemble.orchestration.retry_state import MeetingStatus
from project_ensemble.providers.base import ProviderAdapter
from project_ensemble.providers.base import StreamProgressCallback
from project_ensemble.providers.retry import call_with_retries


class RepresentativeInvoker:
    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay_seconds: float = 30.0,
        on_unavailable: Callable[[str, RepresentativeUnavailableError], None] | None = None,
        on_retry: Callable[[str, int, int, float, Exception], None] | None = None,
    ):
        self.max_retries = max_retries
        self.base_delay_seconds = base_delay_seconds
        self.on_unavailable = on_unavailable
        self.on_retry = on_retry

    def invoke(
        self,
        representative_id: str,
        adapter: ProviderAdapter,
        request: GenerationRequest,
        meeting_status: MeetingStatus,
        *,
        sleep: Callable[[float], None] = time.sleep,
        on_stream_progress: StreamProgressCallback | None = None,
    ) -> GenerationResponse:
        try:
            return call_with_retries(
                lambda: adapter.generate_with_progress(request, on_stream_progress),
                max_retries=self.max_retries,
                base_delay_seconds=self.base_delay_seconds,
                sleep=sleep,
                on_retry=(
                    None
                    if self.on_retry is None
                    else lambda retry, maximum, delay, exc: self.on_retry(
                        representative_id, retry, maximum, delay, exc
                    )
                ),
            )
        except RepresentativeUnavailableError as exc:
            meeting_status.pause_for_unavailable(representative_id, exc)
            if self.on_unavailable is not None:
                self.on_unavailable(representative_id, exc)
            raise
