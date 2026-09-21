"""Shared lifecycle facts for bounded, domain-owned proposal planners."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProposalState(StrEnum):
    planning = "planning"
    validating = "validating"
    ready = "ready"
    applying = "applying"
    applied = "applied"
    cancelled = "cancelled"
    failed = "failed"


class ProposalActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: float
    stage: str = Field(max_length=64)
    detail: str = Field(default="", max_length=240)


class ProposalUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ProposalJob(BaseModel):
    """The common envelope only; each planner still owns and validates its typed result."""

    model_config = ConfigDict(extra="allow")

    id: str
    domain: str
    state: ProposalState
    generation_id: str
    base_digest: str = ""
    started_at: float
    updated_at: float
    activity: list[ProposalActivity] = Field(default_factory=list, max_length=40)
    usage: ProposalUsage = Field(default_factory=ProposalUsage)


def new_proposal(domain: str, *, base_digest: str = "", **domain_fields: Any) -> dict[str, Any]:
    now = time.time()
    return ProposalJob(
        id=uuid.uuid4().hex,
        domain=domain,
        state=ProposalState.planning,
        generation_id=uuid.uuid4().hex,
        base_digest=base_digest,
        started_at=now,
        updated_at=now,
        activity=[ProposalActivity(at=now, stage="planning")],
        **domain_fields,
    ).model_dump(mode="json")


def with_activity(value: dict[str, Any], stage: str, detail: str = "") -> dict[str, Any]:
    now = time.time()
    activity = list(value.get("activity") or [])
    event = ProposalActivity(at=now, stage=stage, detail=detail[:240]).model_dump(mode="json")
    if not activity or (activity[-1].get("stage"), activity[-1].get("detail", "")) != (stage, detail[:240]):
        activity.append(event)
    return {**value, "updated_at": now, "activity": activity[-40:]}


def same_generation(current: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    return bool(
        current
        and current.get("id") == expected.get("id")
        and current.get("generation_id") == expected.get("generation_id")
    )


__all__ = ["ProposalActivity", "ProposalJob", "ProposalState", "ProposalUsage", "new_proposal", "same_generation", "with_activity"]
