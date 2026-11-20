"""Tests for the cold-cache restart simulator (``POST /simulate``).

Every scenario is written out by hand so the expected upstream query counts and
timings follow from the stated rules, not from the implementation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models import SimulateRequest, Zone
from app.resolver import Resolver
from app.service import build_simulation

client = TestClient(app)


def rec(name: str, rtype: str, value: str, ttl: int = 30) -> dict:
    return {"name": name, "type": rtype, "value": value, "ttl": ttl}


def a(name: str, value: str, ttl: int = 30) -> dict:
    return rec(name, "A", value, ttl)


def cname(name: str, value: str, ttl: int = 30) -> dict:
    return rec(name, "CNAME", value, ttl)


def zone(name: str, records: list[dict], latency_ms: int = 10) -> dict:
    return {
        "name": name,
        "records": records,
        "new_records": records,
        "latency_ms": latency_ms,
    }


def simulate(zones: list[dict], queries: list[tuple[int, str]]) -> dict:
    request = SimulateRequest.model_validate(
        {
            "zones": zones,
            "queries": [{"name": n, "at_ms": t} for t, n in queries],
        }
    )
    return build_simulation(request)


def zone_by_name(out: dict, name: str) -> dict:
    return next(z for z in out["zones"] if z["zone"] == name)


# --------------------------------------------------------------------------- #
# Timing and cache semantics
# --------------------------------------------------------------------------- #


def test_single_query_one_upstream_round_trip() -> None:
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1")], 10)],
        [(0, "www.a.example.com")],
    )
    result = out["results"][0]
    assert result["resolved"] is True
    assert result["addresses"] == ["10.0.0.1"]
    # Sent at 0, reply at 10, query completes when the reply arrives.
    assert result["completion_ms"] == 10
    assert out["total_upstream_queries"] == 1
    assert zone_by_name(out, "a.example.com")["upstream_queries"] == 1


def test_cache_hit_after_reply_does_not_go_upstream() -> None:
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 300)], 10)],
        [(0, "www.a.example.com"), (10, "www.a.example.com")],
    )
    first, second = out["results"]
    assert first["upstream_sent"] == 1
    assert second["upstream_sent"] == 0
    assert second["cache_hits"] == 1
    assert second["completion_ms"] == 10  # answered instantly from cache
    assert out["total_upstream_queries"] == 1


def test_reply_is_visible_to_lookup_in_same_millisecond() -> None:
    """"先处理回包，再处理新查找": a lookup at reply time uses the cache."""
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 300)], 10)],
        [(0, "www.a.example.com"), (10, "www.a.example.com")],
    )
    assert out["results"][1]["cache_hits"] == 1
    assert out["results"][1]["completion_ms"] == 10
    assert out["total_upstream_queries"] == 1


def test_lookup_one_ms_before_reply_is_coalesced() -> None:
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 300)], 10)],
        [(0, "www.a.example.com"), (9, "www.a.example.com")],
    )
    second = out["results"][1]
    assert second["coalesced_waits"] == 1
    assert second["upstream_sent"] == 0
    assert second["completion_ms"] == 10  # served by the first reply
    assert out["total_upstream_queries"] == 1


def test_ttl_counts_from_reply_receipt_and_expires_exactly() -> None:
    """TTL 50, reply at 10 -> entry valid until 60; a lookup at 60 misses."""
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 50)], 10)],
        [
            (0, "www.a.example.com"),
            (59, "www.a.example.com"),  # still cached
            (60, "www.a.example.com"),  # expired at exactly 60
        ],
    )
    first, cached, expired = out["results"]
    assert first["upstream_sent"] == 1
    assert cached["cache_hits"] == 1 and cached["upstream_sent"] == 0
    assert expired["cache_hits"] == 0 and expired["upstream_sent"] == 1
    assert expired["completion_ms"] == 70
    assert out["total_upstream_queries"] == 2


def test_shortest_ttl_in_rrset_wins() -> None:
    out = simulate(
        [
            zone(
                "a.example.com",
                [
                    a("www.a.example.com", "10.0.0.1", 100),
                    a("www.a.example.com", "10.0.0.2", 20),
                ],
                10,
            )
        ],
        [(0, "www.a.example.com"), (30, "www.a.example.com")],
    )
    assert out["results"][0]["addresses"] == ["10.0.0.1", "10.0.0.2"]
    # Cached at 10 with TTL 20 -> expires at 30, so the 30ms lookup misses.
    assert out["results"][1]["upstream_sent"] == 1
    assert out["total_upstream_queries"] == 2


def test_cname_and_a_use_their_own_ttls() -> None:
    zones = [
        zone("a.example.com", [cname("www.a.example.com", "svc.b.example.com", 5)], 10),
        zone("b.example.com", [a("svc.b.example.com", "10.0.1.1", 100)], 10),
    ]
    out = simulate(
        zones,
        [(0, "www.a.example.com"), (40, "www.a.example.com")],
    )
    # CNAME expired at 15 + 10 = 25, so the second query refetches only the
    # alias; the A record is still fresh.
    second = out["results"][1]
    assert second["upstream_sent"] == 1
    assert second["cache_hits"] == 1
    assert zone_by_name(out, "a.example.com")["upstream_queries"] == 2
    assert zone_by_name(out, "b.example.com")["upstream_queries"] == 1


def test_missing_record_answer_is_not_cached() -> None:
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1")], 10)],
        [(0, "gone.a.example.com"), (100, "gone.a.example.com")],
    )
    assert out["results"][0]["category"] == "missing_record"
    assert out["results"][1]["category"] == "missing_record"
    assert out["results"][1]["upstream_sent"] == 1
    assert out["total_upstream_queries"] == 2


def test_nodata_and_nxdomain_are_both_asked_each_time() -> None:
    zones = [
        zone(
            "a.example.com",
            [
                a("deep.empty.a.example.com", "10.0.0.1"),
            ],
            5,
        )
    ]
    out = simulate(
        zones,
        [(0, "empty.a.example.com"), (0, "nx.a.example.com")],
    )
    by_domain = {r["domain"]: r for r in out["results"]}
    assert by_domain["empty.a.example.com"]["detail"] == "nodata"
    assert by_domain["nx.a.example.com"]["detail"] == "nxdomain"
    assert out["total_upstream_queries"] == 2


def test_ttl_zero_is_reported_but_never_served_from_cache() -> None:
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 0)], 10)],
        [(0, "www.a.example.com"), (20, "www.a.example.com")],
    )
    assert out["results"][1]["cache_hits"] == 0
    assert out["total_upstream_queries"] == 2


# --------------------------------------------------------------------------- #
# CNAME chains, coalescing, independent continuation
# --------------------------------------------------------------------------- #


def test_cname_chain_reports_final_address_and_intermediate_upstreams() -> None:
    zones = [
        zone("a.example.com", [cname("www.a.example.com", "mid.b.example.com", 60)], 15),
        zone("b.example.com", [cname("mid.b.example.com", "end.c.example.com", 60)], 25),
        zone("c.example.com", [a("end.c.example.com", "10.0.0.9", 60)], 5),
    ]
    out = simulate(zones, [(0, "www.a.example.com")])
    result = out["results"][0]
    assert result["addresses"] == ["10.0.0.9"]
    # 15 (a) + 25 (b) + 5 (c) = 45 ms end to end.
    assert result["completion_ms"] == 45
    assert [t["outcome"] for t in result["trace"]] == ["cname", "cname", "a"]
    assert result["chain"] == (
        "www.a.example.com --CNAME--> mid.b.example.com"
        " --CNAME--> end.c.example.com => A 10.0.0.9"
    )
    assert out["total_upstream_queries"] == 3


def test_each_query_continues_independently_after_shared_reply() -> None:
    """Both queries coalesce on the alias, then each asks the target itself."""
    zones = [
        zone("a.example.com", [cname("www.a.example.com", "svc.b.example.com", 300)], 10),
        zone("b.example.com", [a("svc.b.example.com", "10.0.1.1", 300)], 20),
    ]
    out = simulate(
        zones,
        [(0, "www.a.example.com"), (5, "www.a.example.com")],
    )
    first, second = out["results"]
    # Both received the alias reply at 10 and continued at 10; the second one
    # found the target's in-flight request and waited for it.
    assert second["completion_ms"] == 30
    assert second["coalesced_waits"] == 2  # once on the alias, once on the A
    assert second["upstream_sent"] == 0
    assert first["completion_ms"] == 30
    assert zone_by_name(out, "a.example.com")["upstream_queries"] == 1
    assert zone_by_name(out, "b.example.com")["upstream_queries"] == 1
    assert out["total_upstream_queries"] == 2


def test_separate_queries_to_same_target_share_one_upstream() -> None:
    zones = [
        zone("a.example.com", [cname("one.a.example.com", "svc.b.example.com", 300)], 10),
        zone("a2.example.com", [cname("two.a2.example.com", "svc.b.example.com", 300)], 10),
        zone("b.example.com", [a("svc.b.example.com", "10.0.1.1", 300)], 30),
    ]
    out = simulate(
        zones,
        [(0, "one.a.example.com"), (1, "two.a2.example.com")],
    )
    assert zone_by_name(out, "b.example.com")["upstream_queries"] == 1
    assert all(r["resolved"] for r in out["results"])


# --------------------------------------------------------------------------- #
# Per-zone, per-second statistics
# --------------------------------------------------------------------------- #


def test_per_second_counts_and_earliest_peak() -> None:
    """Zone a's peak appears in second 0 first, then ties in second 2."""
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 0)], 10)],
        [
            (0, "www.a.example.com"),
            (500, "www.a.example.com"),
            (2000, "www.a.example.com"),
            (2100, "www.a.example.com"),
            (3000, "www.a.example.com"),
        ],
    )
    stats = zone_by_name(out, "a.example.com")
    assert stats["seconds"] == [
        {"second": 0, "queries": 2},
        {"second": 2, "queries": 2},
        {"second": 3, "queries": 1},
    ]
    assert stats["peak"] == {"second": 0, "queries": 2}
    assert stats["upstream_queries"] == 5


