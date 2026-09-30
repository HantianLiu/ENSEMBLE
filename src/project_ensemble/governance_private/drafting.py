from __future__ import annotations

from collections import defaultdict
from math import floor
from pydantic import BaseModel, Field, model_validator


class AtomicDraftItem(BaseModel):
    item_id: str
    adopted: bool
    author: str | None = None
    co_authors: set[str] = Field(default_factory=set)
    co_sponsors: set[str] = Field(default_factory=set)
    source_type: str  # e.g. initial_draft | amendment
    source_references: list[str] = Field(default_factory=list)
    retention_basis: str | None = None

    @model_validator(mode="after")
    def has_direct_authorship(self) -> "AtomicDraftItem":
        if self.author is None and not self.co_authors:
            raise ValueError("an atomic drafting item requires at least one direct author")
        if self.author is not None and self.author in self.co_authors:
            raise ValueError("primary author must not be repeated in co_authors")
        return self

    @property
    def direct_authors(self) -> set[str]:
        return ({self.author} if self.author is not None else set()) | set(self.co_authors)


class DraftingAlignmentResult(BaseModel):
    scores: dict[str, float]
    authored_adopted: dict[str, int]
    cosponsored_adopted: dict[str, int]


def calculate_drafting_alignment(items: list[AtomicDraftItem], representatives: list[str]) -> DraftingAlignmentResult:
    """Governance-private. Initial retained items and adopted amendments are equal at item level."""
    authored = defaultdict(int)
    cosponsored = defaultdict(int)
    for item in items:
        if not item.adopted:
            continue
        for author in item.direct_authors:
            authored[author] += 1
        for rid in item.co_sponsors:
            if rid not in item.direct_authors:
                cosponsored[rid] += 1
    scores = {rid: authored[rid] + 0.5 * cosponsored[rid] for rid in representatives}
    return DraftingAlignmentResult(
        scores=scores,
        authored_adopted={rid: authored[rid] for rid in representatives},
        cosponsored_adopted={rid: cosponsored[rid] for rid in representatives},
    )


def choose_consultative(scores: dict[str, float], fraction: float = 0.30) -> set[str]:
    """Remove no more than floor(fraction*N); tied cutoff candidates are all retained."""
    if not 0 <= fraction < 1:
        raise ValueError("fraction must be in [0, 1)")
    n = len(scores)
    k = floor(fraction * n)
    if k <= 0:
        return set()
    ordered = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))
    cutoff_score = ordered[k - 1][1]
    # Retain the whole tie only when it crosses the conversion boundary.
    if k < n and ordered[k][1] == cutoff_score:
        selected = {rid for rid, score in ordered if score < cutoff_score}
    else:
        selected = {rid for rid, _score in ordered[:k]}
    assert len(selected) <= k
    return selected


def primary_drafter_candidates(scores: dict[str, float]) -> list[str]:
    if not scores:
        return []
    high = max(scores.values())
    return sorted(rid for rid, score in scores.items() if score == high)
