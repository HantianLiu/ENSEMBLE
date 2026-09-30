from __future__ import annotations

import json
from math import ceil, floor

from project_ensemble.domain import DecisionRigor


def supermajority_threshold(n: int) -> int:
    """Current ENSEMBLE high threshold: ceil(3N/4)."""
    if n <= 0:
        raise ValueError("n must be positive")
    return ceil(3 * n / 4)


def strict_majority_threshold(n: int) -> int:
    """Smallest integer vote count satisfying > N/2."""
    if n <= 0:
        raise ValueError("n must be positive")
    return floor(n / 2) + 1


def meeting_decision_rigor(repo) -> DecisionRigor:
    """A missing field means an older, strictly governed meeting."""

    manifest = json.loads(repo.docs.read_text("identity_private/meeting_manifest.json"))
    return DecisionRigor(manifest.get("decision_rigor", DecisionRigor.STRICT.value))


def high_threshold(repo, n: int) -> int:
    """Resolve a former 3/4 threshold using this meeting's frozen Human choice."""

    if meeting_decision_rigor(repo) == DecisionRigor.RELAXED:
        return strict_majority_threshold(n)
    return supermajority_threshold(n)


def high_threshold_formula(repo, *, active_symbol: str = "N_ACTIVE") -> str:
    if meeting_decision_rigor(repo) == DecisionRigor.RELAXED:
        return f"floor({active_symbol}/2)+1"
    return f"ceil(3*{active_symbol}/4)"