def test_zone_never_queried_reports_null_peak_and_no_seconds() -> None:
    out = simulate(
        [
            zone("a.example.com", [a("www.a.example.com", "10.0.0.1")], 10),
            zone("b.example.com", [a("www.b.example.com", "10.0.0.2")], 10),
        ],
        [(0, "www.a.example.com")],
    )
    unused = zone_by_name(out, "b.example.com")
    assert unused["seconds"] == []
    assert unused["peak"] == {"second": None, "queries": 0}
    assert unused["upstream_queries"] == 0


def test_counting_uses_send_time_not_arrival_time() -> None:
    """Latency 1500ms: sent at 900ms lands in second 0 on the wire."""
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 0)], 1500)],
        [(900, "www.a.example.com")],
    )
    stats = zone_by_name(out, "a.example.com")
    assert stats["seconds"] == [{"second": 0, "queries": 1}]
    assert out["results"][0]["completion_ms"] == 2400


def test_latency_differs_per_zone() -> None:
    zones = [
        zone("a.example.com", [cname("www.a.example.com", "svc.b.example.com", 60)], 300),
        zone("b.example.com", [a("svc.b.example.com", "10.0.1.1", 60)], 100),
    ]
    out = simulate(zones, [(0, "www.a.example.com")])
    assert out["results"][0]["completion_ms"] == 400


