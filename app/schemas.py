"""Request/response shapes for the planner, documented for OpenAPI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TraceStep(BaseModel):
    name: str
    zone: str | None
    outcome: Literal[
        "cname", "a", "nodata", "nxdomain", "external", "loop", "too_long"
    ]
    target: str | None = None
    addresses: list[str] = Field(default_factory=list)


class ChainFailure(BaseModel):
    domain: str
    resolved: bool
    addresses: list[str]
    category: Literal[
        "missing_record", "cname_loop", "external_target", "chain_too_long"
    ]
    category_label: str | None
    detail: str | None = None
    message: str
    chain: str
    trace: list[TraceStep]


class FailureSummary(BaseModel):
    total: int
    by_category: dict[str, int]
    by_category_label: dict[str, int]


class PlanStep(BaseModel):
    step: int
    zone: str
    action: str
    addresses_before: dict[str, list[str]]
    addresses_after: dict[str, list[str]]
    all_required_resolvable: bool


class BlockedZone(BaseModel):
    zone: str
    categories: list[dict[str, str]]
    failures: list[ChainFailure]


class PlanResponse(BaseModel):
    mode: Literal["authoritative_no_cache"]
    zone_count: int
    domain_count: int
    assumptions: list[str]
    status: Literal["already_broken", "safe_order_found", "no_safe_order"]
    status_meaning: str
    message: str
    order: list[str]
    safe_prefix: list[str]
    safe_prefix_is_maximal: bool | None = None
    steps: list[PlanStep]
    final_state_resolvable: bool | None = None
    fault_chains: list[ChainFailure]
    blocked_zones: list[BlockedZone]
    failure_summary: FailureSummary

    model_config = {"extra": "allow"}


class HealthResponse(BaseModel):
    status: str
    version: str


# --------------------------------------------------------------------------- #
# Cache-restart simulation
# --------------------------------------------------------------------------- #


class SecondBucket(BaseModel):
    second: int
    queries: int


class PeakSecond(BaseModel):
    second: int | None
    queries: int


class ZoneTraffic(BaseModel):
    zone: str
    latency_ms: int
    upstream_queries: int
    # Only seconds in which the zone actually received an upstream query.
    seconds: list[SecondBucket]
    peak: PeakSecond


class QueryTraceStep(BaseModel):
    name: str
    zone: str | None
    outcome: Literal[
        "cname", "a", "nodata", "nxdomain", "external", "loop", "too_long"
    ]
    target: str | None = None
    addresses: list[str] = Field(default_factory=list)


class QueryResult(BaseModel):
    query_id: int
    domain: str
    started_at_ms: int
    completion_ms: int
    resolved: bool
    addresses: list[str]
    category: Literal[
        "missing_record", "cname_loop", "external_target", "chain_too_long"
    ] | None = None
    category_label: str | None = None
    detail: str | None = None
    message: str
    chain: str
    trace: list[QueryTraceStep]
    cache_hits: int
    coalesced_waits: int
    upstream_sent: int


class SimulateResponse(BaseModel):
    mode: Literal["cold_cache_simulation"]
    zone_count: int
    query_count: int
    assumptions: list[str]
    zones: list[ZoneTraffic]
    total_upstream_queries: int
    finish_ms: int
    results: list[QueryResult]
    failure_summary: FailureSummary
