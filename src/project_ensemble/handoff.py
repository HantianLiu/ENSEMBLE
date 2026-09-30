from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class ChairHandoffBrief(BaseModel):
    meeting_id: str
    task_goal: str
    approved_constraints: list[str] = Field(default_factory=list)
    provenance_refs: list[str] = Field(default_factory=list)
    contested_items: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    freeform: dict[str, Any] = Field(default_factory=dict)


class ThinkTankReview(BaseModel):
    meeting_id: str
    reviewer_ids: list[str]
    findings: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    freeform: dict[str, Any] = Field(default_factory=dict)


class HumanHandoffBundle(BaseModel):
    chair_brief: ChairHandoffBrief
    think_tank_reviews: list[ThinkTankReview]

    def model_post_init(self, __context) -> None:
        mids = {x.meeting_id for x in self.think_tank_reviews}
        if mids and mids != {self.chair_brief.meeting_id}:
            raise ValueError("all handoff components must refer to the same meeting")