# --------------------------------------------------------------------------- #
# Failures follow /plan's rules
# --------------------------------------------------------------------------- #


def test_failure_categories_match_plan_resolver() -> None:
    records = [
        cname("one.a.example.com", "gone.a.example.com", 30),
        cname("two.a.example.com", "outside.example.net", 30),
        cname("loop1.a.example.com", "loop2.a.example.com", 30),
        cname("loop2.a.example.com", "loop1.a.example.com", 30),
        a("deep.nodata.a.example.com", "10.0.0.1", 30),
    ]
    zones = [zone("a.example.com", records, 5)]
    names = [
        "one.a.example.com",
        "two.a.example.com",
        "loop1.a.example.com",
        "nodata.a.example.com",
    ]
    out = simulate(zones, [(0, n) for n in names])
    categories = {r["domain"]: r["category"] for r in out["results"]}
    assert categories == {
        "one.a.example.com": "missing_record",
        "two.a.example.com": "external_target",
        "loop1.a.example.com": "cname_loop",
        "nodata.a.example.com": "missing_record",
    }
    # Cross-check against the planner's cache-less resolver.
    resolver = Resolver([Zone.model_validate(z) for z in zones])
    for name in names:
        expected = resolver.resolve(name, frozenset())
        actual = next(r for r in out["results"] if r["domain"] == name)
        assert actual["category"] == expected.category
        assert actual["detail"] == expected.detail
        assert actual["chain"] == expected.chain_text()


