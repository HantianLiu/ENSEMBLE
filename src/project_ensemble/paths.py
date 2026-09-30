from __future__ import annotations

import sysconfig
from pathlib import Path


def bundled_governance_docs() -> Path:
    """Locate the governance package in a source checkout or installed wheel."""

    source_checkout = Path(__file__).resolve().parents[2] / "docs" / "governance"
    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "project-ensemble-v071"
        / "governance"
    )
    for candidate in (source_checkout, installed):
        if (candidate / "01_constitution").is_dir():
            return candidate.resolve()
    raise FileNotFoundError(
        "the v0.7.1 governance package is missing; reinstall project-ensemble-v071"
    )


def bundled_legacy_governance_docs() -> Path | None:
    """Return the immutable pre-translation package for already-frozen meetings."""

    archive_name = "governance_legacy_2026_09_23"
    candidates = (
        Path(__file__).resolve().parents[2] / "docs" / archive_name,
        Path(sysconfig.get_path("data")) / "share" / "project-ensemble-v071" / archive_name,
    )
    for candidate in candidates:
        if (candidate / "01_constitution").is_dir():
            return candidate.resolve()
    return None


def bundled_historical_governance_docs() -> tuple[Path, ...]:
    """Locate archived governance packages without relaxing frozen digest checks."""

    archive_names = (
        "governance_legacy_2026_09_23",
        "governance_frozen_2026_09_24_pre_replan",
    )
    roots = (
        Path(__file__).resolve().parents[2] / "docs",
        Path(sysconfig.get_path("data")) / "share" / "project-ensemble-v071",
    )
    found: list[Path] = []
    for archive_name in archive_names:
        for root in roots:
            candidate = root / archive_name
            if (candidate / "01_constitution").is_dir():
                found.append(candidate.resolve())
                break
    return tuple(found)
