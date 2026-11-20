"""Assemble the API response from resolver and planner output."""

from __future__ import annotations

from typing import Any

from .models import PlanRequest, SimulateRequest
from .planner import CandidateBlock, plan_order
from .resolver import Resolution, Resolver
from .simulator import CacheSimulator, QueryOutcome

CATEGORY_LABELS = {
    "missing_record": "缺记录",
    "cname_loop": "成环",
    "external_target": "链出已提交的权威区",
    "chain_too_long": "CNAME 链过长",
}

STATUS_MEANINGS = {
    "already_broken": "现状已无法解析，先修复再发布",
    "safe_order_found": "存在安全顺序",
    "no_safe_order": "不存在安全顺序，返回最大安全前缀与卡点",
}


def _resolution_json(res: Resolution) -> dict[str, Any]:
    return {
        "domain": res.domain,
        "resolved": res.ok,
        "addresses": res.addresses,
        "category": res.category,
        "category_label": CATEGORY_LABELS.get(res.category) if res.category else None,
        "detail": res.detail,
        "message": res.message,
        "chain": res.chain_text(),
        "trace": [
            {
                "name": t.name,
                "zone": t.zone,
                "outcome": t.outcome,
                "target": t.target,
                "addresses": t.addresses,
            }
            for t in res.trace
        ],
    }


def _failure_summary(failures: list[Resolution]) -> dict[str, Any]:
    counts = {key: 0 for key in CATEGORY_LABELS}
    for f in failures:
        if f.category:
            counts[f.category] += 1
    return {
        "total": len(failures),
        "by_category": counts,
        "by_category_label": {
            CATEGORY_LABELS[k]: v for k, v in counts.items() if v
        },
    }


def _steps(
    request: PlanRequest,
    resolver: Resolver,
    order: list[int],
    domains: list[str],
) -> list[dict[str, Any]]:
    """Describe each cutover with the resolution state before and after it."""
    steps: list[dict[str, Any]] = []
    released: set[int] = set()
    for number, idx in enumerate(order, start=1):
        zone = request.zones[idx]
        before = resolver.resolve_all(domains, frozenset(released))
        released.add(idx)
        after = resolver.resolve_all(domains, frozenset(released))
        steps.append(
            {
                "step": number,
                "zone": zone.name,
                "action": "整区切换到新记录",
                "addresses_before": {
                    r.domain: r.addresses for r in before if r.ok
                },
                "addresses_after": {
                    r.domain: r.addresses for r in after if r.ok
                },
                "all_required_resolvable": all(r.ok for r in after),
            }
        )
    return steps


def _blocked_candidate_json(block: CandidateBlock) -> dict[str, Any]:
    return {
        "zone": block.zone_name,
        "categories": [
            {"category": c, "label": CATEGORY_LABELS[c]} for c in block.categories
        ],
        "failures": [_resolution_json(f) for f in block.failures],
    }


# --------------------------------------------------------------------------- #
# Cache-restart simulation
# --------------------------------------------------------------------------- #

SIM_ASSUMPTIONS = [
    "所有区均为权威区，且互不嵌套；一个名称只归属一个已提交的区。",
    "缓存初始为空，所有查询共用一个缓存；只缓存 A / CNAME 正向应答。",
    "缺记录（NXDOMAIN / NODATA）的应答不写入缓存，下一次查询会再次回源。",
    (
        "缓存按记录名存放；同一组记录的 TTL 取该名 RRset 中的最短 TTL，"
        "计时从收到回包的时刻开始。"
    ),
    "区的回源耗时是固定往返毫秒数：发出后经过该毫秒数收到回包。",
    (
        "缓存未命中且同名回源已在途时不重复发包，等待同一份回包，"
        "各查询收到后各自继续解析。"
    ),
    "同一毫秒内先处理回包，再处理新查找；先处理过期，再处理回包。",
    "按发出时刻统计回源：同一次查找的重试按实际发包次数计。",
    (
        "区数据在整个观测窗口内不变，只使用 records（重启后线上数据）；"
        "new_records 仅为与 /plan 格式一致而接受，不参与模拟。"
    ),
]


def _sim_result_json(outcome: QueryOutcome) -> dict[str, Any]:
    res = outcome.resolution
    return {
        "query_id": outcome.query_id,
        "domain": outcome.domain,
        "started_at_ms": outcome.started_at,
        "completion_ms": outcome.completion_ms,
        "resolved": res.ok,
        "addresses": res.addresses,
        "category": res.category,
        "category_label": CATEGORY_LABELS.get(res.category) if res.category else None,
        "detail": res.detail,
        "message": res.message,
        "chain": res.chain_text(),
        "trace": [
            {
                "name": t.name,
                "zone": t.zone,
                "outcome": t.outcome,
                "target": t.target,
                "addresses": t.addresses,
            }
            for t in res.trace
        ],
        "cache_hits": outcome.cache_hits,
        "coalesced_waits": outcome.coalesced_waits,
        "upstream_sent": outcome.upstream_sent,
    }