def test_chain_too_long_matches_plan_resolver() -> None:
    records = [
        cname(f"n{i}.a.example.com", f"n{i + 1}.a.example.com", 30)
        for i in range(40)
    ] + [a("n40.a.example.com", "10.0.0.1", 30)]
    zones = [zone("a.example.com", records, 1)]
    out = simulate(zones, [(0, "n0.a.example.com")])
    result = out["results"][0]
    resolver = Resolver([Zone.model_validate(z) for z in zones])
    expected = resolver.resolve("n0.a.example.com", frozenset())
    assert result["category"] == "chain_too_long" == expected.category
    assert result["chain"] == expected.chain_text()
    assert [t["outcome"] for t in result["trace"]] == [
        t.outcome for t in expected.trace
    ]


def test_repeat_at_hop_limit_is_too_long_not_loop() -> None:
    """/plan examines 33 names and never classifies the 34th as a loop."""
    records = [
        cname(f"n{i}.a.example.com", f"n{i + 1}.a.example.com", 30)
        for i in range(35)
    ]
    records[33] = cname("n33.a.example.com", "n1.a.example.com", 30)
    zones = [zone("a.example.com", records, 1)]
    resolver = Resolver([Zone.model_validate(z) for z in zones])
    expected = resolver.resolve("n0.a.example.com", frozenset())
    out = simulate(zones, [(0, "n0.a.example.com")])
    assert expected.category == "chain_too_long"
    assert out["results"][0]["category"] == expected.category
    assert out["results"][0]["chain"] == expected.chain_text()


def test_random_chains_match_plan_categories() -> None:
    """For fixed zone data the simulated failure must equal /plan's exactly."""
    import random

    rng = random.Random(20241120)
    compared = 0
    for _ in range(120):
        records: list[dict] = []
        names = [f"n{i}.a.example.com" for i in range(8)]
        roll = rng.random()
        if roll < 0.3:
            # Long chain ending in a repeat of an early name.
            length = rng.randint(34, 40)
            chain = [f"c{i}.a.example.com" for i in range(length)]
            for i in range(length - 1):
                records.append(cname(chain[i], chain[i + 1], 30))
            records.append(cname(chain[-1], chain[rng.randrange(0, 5)], 30))
        else:
            for i, name in enumerate(names):
                r = rng.random()
                if r < 0.35:
                    records.append(a(name, f"10.0.0.{i + 1}", 30))
                elif r < 0.65:
                    records.append(cname(name, rng.choice(names + ["gone.a.example.com"]), 30))
                elif r < 0.85:
                    records.append(cname(name, "outside.example.net", 30))
                # else: name has no record -> NXDOMAIN / NODATA
            records.append(a("deep.empty.a.example.com", "10.0.0.99", 30))
        zones = [zone("a.example.com", records, 1)]
        resolver = Resolver([Zone.model_validate(z) for z in zones])
        domains = [f"n{i}.a.example.com" for i in range(8)]
        domains += ["empty.a.example.com"]
        if records and records[0]["name"].startswith("c"):
            domains = ["c0.a.example.com"]
        expected = [resolver.resolve(d, frozenset()) for d in domains]
        out = simulate(zones, [(0, d) for d in domains])
        for result, res in zip(out["results"], expected):
            assert result["resolved"] == res.ok
            assert result["category"] == res.category
            assert result["detail"] == res.detail
            assert result["chain"] == res.chain_text()
        compared += 1
    assert compared >= 80


def test_query_outside_submitted_zones_is_external_target() -> None:
    out = simulate(
        [zone("a.example.com", [a("www.a.example.com", "10.0.0.1")], 5)],
        [(0, "www.elsewhere.example.org")],
    )
    result = out["results"][0]
    assert result["resolved"] is False
    assert result["category"] == "external_target"
    # No packet can be sent: we hold no authoritative data for that name.
    assert out["total_upstream_queries"] == 0


