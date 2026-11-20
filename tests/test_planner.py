"""End-to-end tests: statuses, ordering, failure classification, validation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models import PlanRequest, Zone
from app.resolver import Resolver
from app.service import build_plan

client = TestClient(app)


def rec(name: str, rtype: str, value: str) -> dict[str, str]:
    return {"name": name, "type": rtype, "value": value}


def zone(name: str, old: list[dict], new: list[dict] | None = None) -> dict:
    return {"name": name, "records": old, "new_records": old if new is None else new}


def payload(zones: list[dict], domains: list[str]) -> dict:
    return {"zones": zones, "required_domains": domains}


def plan(zones: list[dict], domains: list[str]) -> dict:
    return build_plan(PlanRequest.model_validate(payload(zones, domains)))


def _resolver(zones: list[dict]) -> Resolver:
    return Resolver([Zone.model_validate(z) for z in zones])


def replay_and_assert_steps(out: dict, zones: list[dict], domains: list[str]) -> None:
    """Independently verify every reported step keeps all domains resolvable."""
    order = out["safe_prefix"] if out["status"] == "no_safe_order" else out["order"]
    assert len(order) == len(out["steps"])
    resolver = _resolver(zones)
    released: set[int] = set()
    for zone_name, step in zip(order, out["steps"]):
        assert step["zone"] == zone_name
        released.add(next(i for i, z in enumerate(zones) if z["name"] == zone_name))
        resolutions = resolver.resolve_all(domains, frozenset(released))
        assert all(r.ok for r in resolutions), [
            r.message for r in resolutions
        ]
        assert step["all_required_resolvable"] is True
        assert step["addresses_after"] == {r.domain: r.addresses for r in resolutions}
    # The reported prefix is maximal: every remaining zone breaks something,
    # and that is exactly what blocked_zones says.
    if out["status"] == "no_safe_order":
        remaining = [z["name"] for z in zones if z["name"] not in order]
        assert [b["zone"] for b in out["blocked_zones"]] == remaining
        for blocked in out["blocked_zones"]:
            idx = next(i for i, z in enumerate(zones) if z["name"] == blocked["zone"])
            resolutions = resolver.resolve_all(domains, frozenset(released | {idx}))
            assert [r.domain for r in resolutions if not r.ok] == [
                f["domain"] for f in blocked["failures"]
            ]


# --------------------------------------------------------------------------- #
# Safe order
# --------------------------------------------------------------------------- #


def test_target_zone_must_go_first() -> None:
    """New CNAME target only exists after zone B cuts over, so B must be first."""
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
            [rec("www.a.example.com", "CNAME", "svc2.b.example.com")],
        ),
        zone(
            "b.example.com",
            [rec("svc.b.example.com", "A", "10.1.1.1")],
            [
                rec("svc.b.example.com", "A", "10.1.1.1"),
                rec("svc2.b.example.com", "A", "10.2.2.2"),
            ],
        ),
    ]
    domains = ["www.a.example.com"]
    out = plan(zones, domains)
    assert out["status"] == "safe_order_found"
    assert out["order"] == ["b.example.com", "a.example.com"]
    assert out["final_state_resolvable"] is True
    replay_and_assert_steps(out, zones, domains)
    assert out["steps"][0]["addresses_after"]["www.a.example.com"] == ["10.1.1.1"]
    assert out["steps"][1]["addresses_after"]["www.a.example.com"] == ["10.2.2.2"]
    assert out["failure_summary"]["total"] == 0


def test_consumer_must_move_before_old_address_removed() -> None:
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
            [rec("www.a.example.com", "A", "10.9.9.9")],
        ),
        zone("b.example.com", [rec("svc.b.example.com", "A", "10.1.1.1")], []),
    ]
    domains = ["www.a.example.com"]
    out = plan(zones, domains)
    assert out["status"] == "safe_order_found"
    assert out["order"] == ["a.example.com", "b.example.com"]
    replay_and_assert_steps(out, zones, domains)


def test_multiple_domains_and_zones() -> None:
    zones = [
        zone("a.example.com", [rec("www.a.example.com", "A", "10.0.0.1")],
             [rec("www.a.example.com", "A", "10.0.0.2")]),
        zone("b.example.com", [rec("api.b.example.com", "A", "10.0.1.1")],
             [rec("api.b.example.com", "A", "10.0.1.2")]),
        zone("c.example.com", [rec("m.c.example.com", "A", "10.0.2.1")],
             [rec("m.c.example.com", "A", "10.0.2.2")]),
    ]
    domains = ["www.a.example.com", "api.b.example.com", "m.c.example.com"]
    out = plan(zones, domains)
    assert out["status"] == "safe_order_found"
    assert sorted(out["order"]) == ["a.example.com", "b.example.com", "c.example.com"]
    replay_and_assert_steps(out, zones, domains)


def test_unchanged_zone_modelled_with_identical_states() -> None:
    zones = [
        zone("a.example.com", [rec("www.a.example.com", "A", "10.0.0.1")]),
        zone("b.example.com", [rec("x.b.example.com", "CNAME", "www.a.example.com")]),
    ]
    out = plan(zones, ["x.b.example.com"])
    assert out["status"] == "safe_order_found"
    assert len(out["order"]) == 2
    replay_and_assert_steps(out, zones, ["x.b.example.com"])


def test_eight_zones_is_accepted_and_solved() -> None:
    zones = [
        zone(f"z{i}.example.com", [rec(f"www.z{i}.example.com", "A", "10.0.0.1")],
             [rec(f"www.z{i}.example.com", "A", "10.0.0.2")])
        for i in range(8)
    ]
    domains = [f"www.z{i}.example.com" for i in range(8)]
    out = plan(zones, domains)
    assert out["status"] == "safe_order_found"
    assert len(out["order"]) == 8
    replay_and_assert_steps(out, zones, domains)


def test_diamond_with_shared_alias_target() -> None:
    """Two consumer zones point at one provider zone; provider publishes first."""
    zones = [
        zone(
            "edge1.example.com",
            [rec("www.edge1.example.com", "CNAME", "lb.core.example.com")],
            [rec("www.edge1.example.com", "CNAME", "lb2.core.example.com")],
        ),
        zone(
            "edge2.example.com",
            [rec("www.edge2.example.com", "CNAME", "lb.core.example.com")],
            [rec("www.edge2.example.com", "CNAME", "lb2.core.example.com")],
        ),
        zone(
            "core.example.com",
            [rec("lb.core.example.com", "A", "10.5.0.1")],
            [
                rec("lb.core.example.com", "A", "10.5.0.1"),
                rec("lb2.core.example.com", "A", "10.5.0.2"),
            ],
        ),
    ]
    domains = ["www.edge1.example.com", "www.edge2.example.com"]
    out = plan(zones, domains)
    assert out["status"] == "safe_order_found"
    assert out["order"][0] == "core.example.com"
    replay_and_assert_steps(out, zones, domains)


# --------------------------------------------------------------------------- #
# Already broken at the initial state
# --------------------------------------------------------------------------- #


def test_initially_missing_record_returns_fault_chain() -> None:
    zones = [
        zone("a.example.com",
             [rec("www.a.example.com", "CNAME", "gone.b.example.com")],
             [rec("www.a.example.com", "A", "10.0.0.1")]),
        zone("b.example.com", []),
    ]
    out = plan(zones, ["www.a.example.com"])
    assert out["status"] == "already_broken"
    assert out["order"] == []
    assert out["safe_prefix"] == []
    chain = out["fault_chains"][0]
    assert chain["category"] == "missing_record"
    assert chain["category_label"] == "缺记录"
    assert chain["detail"] == "nxdomain"
    assert chain["chain"] == (
        "www.a.example.com --CNAME--> gone.b.example.com => 无记录（NXDOMAIN）"
    )
    assert out["failure_summary"]["by_category_label"] == {"缺记录": 1}


def test_missing_record_distinguishes_nodata_from_nxdomain() -> None:
    """A CNAME target that exists but holds no A record is NODATA, not NXDOMAIN."""
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "CNAME", "alias.b.example.com")],
            [rec("www.a.example.com", "A", "10.0.0.1")],
        ),
        zone(
            "b.example.com",
            [rec("deeper.alias.b.example.com", "A", "10.0.1.1")],
            [rec("deeper.alias.b.example.com", "A", "10.0.1.1")],
        ),
    ]
    out = plan(zones, ["www.a.example.com"])
    assert out["status"] == "already_broken"
    chain = out["fault_chains"][0]
    assert chain["category"] == "missing_record"
    assert chain["detail"] == "nodata"
    assert chain["chain"].endswith("=> 无记录（NODATA）")


def test_initially_loop_returns_fault_chain() -> None:
    zones = [
        zone(
            "a.example.com",
            [
                rec("x.a.example.com", "CNAME", "y.a.example.com"),
                rec("y.a.example.com", "CNAME", "x.a.example.com"),
            ],
            [rec("x.a.example.com", "A", "10.0.0.1")],
        )
    ]
    out = plan(zones, ["x.a.example.com"])
    assert out["status"] == "already_broken"
    chain = out["fault_chains"][0]
    assert chain["category"] == "cname_loop"
    assert chain["category_label"] == "成环"
    assert chain["chain"].endswith("=> 回到 x.a.example.com，成环")
    assert [t["outcome"] for t in chain["trace"]] == ["cname", "cname", "loop"]


def test_initial_external_target() -> None:
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "CNAME", "www.partner.example.org")],
        )
    ]
    out = plan(zones, ["www.a.example.com"])
    assert out["status"] == "already_broken"
    assert out["fault_chains"][0]["category"] == "external_target"


def test_initial_broken_reported_for_every_domain() -> None:
    zones = [
        zone("a.example.com", [rec("ok.a.example.com", "A", "10.0.0.1")], []),
    ]
    out = plan(zones, ["ok.a.example.com", "missing.a.example.com"])
    assert out["status"] == "already_broken"
    assert {c["domain"] for c in out["fault_chains"]} == {"missing.a.example.com"}


# --------------------------------------------------------------------------- #
# No safe order
# --------------------------------------------------------------------------- #


def test_mutual_dependency_reports_nonempty_safe_prefix() -> None:
    """A and B block each other; independent zone C is publishable first."""
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "CNAME", "t.b.example.com")],
            [rec("www.a.example.com", "CNAME", "u.b.example.com")],
        ),
        zone(
            "b.example.com",
            [rec("t.b.example.com", "A", "10.0.1.1")],
            [rec("u.b.example.com", "A", "10.0.1.2")],
        ),
        zone(
            "c.example.com",
            [rec("m.c.example.com", "A", "10.0.2.1")],
            [rec("m.c.example.com", "A", "10.0.2.2")],
        ),
    ]
    domains = ["www.a.example.com", "m.c.example.com"]
    out = plan(zones, domains)
    assert out["status"] == "no_safe_order"
    assert out["safe_prefix"] == ["c.example.com"]
    assert out["safe_prefix_is_maximal"] is True
    replay_and_assert_steps(out, zones, domains)
    assert {b["zone"] for b in out["blocked_zones"]} == {
        "a.example.com",
        "b.example.com",
    }
    # Either remaining zone breaks the CNAME chain on a missing record.
    assert out["failure_summary"]["by_category"]["missing_record"] == 2


def test_intermediate_loop_is_reported_as_loop() -> None:
    """Publishing B closes a CNAME loop; A alone is safe, so A is the prefix."""
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "A", "10.0.0.1")],
            [rec("www.a.example.com", "CNAME", "loop.b.example.com")],
        ),
        zone(
            "b.example.com",
            [rec("loop.b.example.com", "A", "10.0.0.2")],
            [rec("loop.b.example.com", "CNAME", "www.a.example.com")],
        ),
    ]
    domains = ["www.a.example.com"]
    out = plan(zones, domains)
    assert out["status"] == "no_safe_order"
    assert out["safe_prefix"] == ["a.example.com"]
    replay_and_assert_steps(out, zones, domains)
    assert [b["zone"] for b in out["blocked_zones"]] == ["b.example.com"]
    assert out["blocked_zones"][0]["categories"] == [
        {"category": "cname_loop", "label": "成环"}
    ]
    assert out["failure_summary"]["by_category_label"] == {"成环": 1}


def test_blocked_zones_distinguish_missing_record_and_loop() -> None:
    """Different remaining zones fail for different reasons."""
    zones = [
        zone(
            "a.example.com",
            [rec("one.a.example.com", "CNAME", "keep.b.example.com"),
             rec("two.a.example.com", "CNAME", "keep.c.example.com")],
            [rec("one.a.example.com", "CNAME", "gone.b.example.com"),
             rec("two.a.example.com", "CNAME", "keep.c.example.com")],
        ),
        zone("b.example.com", [rec("keep.b.example.com", "A", "10.0.1.1")], []),
        zone(
            "c.example.com",
            [rec("keep.c.example.com", "A", "10.0.2.1")],
            [rec("keep.c.example.com", "CNAME", "keep.c.example.com")],
        ),
    ]
    domains = ["one.a.example.com", "two.a.example.com"]
    out = plan(zones, domains)
    assert out["status"] == "no_safe_order"
    by_zone = {b["zone"]: b for b in out["blocked_zones"]}
    # Publishing a (or b) strands one.a on a removed name; publishing c loops two.a.
    assert [c["category"] for c in by_zone["a.example.com"]["categories"]] == [
        "missing_record"
    ]
    assert [c["category"] for c in by_zone["c.example.com"]["categories"]] == [
        "cname_loop"
    ]
    assert out["failure_summary"]["by_category"]["missing_record"] == 2
    assert out["failure_summary"]["by_category"]["cname_loop"] == 1
    assert out["failure_summary"]["by_category_label"] == {"缺记录": 2, "成环": 1}


def test_final_state_removing_all_addresses_is_not_a_safe_order() -> None:
    zones = [zone("a.example.com", [rec("www.a.example.com", "A", "10.0.0.1")], [])]
    out = plan(zones, ["www.a.example.com"])
    assert out["status"] == "no_safe_order"
    assert out["safe_prefix"] == []
    assert out["final_state_resolvable"] is False
    assert "新状态本身也无法解析" in out["message"]
    assert out["blocked_zones"][0]["failures"][0]["category"] == "missing_record"


def test_safe_prefix_is_longest_possible_not_merely_greedy() -> None:
    """Publishing A first is safe but strands everything after one step.

    Publishing B first is also safe and allows B, C before reaching the actual
    dead end.  A greedy planner that just takes the first safe zone (A) would
    report a shorter prefix than necessary.
    """
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "A", "10.0.0.1")],
            [rec("www.a.example.com", "CNAME", "p.b.example.com")],
        ),
        zone(
            "b.example.com",
            [rec("p.b.example.com", "A", "10.0.1.1")],
            [rec("q.b.example.com", "A", "10.0.1.2")],
        ),
        zone(
            "c.example.com",
            [rec("www.c.example.com", "A", "10.0.2.1")],
            [rec("www.c.example.com", "CNAME", "q.b.example.com")],
        ),
    ]
    domains = ["www.a.example.com", "www.c.example.com"]
    out = plan(zones, domains)
    assert out["status"] == "no_safe_order"
    assert out["safe_prefix"] == ["b.example.com", "c.example.com"]
    replay_and_assert_steps(out, zones, domains)
    # Reaching the dead end: only A is left and it depends on the removed p.b.
    assert [b["zone"] for b in out["blocked_zones"]] == ["a.example.com"]
    assert out["blocked_zones"][0]["categories"] == [
        {"category": "missing_record", "label": "缺记录"}
    ]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_rejects_more_than_eight_zones() -> None:
    zones = [
        zone(f"z{i}.example.com", [rec(f"www.z{i}.example.com", "A", "10.0.0.1")])
        for i in range(9)
    ]
    with pytest.raises(ValidationError) as exc:
        PlanRequest.model_validate(payload(zones, ["www.z0.example.com"]))
    assert "too_long" in str(exc.value)


def test_rejects_nested_zones() -> None:
    zones = [zone("example.com", []), zone("sub.example.com", [])]
    with pytest.raises(ValidationError, match="non-nested"):
        PlanRequest.model_validate(payload(zones, ["x.example.com"]))


def test_rejects_record_outside_own_zone() -> None:
    zones = [zone("a.example.com", [rec("www.b.example.com", "A", "10.0.0.1")])]
    with pytest.raises(ValidationError, match="not owned by zone"):
        PlanRequest.model_validate(payload(zones, ["www.a.example.com"]))


def test_rejects_alias_and_address_at_same_name() -> None:
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "A", "10.0.0.1"),
             rec("www.a.example.com", "CNAME", "other.a.example.com")],
        )
    ]
    with pytest.raises(ValidationError, match="both A and CNAME"):
        PlanRequest.model_validate(payload(zones, ["www.a.example.com"]))


def test_rejects_cname_at_apex() -> None:
    zones = [zone("a.example.com", [rec("a.example.com", "CNAME", "b.example.com")])]
    with pytest.raises(ValidationError, match="apex"):
        PlanRequest.model_validate(payload(zones, ["x.a.example.com"]))


def test_rejects_cname_with_descendants() -> None:
    zones = [
        zone(
            "a.example.com",
            [rec("alias.a.example.com", "CNAME", "www.a.example.com"),
             rec("deep.alias.a.example.com", "A", "10.0.0.1")],
        )
    ]
    with pytest.raises(ValidationError, match="names below"):
        PlanRequest.model_validate(payload(zones, ["alias.a.example.com"]))


def test_rejects_non_ipv4_a_record() -> None:
    zones = [zone("a.example.com", [rec("www.a.example.com", "A", "not-an-ip")])]
    with pytest.raises(ValidationError, match="IPv4"):
        PlanRequest.model_validate(payload(zones, ["www.a.example.com"]))


def test_rejects_ipv6_value_in_a_record() -> None:
    zones = [zone("a.example.com", [rec("www.a.example.com", "A", "2001:db8::1")])]
    with pytest.raises(ValidationError, match="IPv4"):
        PlanRequest.model_validate(payload(zones, ["www.a.example.com"]))


def test_rejects_conflicting_cname_targets() -> None:
    zones = [
        zone(
            "a.example.com",
            [rec("www.a.example.com", "CNAME", "x.a.example.com"),
             rec("www.a.example.com", "CNAME", "y.a.example.com")],
        )
    ]
    with pytest.raises(ValidationError, match="conflicting CNAME"):
        PlanRequest.model_validate(payload(zones, ["www.a.example.com"]))


def test_rejects_required_domain_outside_submitted_zones() -> None:
    zones = [zone("a.example.com", [rec("www.a.example.com", "A", "10.0.0.1")])]
    with pytest.raises(ValidationError, match="not inside any submitted zone"):
        PlanRequest.model_validate(payload(zones, ["www.example.org"]))


def test_rejects_endless_record_chain() -> None:
    zones = [
        zone(
            "a.example.com",
            [rec(f"n{i}.a.example.com", "CNAME", f"n{i + 1}.a.example.com")
             for i in range(40)]
            + [rec("n40.a.example.com", "A", "10.0.0.1")],
        )
    ]
    out = plan(zones, ["n0.a.example.com"])
    # The plan is still produced; the chain simply cannot be resolved.
    assert out["status"] == "already_broken"
    assert out["fault_chains"][0]["category"] == "chain_too_long"


def test_accepts_trailing_dots_and_mixed_case() -> None:
    zones = [
        zone("A.Example.COM.", [rec("WWW.a.example.com.", "CNAME", "Svc.B.example.com.")],
             [rec("WWW.a.example.com.", "A", "10.0.0.9")]),
        zone("b.example.com", [rec("svc.b.example.com", "A", "10.0.0.1")], []),
    ]
    out = plan(zones, ["WWW.A.example.com."])
    assert out["status"] == "safe_order_found"


def test_accepts_lowercase_record_types_and_apex_names() -> None:
    zones = [
        zone(
            "a.example.com",
            [rec("a.example.com", "a", "10.0.0.1"),
             rec("alias.a.example.com", "cname", "a.example.com")],
            [rec("a.example.com", "A", "10.0.0.2"),
             rec("alias.a.example.com", "CNAME", "a.example.com")],
        )
    ]
    out = plan(zones, ["a.example.com", "alias.a.example.com"])
    assert out["status"] == "safe_order_found"
    assert out["steps"][0]["addresses_after"] == {
        "a.example.com": ["10.0.0.2"],
        "alias.a.example.com": ["10.0.0.2"],
    }


def test_same_zone_flip_dropping_a_target_is_blocked() -> None:
    """A single zone that removes a name still needed by its own alias has no safe order."""
    zones = [
        zone(
            "a.example.com",
            [rec("x.a.example.com", "CNAME", "y.a.example.com"),
             rec("y.a.example.com", "A", "10.0.0.1")],
            [rec("x.a.example.com", "A", "10.0.0.9")],
        )
    ]
    out = plan(zones, ["y.a.example.com"])
    assert out["status"] == "no_safe_order"
    assert out["safe_prefix"] == []
    assert out["blocked_zones"][0]["zone"] == "a.example.com"
    assert out["blocked_zones"][0]["failures"][0]["category"] == "missing_record"


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def test_healthz() -> None:
    assert client.get("/healthz").json()["status"] == "ok"


def test_plan_endpoint_round_trip() -> None:
    zones = [
        zone("a.example.com", [rec("www.a.example.com", "A", "10.0.0.1")],
             [rec("www.a.example.com", "A", "10.0.0.2")]),
    ]
    response = client.post("/plan", json=payload(zones, ["www.a.example.com"]))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "safe_order_found"
    assert body["order"] == ["a.example.com"]


def test_plan_endpoint_validation_error_is_422() -> None:
    zones = [zone("a.example.com", [rec("www.a.example.com", "A", "bad")])]
    response = client.post("/plan", json=payload(zones, ["www.a.example.com"]))
    assert response.status_code == 422


def test_openapi_documents_endpoint() -> None:
    spec = client.get("/openapi.json").json()
    assert "/plan" in spec["paths"]
    assert spec["info"]["title"].startswith("跨区 DNS")
