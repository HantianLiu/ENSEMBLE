"""Report safe shutdown while active parallel calls finish after Ctrl+C."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable


class DrainingThreadPoolExecutor(ThreadPoolExecutor):
    def __init__(self, *args, on_interrupt: Callable[[], None], **kwargs):
        super().__init__(*args, **kwargs)
        self._on_interrupt = on_interrupt

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
            self._on_interrupt()
            # Queued work has not started and can safely be discarded. Active
            # requests must finish before their threads are joined, so frozen
            # evidence files cannot be left half-written by interpreter exit.
            self.shutdown(wait=True, cancel_futures=True)
            return False
        return super().__exit__(exc_type, exc_value, traceback)
