from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from project_ensemble.errors import AccessDeniedError


class Principal(str, Enum):
    REPRESENTATIVE = "representative"
    CHAIR = "chair"
    THINK_TANK = "think_tank"
    AUDIT = "audit"
    HUMAN = "human"
    ORCHESTRATOR = "orchestrator"


def _top(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if parts else ""


class AccessPolicy:
    """Compartment-level policy. Representative-specific ownership is checked separately."""

    def assert_read(self, principal: Principal, path: str, *, representative_id: str | None = None) -> None:
        top = _top(path)
        if principal in {Principal.HUMAN, Principal.ORCHESTRATOR}:
            return
        if top == "public":
            return
        if top == "representatives":
            parts = PurePosixPath(path).parts
            owner = parts[1] if len(parts) > 1 else None
            if principal == Principal.REPRESENTATIVE and representative_id == owner:
                return
            if principal in {Principal.CHAIR, Principal.AUDIT}:
                return
        if top == "chair_private" and principal == Principal.CHAIR:
            return
        if top == "governance_private" and principal in {Principal.CHAIR, Principal.AUDIT}:
            return
        if top == "identity_private" and principal == Principal.AUDIT:
            return
        if top == "audit_private" and principal == Principal.AUDIT:
            return
        if top == "think_tank_private" and principal == Principal.THINK_TANK:
            return
        if top == "human_private":
            raise AccessDeniedError(f"{principal.value} cannot read {path}")
        raise AccessDeniedError(f"{principal.value} cannot read {path}")
