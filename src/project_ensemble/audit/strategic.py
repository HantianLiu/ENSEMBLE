from __future__ import annotations

from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field


class GamingPattern(str, Enum):
    BLANKET_COSPONSORSHIP = "blanket_cosponsorship"
    CONFLICTING_COSPONSORSHIP = "conflicting_cosponsorship"
    ATOMIC_ITEM_FRAGMENTATION = "atomic_item_fragmentation"
    SEMANTIC_DUPLICATE_AMENDMENT = "semantic_duplicate_amendment"
    TIMING_EXPLOITATION = "timing_exploitation"
    BACKTRACKING_EXPLOITATION = "backtracking_exploitation"
    AMENDMENT_CATEGORY_GAMING = "amendment_category_gaming"
    LOW_INFORMATION_CREDIT = "low_information_drafting_credit"
    OTHER = "other"


class StrategicBehaviorEvent(BaseModel):
    event_id: str
    meeting_id: str
    representative_id: str
    pattern: GamingPattern
    evidence_refs: list[str] = Field(default_factory=list)
    procedural_effect: str = "none"


class GamingRisk(BaseModel):
    gaming_risk_id: str
    status: Literal["low", "moderate", "high"]
    observed_pattern: str
    affected_rules: list[str] = Field(default_factory=list)
    institutional_advantage_observed: bool = False
    institutional_advantage_description: str = ""
    substantive_justification: Literal["sufficient", "partial", "weak", "unknown"] = "unknown"
    evidence_refs: list[str] = Field(default_factory=list)
    cross_meeting_pattern_observed: bool = False
    recommended_action: list[Literal["observe", "modify", "experiment", "no_action"]] = Field(default_factory=list)
