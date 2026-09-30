from __future__ import annotations

import hashlib
import re
import secrets
from enum import Enum
from pydantic import BaseModel, ConfigDict

from project_ensemble.errors import PolicyNotConfiguredError


class AmendmentType(str, Enum):
    OBJECTION = "OBJECTION"
    SUPPLEMENTARY = "SUPPLEMENTARY"


class AmendmentSubmission(BaseModel):
    amendment_id: str
    proposer_id: str
    text: str
    declared_type: AmendmentType
    declared_impact_scope: list[str]


class ChairRuling(BaseModel):
    ruling_id: str
    amendment_id: str
    field: str
    before: object
    after: object
    reason: str
    procedural_effect: str | None = None


class ConflictEdge(BaseModel):
    a: str
    b: str
    reason: str
    ruling_id: str


class RandomizedAmendmentOrder(BaseModel):
    model_config = ConfigDict(frozen=True)
    seed: str
    algorithm: str = "sha256(seed_bytes || NUL || amendment_id), ascending digest"
    order: list[str]


class AmendmentConflictGraph:
    """Explicit graph maintained from Chair rulings; no semantic conflict inference occurs here."""
    def __init__(self):
        self._edges: dict[frozenset[str], ConflictEdge] = {}

    def add(self, a: str, b: str, *, reason: str, ruling_id: str) -> None:
        if a == b:
            raise ValueError("self-conflict is invalid")
        edge = ConflictEdge(a=a, b=b, reason=reason, ruling_id=ruling_id)
        self._edges[frozenset((a, b))] = edge

    def conflicts(self, a: str, b: str) -> bool:
        return frozenset((a, b)) in self._edges

    def edges(self) -> list[ConflictEdge]:
        return list(self._edges.values())


class AmendmentDocket:
    def __init__(self, submissions: list[AmendmentSubmission]):
        self.by_id = {x.amendment_id: x for x in submissions}
        if len(self.by_id) != len(submissions):
            raise ValueError("amendment IDs must be unique within a submission window")

    def ordered(self, order: list[str] | None) -> list[AmendmentSubmission]:
        if order is None:
            raise PolicyNotConfiguredError("same-window amendment ordering is unresolved; explicit human/Chair-governance order required")
        if set(order) != set(self.by_id):
            raise ValueError("order must contain each amendment exactly once")
        if len(order) != len(self.by_id):
            raise ValueError("order must not contain duplicate amendment IDs")
        return [self.by_id[x] for x in order]

    def randomized(self, *, seed: str | None = None) -> RandomizedAmendmentOrder:
        """Order a frozen window using a recorded, replayable random seed."""
        seed = seed or secrets.token_hex(32)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", seed):
            raise ValueError("random amendment-order seed must be exactly 32 bytes of hexadecimal")
        seed_bytes = bytes.fromhex(seed)

        def sort_key(amendment_id: str) -> tuple[bytes, str]:
            digest = hashlib.sha256(seed_bytes + b"\0" + amendment_id.encode("utf-8")).digest()
            return digest, amendment_id

        order = sorted(self.by_id, key=sort_key)
        return RandomizedAmendmentOrder(seed=seed.lower(), order=order)
