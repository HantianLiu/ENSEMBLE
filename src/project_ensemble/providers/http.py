from __future__ import annotations

import re
import queue
import threading
from contextlib import contextmanager
from typing import Any
from collections.abc import Callable, Iterator

import httpx
from project_ensemble.errors import (
    PermanentProviderError, ProviderContentRejectedError, TransientProviderError,
)

TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@contextmanager
def interruptible_stream_open(
    stream: Any,
    on_progress: Callable[[str, int, int], None] | None,
) -> Iterator[httpx.Response]:
    """Poll the Human control while a provider has not sent HTTP headers."""

    if on_progress is None:
        with stream as response:
            yield response
        return
    ready: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
    abandoned = threading.Event()
    handoff_lock = threading.Lock()

    def open_stream() -> None:
        try:
            response = stream.__enter__()
            with handoff_lock:
                if abandoned.is_set():
                    stream.__exit__(None, None, None)
                else:
                    ready.put(("response", response))
        except BaseException as exc:
            if not abandoned.is_set():
                ready.put(("error", exc))

    threading.Thread(target=open_stream, name="ensemble-sse-open", daemon=True).start()
    try:
        while True:
            on_progress("heartbeat", 0, 0)
            try:
                kind, value = ready.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == "error":
                if isinstance(value, BaseException):
                    raise value
                raise RuntimeError("HTTP stream opener returned an invalid error")
            break
    except BaseException:
        with handoff_lock:
            abandoned.set()
            if not ready.empty():
                kind, _value = ready.get_nowait()
                if kind == "response":
                    stream.__exit__(None, None, None)
        raise
    try:
        on_progress("heartbeat", 0, 0)
        yield value  # type: ignore[misc]
    finally:
        stream.__exit__(None, None, None)


def interruptible_stream_lines(
    response: httpx.Response,
    on_progress: Callable[[str, int, int], None] | None,
) -> Iterator[str]:
    """Keep a silent SSE body from trapping Ctrl-R until the HTTP read timeout.

    The socket reader is isolated in a daemon thread. On forced handoff the
    caller leaves the response context, closing the local connection; partial
    text is discarded by the normal invocation boundary.
    """

    if on_progress is None:
        yield from response.iter_lines()
        return
    lines: queue.Queue[tuple[str, object]] = queue.Queue()

    def read() -> None:
        try:
            for line in response.iter_lines():
                lines.put(("line", line))
        except Exception as exc:
            lines.put(("error", exc))
        finally:
            lines.put(("done", None))

    threading.Thread(target=read, name="ensemble-sse-reader", daemon=True).start()
    while True:
        on_progress("heartbeat", 0, 0)
        try:
            kind, value = lines.get(timeout=0.25)
        except queue.Empty:
            continue
        if kind == "line":
            yield str(value)
        elif kind == "error":
            if isinstance(value, BaseException):
                raise value
            raise RuntimeError("HTTP stream reader returned an invalid error")
        else:
            return


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
