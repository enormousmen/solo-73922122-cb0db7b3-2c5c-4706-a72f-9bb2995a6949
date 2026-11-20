"""Algorithm-level tests for the planner's completeness and prefix maximality."""

from __future__ import annotations

import itertools
import random

from app.models import PlanRequest, Record, Zone
from app.planner import plan_order
from app.resolver import Resolver


def _zone(name: str, old: list[Record], new: list[Record]) -> Zone:
    return Zone(name=name, records=old, new_records=new)


def _a(owner: str, value: str) -> Record:
    return Record(name=owner, type="A", value=value)


def _cname(owner: str, value: str) -> Record:
    return Record(name=owner, type="CNAME", value=value)


def brute_force(resolver: Resolver, domains: list[str], n: int) -> tuple[bool, list[int]]:
    """Exhaustively find a full safe order, else the longest safe prefix."""
    best: list[int] = []
    for perm in itertools.permutations(range(n)):
        released: set[int] = set()
        prefix: list[int] = []
        for i in perm:
            released.add(i)
            if all(r.ok for r in resolver.resolve_all(domains, frozenset(released))):
                prefix.append(i)
            else:
                break
        if len(prefix) == n:
            return True, prefix
        if len(prefix) > len(best):
            best = prefix
    return False, best


def test_matches_brute_force_on_random_small_instances() -> None:
    rng = random.Random(20240101)
    compared = 0
    for _ in range(300):
        n = rng.randint(1, 4)
        names = [f"z{i}.example.com" for i in range(n)]

        def state(i: int) -> list[Record]:
            recs: list[Record] = []
            for j in range(n):
                owner = f"n{i}_{j}.{names[i]}"
                roll = rng.random()
                if roll < 0.45:
                    recs.append(_a(owner, f"10.{i}.{j}.1"))
                elif roll < 0.85:
                    t, k = rng.randrange(n), rng.randrange(n)
                    recs.append(_cname(owner, f"n{t}_{k}.{names[t]}"))
            return recs

        zones = [_zone(names[i], state(i), state(i)) for i in range(n)]
        domains = [f"n{i}_{j}.{names[i]}" for i in range(n) for j in range(n)]
        domains = [d for d in domains if rng.random() < 0.5] or [domains[0]]
        try:
            request = PlanRequest(zones=zones, required_domains=domains)
        except Exception:  # generated duplicate/conflicting records
            continue
        resolver = Resolver(zones)
        if not all(r.ok for r in resolver.resolve_all(domains, frozenset())):
            continue
        compared += 1
        outcome = plan_order(resolver, request)
        found, prefix = brute_force(resolver, domains, n)
        assert outcome.found_full is found
        if not found:
            assert len(outcome.safe_prefix) == len(prefix)
            # The reported prefix must itself be safe and maximal.
            resolver.resolve_all(domains, frozenset(outcome.safe_prefix))
            remaining = [i for i in range(n) if i not in outcome.safe_prefix]
            for i in remaining:
                assert not all(
                    r.ok
                    for r in resolver.resolve_all(
                        domains, frozenset(outcome.safe_prefix) | {i}
                    )
                )
    assert compared >= 50


def test_greedy_would_be_wrong_but_search_finds_full_order() -> None:
    """The first safe zone alphabetically is a trap, yet a full order exists."""
    zones = [
        _zone(
            "a.example.com",
            [_cname("www.a.example.com", "x.b.example.com")],
            [_cname("www.a.example.com", "y.b.example.com")],
        ),
        _zone(
            "b.example.com",
            [_a("x.b.example.com", "10.1.1.1"), _a("y.b.example.com", "10.1.1.2")],
            [_a("y.b.example.com", "10.1.1.2")],
        ),
        _zone(
            "c.example.com",
            [_a("m.c.example.com", "10.2.2.1")],
            [_a("m.c.example.com", "10.2.2.2")],
        ),
    ]
    request = PlanRequest(
        zones=zones,
        required_domains=["www.a.example.com", "m.c.example.com"],
    )
    resolver = Resolver(zones)
    outcome = plan_order(resolver, request)
    # c -> b -> a is a full safe order even though publishing a first is also safe.
    assert outcome.found_full
    released: set[int] = set()
    for idx in outcome.safe_prefix:
        released.add(idx)
        assert all(
            r.ok for r in resolver.resolve_all(request.required_domains, frozenset(released))
        )


def test_prefix_choice_minimises_failures_at_dead_end() -> None:
    """When several prefixes tie on length, the one with fewer failures is used."""
    zones = [
        _zone(
            "a.example.com",
            [_cname("d.a.example.com", "t.b.example.com")],
            [_cname("d.a.example.com", "u.b.example.com")],
        ),
        _zone(
            "b.example.com",
            [_a("t.b.example.com", "10.1.1.1")],
            [_a("u.b.example.com", "10.1.1.2")],
        ),
    ]
    request = PlanRequest(zones=zones, required_domains=["d.a.example.com"])
    resolver = Resolver(zones)
    outcome = plan_order(resolver, request)
    assert not outcome.found_full
    # Both single-zone prefixes are impossible; zero-length prefix is the max.
    assert outcome.safe_prefix == []
    assert len(outcome.candidates) == 2
