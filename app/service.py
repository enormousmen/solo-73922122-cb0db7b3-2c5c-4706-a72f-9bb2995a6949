"""Assemble the API response from resolver and planner output."""

from __future__ import annotations

from typing import Any

from .models import PlanRequest
from .planner import CandidateBlock, plan_order
from .resolver import Resolution, Resolver

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
            "CNAME 链离开已提交的区即视为无法确认；"
            "如需使用外部区，请把该区按新旧相同一并提交。",
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