def test_failure_summary_counts_failed_queries() -> None:
    zones = [
        zone(
            "a.example.com",
            [
                a("ok.a.example.com", "10.0.0.1"),
                cname("bad.a.example.com", "gone.a.example.com"),
            ],
        )
    ]
    out = simulate(
        zones,
        [(0, "ok.a.example.com"), (0, "bad.a.example.com"), (10, "bad.a.example.com")],
    )
    assert out["failure_summary"]["total"] == 2
    assert out["failure_summary"]["by_category_label"] == {"缺记录": 2}


def test_new_records_are_ignored() -> None:
    """The measuring window uses live ``records``, not the planned next state."""
    zones = [
        {
            "name": "a.example.com",
            "records": [a("www.a.example.com", "10.0.0.1", 300)],
            "new_records": [a("www.a.example.com", "10.0.0.2", 300)],
            "latency_ms": 10,
        }
    ]
    out = simulate(zones, [(0, "www.a.example.com")])
    assert out["results"][0]["addresses"] == ["10.0.0.1"]


# --------------------------------------------------------------------------- #
# Validation and HTTP
# --------------------------------------------------------------------------- #


def test_rejects_more_than_500_queries() -> None:
    zones = [zone("a.example.com", [a("www.a.example.com", "10.0.0.1")])]
    queries = [{"name": "www.a.example.com", "at_ms": i} for i in range(501)]
    with pytest.raises(ValidationError, match="too_long"):
        SimulateRequest.model_validate({"zones": zones, "queries": queries})


def test_accepts_exactly_500_queries() -> None:
    """500 queries one millisecond apart; TTL 30 means a refetch per expiry."""
    zones = [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 30)], 10)]
    queries = [{"name": "www.a.example.com", "at_ms": i} for i in range(500)]
    request = SimulateRequest.model_validate({"zones": zones, "queries": queries})
    out = build_simulation(request)
    assert out["query_count"] == 500
    # Reply at t+10, TTL 30 -> cache lives 30..40 ms per fill; 13 fills cover
    # the whole 0..499 ms span (0, 40, 80, ..., 480).
    assert out["total_upstream_queries"] == 13
    assert out["results"][0]["cache_hits"] == 0
    assert out["results"][-1]["cache_hits"] == 1


def test_rejects_missing_or_non_positive_latency() -> None:
    bad_zones = [
        {"name": "a.example.com", "records": [], "latency_ms": 0},
    ]
    with pytest.raises(ValidationError):
        SimulateRequest.model_validate(
            {"zones": bad_zones, "queries": [{"name": "www.a.example.com", "at_ms": 0}]}
        )
    missing = [{"name": "a.example.com", "records": []}]
    with pytest.raises(ValidationError):
        SimulateRequest.model_validate(
            {"zones": missing, "queries": [{"name": "www.a.example.com", "at_ms": 0}]}
        )


def test_rejects_non_integer_latency_and_time() -> None:
    """Latency and query time must be integers, not floats or booleans."""
    with pytest.raises(ValidationError):
        SimulateRequest.model_validate(
            {
                "zones": [{"name": "a.example.com", "records": [], "latency_ms": 1.5}],
                "queries": [{"name": "www.a.example.com", "at_ms": 0}],
            }
        )
    with pytest.raises(ValidationError):
        SimulateRequest.model_validate(
            {
                "zones": [zone("a.example.com", [], 5)],
                "queries": [{"name": "www.a.example.com", "at_ms": 1.25}],
            }
        )


def test_rejects_negative_time_and_unknown_fields_are_still_validated() -> None:
    with pytest.raises(ValidationError):
        SimulateRequest.model_validate(
            {
                "zones": [zone("a.example.com", [])],
                "queries": [{"name": "www.a.example.com", "at_ms": -1}],
            }
        )


def test_rejects_nested_zones_like_plan() -> None:
    with pytest.raises(ValidationError, match="non-nested"):
        SimulateRequest.model_validate(
            {
                "zones": [zone("example.com", []), zone("sub.example.com", [])],
                "queries": [{"name": "www.example.com", "at_ms": 0}],
            }
        )


