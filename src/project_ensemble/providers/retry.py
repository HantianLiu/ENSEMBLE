from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from project_ensemble.errors import RepresentativeUnavailableError, TransientProviderError

T = TypeVar("T")


def call_with_retries(
    fn: Callable[[], T],
    *,
    max_retries: int = 3,
    base_delay_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, int, float, Exception], None] | None = None,
) -> T:
    """Initial attempt + max_retries. Exhaustion means the Representative is unavailable."""
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except TransientProviderError as exc:
            last = exc
            if attempt >= max_retries:
                break
            delay = base_delay_seconds * (2 ** attempt)
            if exc.retry_after_seconds is not None:
                delay = max(delay, exc.retry_after_seconds)
            if on_retry is not None:
                on_retry(attempt + 1, max_retries, delay, exc)
            sleep(delay)
    detail = str(last).strip().replace("\n", " ")[:200] if last is not None else "unknown transient failure"
    raise RepresentativeUnavailableError(
        f"provider unavailable after {max_retries} retries; last transient failure: {detail}"
    ) from last
