"""Keep repetitive non-fatal PDF parser diagnostics out of the live TUI."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass
class PdfWarningSummary:
    duplicate_length_count: int = 0
    first_duplicate_length: str | None = None
    broken_cmap_line_count: int = 0
    first_broken_cmap_line: str | None = None


class _RecoverablePdfWarningFilter(logging.Filter):
    def __init__(self, summary: PdfWarningSummary) -> None:
        super().__init__()
        self.summary = summary
        self.thread_id = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.thread != self.thread_id:
            return True
        message = record.getMessage()
        if (record.name == "pypdf.generic._data_structures"
                and message.startswith("Multiple definitions in dictionary")
                and "for key /Length" in message):
            self.summary.duplicate_length_count += 1
            if self.summary.first_duplicate_length is None:
                self.summary.first_duplicate_length = message
            return False
        if (record.name == "pypdf._cmap"
                and message.startswith("Skipping broken line ")
                and "Odd-length string" in message):
            self.summary.broken_cmap_line_count += 1
            if self.summary.first_broken_cmap_line is None:
                self.summary.first_broken_cmap_line = message
            return False
        return True


@contextmanager
def capture_duplicate_pdf_length_warnings() -> Iterator[PdfWarningSummary]:
    """Count known recoverable parser warnings without muting other errors."""
    summary = PdfWarningSummary()
    loggers = [logging.getLogger(name) for name in (
        "pypdf.generic._data_structures", "pypdf._cmap",
    )]
    filter_ = _RecoverablePdfWarningFilter(summary)
    for logger in loggers:
        logger.addFilter(filter_)
    try:
        yield summary
    finally:
        for logger in loggers:
            logger.removeFilter(filter_)