def test_rejects_invalid_zone_records_like_plan() -> None:
    bad = zone("a.example.com", [a("www.b.example.com", "10.0.0.1")])
    with pytest.raises(ValidationError, match="not owned by zone"):
        SimulateRequest.model_validate(
            {"zones": [bad], "queries": [{"name": "www.a.example.com", "at_ms": 0}]}
        )


def test_simulate_endpoint_round_trip() -> None:
    response = client.post(
        "/simulate",
        json={
            "zones": [zone("a.example.com", [a("www.a.example.com", "10.0.0.1")], 10)],
            "queries": [{"name": "www.a.example.com", "at_ms": 0}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "cold_cache_simulation"
    assert body["results"][0]["addresses"] == ["10.0.0.1"]
    assert body["results"][0]["completion_ms"] == 10


def test_simulate_endpoint_validation_error_is_422() -> None:
    response = client.post(
        "/simulate",
        json={
            "zones": [{"name": "a.example.com", "records": [], "latency_ms": 5}],
            "queries": [{"name": "www.a.example.com", "at_ms": 0}] * 501,
        },
    )
    assert response.status_code == 422


def test_openapi_documents_simulate_endpoint() -> None:
    spec = client.get("/openapi.json").json()
    assert "/simulate" in spec["paths"]
    assert "/plan" in spec["paths"]


# --------------------------------------------------------------------------- #
# Determinism and internal consistency
# --------------------------------------------------------------------------- #


def test_result_is_deterministic() -> None:
    zones = [
        zone("a.example.com", [cname("www.a.example.com", "svc.b.example.com", 20)], 7),
        zone("b.example.com", [a("svc.b.example.com", "10.0.1.1", 40)], 13),
    ]
    queries = [(t, "www.a.example.com") for t in (0, 1, 5, 30, 31, 100)]
    first = simulate(zones, queries)
    second = simulate(zones, queries)
    assert first == second


def test_queries_may_be_listed_out_of_order() -> None:
    zones = [zone("a.example.com", [a("www.a.example.com", "10.0.0.1", 300)], 10)]
    ordered = simulate(zones, [(0, "www.a.example.com"), (50, "www.a.example.com")])
    shuffled = simulate(zones, [(50, "www.a.example.com"), (0, "www.a.example.com")])
    # query_id follows input order; the timeline itself is unaffected.
    assert [r["completion_ms"] for r in sorted(
        shuffled["results"], key=lambda r: r["started_at_ms"]
    )] == [10, 50]
    assert shuffled["total_upstream_queries"] == ordered["total_upstream_queries"] == 1
    timing = {
        r["started_at_ms"]: r["completion_ms"] for r in shuffled["results"]
    }
    assert timing == {0: 10, 50: 50}


def test_500_queries_over_long_chains_finish_quickly() -> None:
    import time

    records = [
        cname(f"n{i}.a.example.com", f"n{i + 1}.a.example.com", 0)
        for i in range(30)
    ] + [a("n30.a.example.com", "10.0.0.1", 0)]
    zones = [zone("a.example.com", records, 2)]
    queries = [(i * 3, "n0.a.example.com") for i in range(500)]
    started = time.monotonic()
    out = simulate(zones, queries)
    elapsed = time.monotonic() - started
    assert out["query_count"] == 500
    assert all(r["resolved"] for r in out["results"])
    # TTL 0 means nothing stays cached, so every query sends every hop; with
    # 31 hops that is 15 500 upstream queries, and the run must stay cheap.
    assert out["total_upstream_queries"] == 500 * 31
    assert elapsed < 5


def test_per_query_upstream_sums_to_zone_totals() -> None:
    zones = [
        zone("a.example.com", [cname("www.a.example.com", "svc.b.example.com", 20)], 7),
        zone("b.example.com", [a("svc.b.example.com", "10.0.1.1", 40)], 13),
    ]
    out = simulate(zones, [(t, "www.a.example.com") for t in (0, 3, 25, 26, 80)])
    total = sum(r["upstream_sent"] for r in out["results"])
    assert total == out["total_upstream_queries"]
    assert total == sum(z["upstream_queries"] for z in out["zones"])
    seconds = {z["zone"]: sum(b["queries"] for b in z["seconds"]) for z in out["zones"]}
    assert seconds == {
        z["zone"]: z["upstream_queries"] for z in out["zones"]
    }