def _zone_traffic(zone: Any, stats: Any) -> dict[str, Any]:
    peak_second, peak_count = stats.peak()
    return {
        "zone": zone.name,
        "latency_ms": zone.latency_ms,
        "upstream_queries": stats.upstream_queries,
        "seconds": [
            {"second": second, "queries": count}
            for second, count in sorted(stats.per_second.items())
        ],
        "peak": {"second": peak_second, "queries": peak_count},
    }


def build_simulation(request: SimulateRequest) -> dict[str, Any]:
    simulator = CacheSimulator(request.zones)
    result = simulator.run([(q.at_ms, q.name) for q in request.queries])

    failures = [o.resolution for o in result.outcomes if not o.resolution.ok]
    total = sum(stats.upstream_queries for stats in result.zone_stats.values())

    return {
        "mode": "cold_cache_simulation",
        "zone_count": len(request.zones),
        "query_count": len(request.queries),
        "assumptions": SIM_ASSUMPTIONS,
        "zones": [
            _zone_traffic(zone, result.zone_stats[zone.name])
            for zone in request.zones
        ],
        "total_upstream_queries": total,
        "finish_ms": result.finish_ms,
        "results": [_sim_result_json(o) for o in result.outcomes],
        "failure_summary": _failure_summary(failures),
    }


def build_plan(request: PlanRequest) -> dict[str, Any]:
    resolver = Resolver(request.zones)
    domains = request.required_domains
    initial = resolver.resolve_all(domains, frozenset())

    base: dict[str, Any] = {
        "mode": "authoritative_no_cache",
        "zone_count": len(request.zones),
        "domain_count": len(domains),
        "assumptions": [
            "所有区均为权威区，且互不嵌套；一个名称只归属一个已提交的区。",
            "每次发布都是整区切换，区内不会出现新旧记录共存的中间态。",
            "解析不经过缓存，只按当前状态下的权威记录推演。",
            (
                "CNAME 链离开已提交的区即视为无法确认；"
                "如需使用外部区，请把该区按新旧相同一并提交。"
            ),
        ],
    }

    initial_failures = [r for r in initial if not r.ok]
    if initial_failures:
        return {
            **base,
            "status": "already_broken",
            "status_meaning": STATUS_MEANINGS["already_broken"],
            "message": (
                "按现状的新旧记录推演，必须可用的域名已经无法解析到 IPv4 地址；"
                "在发布任何区之前就存在故障，以下为故障链路。"
            ),
            "order": [],
            "safe_prefix": [],
            "steps": [],
            "fault_chains": [_resolution_json(f) for f in initial_failures],
            "blocked_zones": [],
            "failure_summary": _failure_summary(initial_failures),
        }

    outcome = plan_order(resolver, request)
    final = resolver.resolve_all(domains, frozenset(range(len(request.zones))))

    if outcome.found_full:
        steps = _steps(request, resolver, outcome.safe_prefix, domains)
        return {
            **base,
            "status": "safe_order_found",
            "status_meaning": STATUS_MEANINGS["safe_order_found"],
            "message": (
                f"存在安全发布顺序：依次整区切换 {len(steps)} 个区，"
                "每一步完成后所有必须可用的域名都能解析到 IPv4 地址。"
            ),
            "order": [request.zones[i].name for i in outcome.safe_prefix],
            "safe_prefix": [request.zones[i].name for i in outcome.safe_prefix],
            "steps": steps,
            "final_state_resolvable": all(r.ok for r in final),
            "fault_chains": [],
            "blocked_zones": [],
            "failure_summary": _failure_summary([]),
        }

    safe_prefix = outcome.safe_prefix
    blocked = [_blocked_candidate_json(c) for c in outcome.candidates]
    blocked_failures = [f for c in outcome.candidates for f in c.failures]
    final_ok = all(r.ok for r in final)
    message = (
        "不存在能让所有域名在每一步都可解析的完整发布顺序。"
        f"以下为可安全走到的最大前缀（{len(safe_prefix)} 个区）；"
        "剩余的每个区若紧接着发布都会断链，已逐个列出断在哪条解析链上，"
        "并区分缺记录与成环。请先修复记录或拆小变更后重试。"
    )
    if not final_ok:
        message += (
            " 注意：即使所有区都发布完成，新状态本身也无法解析，"
            "需要先修正 new_records。"
        )
    return {
        **base,
        "status": "no_safe_order",
        "status_meaning": STATUS_MEANINGS["no_safe_order"],
        "message": message,
        "order": [],
        "safe_prefix": [request.zones[i].name for i in safe_prefix],
        "safe_prefix_is_maximal": True,
        "steps": _steps(request, resolver, safe_prefix, domains),
        "final_state_resolvable": final_ok,
        "fault_chains": [],
        "blocked_zones": blocked,
        "failure_summary": _failure_summary(blocked_failures),
    }
