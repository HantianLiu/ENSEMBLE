from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class AuditTurn:
    member_id: str
    round_index: int
    content: str
    is_pass: bool


@dataclass
class AuditDiscussion:
    members: list[str]
    max_turns_per_member: int = 5
    seed: int | None = None
    turns: list[AuditTurn] = field(default_factory=list)

    def __post_init__(self):
        self._rng = random.Random(self.seed)
        self._base_order = list(self.members)
        self._rng.shuffle(self._base_order)

    @property
    def base_order(self) -> tuple[str, ...]:
        return tuple(self._base_order)

    def order_for_round(self, round_index: int) -> list[str]:
        if not self._base_order:
            return []
        k = round_index % len(self._base_order)
        return self._base_order[k:] + self._base_order[:k]

    def used_turns(self, member_id: str) -> int:
        return sum(t.member_id == member_id for t in self.turns)

    def record(self, member_id: str, round_index: int, content: str) -> AuditTurn:
        if member_id not in self.members:
            raise ValueError("unknown audit member")
        if self.used_turns(member_id) >= self.max_turns_per_member:
            raise ValueError("audit speaking-turn limit reached")
        expected_order = self.order_for_round(round_index)
        round_turns = [t for t in self.turns if t.round_index == round_index]
        if len(round_turns) >= len(expected_order):
            raise ValueError("round is already complete")
        expected_member = expected_order[len(round_turns)]
        if member_id != expected_member:
            raise ValueError(f"expected {expected_member}, got {member_id}")
        is_pass = content.strip().upper() == "PASS"
        turn = AuditTurn(member_id=member_id, round_index=round_index, content=content, is_pass=is_pass)
        self.turns.append(turn)
        return turn

    def round_all_pass(self, round_index: int) -> bool:
        items = [t for t in self.turns if t.round_index == round_index]
        return len(items) == len(self.members) and all(t.is_pass for t in items)
