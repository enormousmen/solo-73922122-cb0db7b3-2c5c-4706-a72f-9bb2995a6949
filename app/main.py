"""FastAPI entry point for the cross-zone DNS release-order planner."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from . import __version__
from .models import PlanRequest
from .schemas import HealthResponse, PlanResponse
from .service import build_plan

app = FastAPI(
    title="跨区 DNS 变更发布顺序规划器",
    version=__version__,
    summary="为内网 DNS 值班员推算整区切换的安全顺序",
    description=(
        "提交最多 8 个互不嵌套的权威区的新旧 A/CNAME 记录，以及必须持续可解析的"
        "业务域名。服务按无缓存权威记录推演每个整区切换后的状态：\n\n"
        "* 若存在安全顺序，返回逐步发布顺序，并附每一步前后的解析结果；\n"
        "* 若现状已无法解析，返回故障链路（缺记录 / 成环 / 链出权威区 / 链过长）；\n"
        "* 若无安全顺序，返回最大安全前缀，以及剩余每个区发布后会断在哪条解析链上。\n\n"
        "假设：每个区一次整区切换，区内没有新旧共存的中间态；解析无缓存；"
        "CNAME 链离开已提交的区即视为无法确认（可把外部区按新旧相同一并提交）。"
    ),
)


@app.get("/healthz", response_model=HealthResponse, summary="健康检查")
def healthz() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.post(
    "/plan",
    response_model=PlanResponse,
    response_model_exclude_none=True,
    summary="计算跨区发布顺序",
    responses={
        200: {"description": "推演完成，status 指明三种结果之一"},
        422: {"description": "请求校验失败（区/记录/域名不合法或超出限制）"},
    },
)
def plan(request: PlanRequest) -> dict[str, Any]:
    return build_plan(request)
