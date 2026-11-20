"""Search for a safe ordering of whole-zone cutovers.

The problem is small (at most 8 zones) but the safety predicate is *not*
monotone.  Publishing a zone can both repair and break a required domain:

* a CNAME flip is unsafe until its new target zone has the new address;
* a zone that only removes an old address is safe only after its consumers
  have moved on;
* a broken state can be repaired by publishing a later zone.

A greedy "publish whatever is currently safe" loop is therefore not guaranteed
to find a safe order even when one exists, so we run a complete depth-first
search over states.  A state is the subset of zones already cut over, and it is
all the future depends on, so memoising states bounds the search at 2^n = 256
states for n <= 8, each evaluated in O(zones * domains * chain length).

For an unsatisfiable request the same search yields a maximal safe prefix: the
longest sequence of whole-zone cutovers after which *no* remaining zone can be
published without breaking a required domain.  Among equally long prefixes we
pick the one whose blocked state has the fewest failing domains, so the report
points at the smallest remaining problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import PlanRequest
from .resolver import Resolution, Resolver


@dataclass
class CandidateBlock:
    zone_index: int
    zone_name: str
    failures: list[Resolution]

    @property
    def categories(self) -> list[str]:
        out: list[str] = []
        for f in self.failures:
            if f.category and f.category not in out:
                out.append(f.category)
        return out


@dataclass
class PlanOutcome:
    found_full: bool
    safe_prefix: list[int] = field(default_factory=list)
    candidates: list[CandidateBlock] = field(default_factory=list)


def _failing(resolutions: list[Resolution]) -> list[Resolution]:
    return [r for r in resolutions if not r.ok]


def plan_order(resolver: Resolver, request: PlanRequest) -> PlanOutcome:
    n = len(request.zones)
    domains = request.required_domains
    full = frozenset(range(n))

    # dfs(released) -> (full order reachable?, longest safe suffix,
    #                   -fewest failures among blocked candidates at the end)
    # The suffix tuple is used only for reconstructing the prefix; the length
    # and the failure score drive the search.
    memo: dict[frozenset[int], tuple[bool, tuple[int, ...], int]] = {}

    def failing_count(released: frozenset[int]) -> int:
        return len(_failing(resolver.resolve_all(domains, released)))

    def dfs(released: frozenset[int]) -> tuple[bool, tuple[int, ...], int]:
        if released == full:
            return True, (), 0
        cached = memo.get(released)
        if cached is not None:
            return cached

        remaining = [i for i in range(n) if i not in released]
        publishable = [i for i in remaining if failing_count(released | {i}) == 0]

        if publishable:
            best: tuple[bool, tuple[int, ...], int] | None = None
            for i in publishable:
                found, suffix, neg_fail = dfs(released | {i})
                cand = (found, (i,) + suffix, neg_fail)
                if (
                    best is None
                    or found
                    or (not best[0] and len(cand[1]) > len(best[1]))
                    or (
                        not best[0]
                        and len(cand[1]) == len(best[1])
                        and cand[2] > best[2]
                    )
                ):
                    best = cand
                if found:
                    break
            assert best is not None
            # A publishable step always extends the prefix, so ``best`` wins
            # over stopping here.
            memo[released] = best
            return best

        # Dead end: every remaining zone breaks at least one required domain.
        worst = min(
            (failing_count(released | {i}) for i in remaining),
            default=0,
        )
        result = (False, (), -worst)
        memo[released] = result
        return result

    found, suffix, _ = dfs(frozenset())
    outcome = PlanOutcome(found_full=found, safe_prefix=list(suffix))
    if not found:
        released = frozenset(outcome.safe_prefix)
        for i in range(n):
            if i in released:
                continue
            outcome.candidates.append(
                CandidateBlock(
                    zone_index=i,
                    zone_name=request.zones[i].name,
                    failures=_failing(resolver.resolve_all(domains, released | {i})),
                )
            )
    return outcome
