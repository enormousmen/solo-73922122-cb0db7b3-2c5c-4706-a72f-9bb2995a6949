"""Discrete-event simulation of a cold recursive resolver after a restart.

A duty operator restarts an internal recursive resolver and wants to know how
much upstream query volume an empty cache will push at each authoritative zone.
This module replays a deterministic timeline for up to 500 A queries.

Time model
----------
Every zone has a fixed positive ``latency_ms``: an upstream query sent at time
``t`` gets its reply back at ``t + latency_ms`` (the value is the full round
trip).  Time is measured in integer milliseconds; all events are processed in
``(time, kind, sequence)`` order.

Ordering rule
-------------
At the same millisecond: cache expiry is applied first, then replies, then new
lookups.  In particular a reply is always available to a lookup that starts in
the same millisecond, as required ("先处理回包，再处理新查找").

Cache
-----
One cache is shared by all queries and starts empty.  It is keyed by record
name: the validated zone data can never hold both A and CNAME at one name, so
a name maps to exactly one RRset.  Only positive answers (A or CNAME) are
stored.  A "missing record" answer (NXDOMAIN or NODATA) is *not* cached, so the
next lookup asks the authority again.  A cache entry's TTL timer starts when
the reply is received and its TTL is the shortest TTL in the name's RRset;
at ``received_at + ttl`` the entry is dropped.  Zone data is fixed for the
whole run (there is no cutover in the measuring window), so cached values never
need refreshing while fresh.

In-flight coalescing
--------------------
A cache miss for a name that already has an upstream query in flight does not
send another packet.  Every waiting query is handed the same reply and then
continues with its own CNAME chain independently from the moment it received
that reply.

Statistics
----------
An upstream query is counted against the owner zone at its *send* time, whether
it was the first miss or a retry after expiry.  Each zone reports the distinct
seconds in which it received upstream queries with the number of queries sent
in each, plus the earliest second holding that zone's peak count.

Failures follow the planner's rules exactly (``missing_record`` with
``nodata``/``nxdomain`` detail, ``cname_loop``, ``external_target``,
``chain_too_long``), using the same helpers as :mod:`app.resolver`.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Literal, cast

from .models import Record, SimZone, Zone
from .resolver import (
    MAX_CNAME_HOPS,
    Resolution,
    Resolver,
    TraceEntry,
    external_resolution,
    loop_resolution,
    missing_resolution,
    too_long_resolution,
)

AnswerKind = Literal["a", "cname", "missing"]
EventKind = Literal["expiry", "reply", "query"]

# Expiry first: an entry whose timer ran out is gone before anything else at
# that instant.  Replies next, so lookups in the same millisecond see them.
_KIND_ORDER: dict[str, int] = {"expiry": 0, "reply": 1, "query": 2}


@dataclass(order=True)
class _Event:
    time: int
    kind_order: int
    seq: int
    kind: EventKind = field(compare=False)
    payload: object = field(compare=False)


@dataclass
class _Answer:
    kind: AnswerKind
    addresses: list[str] = field(default_factory=list)
    target: str | None = None
    exists: bool = False
    ttl: int = 0
    zone: str | None = None

    @property
    def cacheable(self) -> bool:
        return self.kind in ("a", "cname")


@dataclass
class _CacheEntry:
    kind: Literal["a", "cname"]
    addresses: list[str]
    target: str | None
    zone: str | None
    ttl: int
    stored_at: int
    expires_at: int


@dataclass
class _InFlight:
    name: str
    zone: str
    reply_at: int
    answer: _Answer
    waiters: list[int] = field(default_factory=list)


@dataclass
class _Query:
    qid: int
    domain: str
    started_at: int
    current: str
    visited: set[str] = field(default_factory=set)
    trace: list[TraceEntry] = field(default_factory=list)
    hops: int = 0
    done_at: int | None = None
    resolution: Resolution | None = None
    cache_hits: int = 0
    coalesced_waits: int = 0
    upstream_sent: int = 0

    def finish(self, resolution: Resolution, at: int) -> None:
        self.done_at = at
        self.resolution = resolution


@dataclass
class ZoneStats:
    upstream_queries: int = 0
    # second index (ms // 1000) -> queries sent to this zone in that second
    per_second: dict[int, int] = field(default_factory=dict)

    def record_send(self, at_ms: int) -> None:
        self.upstream_queries += 1
        second = at_ms // 1000
        self.per_second[second] = self.per_second.get(second, 0) + 1

    def peak(self) -> tuple[int | None, int]:
        """Earliest second with the highest count (None if never queried)."""
        if not self.per_second:
            return None, 0
        second, count = min(
            self.per_second.items(), key=lambda item: (-item[1], item[0])
        )
        return second, count


@dataclass
class QueryOutcome:
    query_id: int
    domain: str
    started_at: int
    completion_ms: int | None
    resolution: Resolution
    cache_hits: int
    coalesced_waits: int
    upstream_sent: int


@dataclass
class SimResult:
    outcomes: list[QueryOutcome]
    zone_stats: dict[str, ZoneStats]
    finish_ms: int


class CacheSimulator:
    """Replay a cold-cache query load against the submitted authoritative zones."""

    def __init__(self, zones: list[SimZone]) -> None:
        # Only the live ``records`` snapshot is authoritative during the run;
        # ``new_records`` is validated for /plan compatibility but unused.
        live = [
            Zone(name=z.name, records=z.records, new_records=z.records) for z in zones
        ]
        self.zones = zones
        self.resolver = Resolver(live)
        self.latency = {z.name: z.latency_ms for z in zones}
        self.cache: dict[str, _CacheEntry] = {}
        self.inflight: dict[str, _InFlight] = {}
        self.zone_stats = {z.name: ZoneStats() for z in zones}
        self._events: list[_Event] = []
        self._seq = 0
        self._queries: list[_Query] = []

    # -- event plumbing ---------------------------------------------------- #

    def _push(self, time: int, kind: EventKind, payload: object) -> None:
        self._seq += 1
        heapq.heappush(
            self._events, _Event(time, _KIND_ORDER[kind], self._seq, kind, payload)
        )

    # -- authority --------------------------------------------------------- #

    @staticmethod
    def _answer_for(zone: str, records: list[Record], exists: bool) -> _Answer:
        if records:
            cname = next((r for r in records if r.type == "CNAME"), None)
            if cname is not None:
                ttl = min(r.ttl for r in records if r.type == "CNAME")
                return _Answer(kind="cname", target=cname.value, ttl=ttl, zone=zone)
            addresses = sorted({r.value for r in records if r.type == "A"})
            ttl = min(r.ttl for r in records if r.type == "A")
            return _Answer(kind="a", addresses=addresses, ttl=ttl, zone=zone)
        # No A/CNAME RRset at this owner: NODATA if it exists (apex or an
        # empty non-terminal), NXDOMAIN otherwise.
        return _Answer(kind="missing", exists=exists, zone=zone)

    # -- query path -------------------------------------------------------- #

    def _hop(self, query: _Query, at: int) -> None:
        """Process the resolver's next hop for ``query`` at time ``at``."""
        name = query.current
        # /plan processes at most MAX_CNAME_HOPS aliases: after the 33rd hop it
        # reports "too long" *before* looking at the next name, so a repeat at
        # that point is chain_too_long rather than cname_loop.  Mirror that.
        if query.hops > MAX_CNAME_HOPS:
            query.finish(too_long_resolution(query.domain, name, query.trace), at)
            return
        if name in query.visited:
            zone, _records, _exists = self.resolver.lookup(name)
            query.finish(loop_resolution(query.domain, name, zone, query.trace), at)
            return
        query.visited.add(name)

        entry = self.cache.get(name)
        if entry is not None and entry.expires_at > at:
            query.cache_hits += 1
            self._advance_from_known(
                query,
                name,
                entry.kind,
                entry.addresses,
                entry.target,
                entry.zone,
                False,
                at,
            )
            return

        pending = self.inflight.get(name)
        if pending is not None:
            pending.waiters.append(query.qid)
            query.coalesced_waits += 1
            return

        zone, records, exists = self.resolver.lookup(name)
        if zone is None:
            # Same rule as /plan: leaving the submitted zones is unverifiable.
            query.finish(external_resolution(query.domain, name, query.trace), at)
            return

        # Even a NODATA/NXDOMAIN answer costs one upstream query: a cold
        # resolver cannot know the name is missing without asking.  The answer
        # simply is not cached, so the next lookup asks again.
        answer = self._answer_for(zone, records, exists)
        latency = self.latency[zone]
        flight = _InFlight(
            name=name,
            zone=zone,
            reply_at=at + latency,
            answer=answer,
            waiters=[query.qid],
        )
        self.inflight[name] = flight
        query.upstream_sent += 1
        self.zone_stats[zone].record_send(at)
        self._push(flight.reply_at, "reply", flight)

    def _advance_from_known(
        self,
        query: _Query,
        name: str,
        kind: AnswerKind,
        addresses: list[str],
        target: str | None,
        zone: str | None,
        exists: bool,
        at: int,
    ) -> None:
        """Apply an answer to one waiting query."""
        if kind == "cname":
            query.trace.append(
                TraceEntry(name=name, zone=zone, outcome="cname", target=target)
            )
            query.current = target or name
            query.hops += 1
            self._push(at, "query", query)
        elif kind == "a":
            query.trace.append(
                TraceEntry(name=name, zone=zone, outcome="a", addresses=addresses)
            )
            query.finish(
                Resolution(domain=query.domain, addresses=addresses, trace=query.trace),
                at,
            )
        elif kind == "missing":
            query.finish(
                missing_resolution(query.domain, name, zone or "", exists, query.trace),
                at,
            )
        else:  # pragma: no cover - no other kinds are constructed
            query.finish(external_resolution(query.domain, name, query.trace), at)

    # -- event handlers ---------------------------------------------------- #

    def _on_reply(self, flight: _InFlight, at: int) -> None:
        name = flight.name
        if self.inflight.get(name) is flight:
            del self.inflight[name]

        answer = flight.answer
        if answer.cacheable and answer.ttl > 0:
            entry = _CacheEntry(
                kind=answer.kind,  # type: ignore[arg-type]
                addresses=answer.addresses,
                target=answer.target,
                zone=answer.zone,
                ttl=answer.ttl,
                stored_at=at,
                expires_at=at + answer.ttl,
            )
            self.cache[name] = entry
            self._push(entry.expires_at, "expiry", (name, entry))

        # Every waiter continues from *its own* receipt time, at time ``at``.
        for qid in flight.waiters:
            query = self._queries[qid]
            self._advance_from_known(
                query,
                name,
                answer.kind,
                answer.addresses,
                answer.target,
                answer.zone,
                answer.exists,
                at,
            )

    def _on_expiry(self, name: str, entry: _CacheEntry) -> None:
        # Identity check: a newer reply may already have replaced this entry.
        if self.cache.get(name) is entry:
            del self.cache[name]

    # -- driver ------------------------------------------------------------ #

    def run(self, queries: list[tuple[int, str]]) -> SimResult:
        """Run ``(start_ms, name)`` queries to completion.

        All run state is reset here, so a simulator instance can be reused for
        a second independent timeline without leaking cache entries, in-flight
        requests, statistics, or query results.
        """
        self.cache.clear()
        self.inflight.clear()
        self.zone_stats = {name: ZoneStats() for name in self.zone_stats}
        self._events.clear()
        self._seq = 0
        self._queries.clear()

        for qid, (start_ms, name) in enumerate(queries):
            query = _Query(qid=qid, domain=name, started_at=start_ms, current=name)
            self._queries.append(query)
            self._push(start_ms, "query", query)

        while self._events:
            event = heapq.heappop(self._events)
            if event.kind == "expiry":
                name, entry = cast(tuple[str, _CacheEntry], event.payload)
                self._on_expiry(name, entry)
            elif event.kind == "reply":
                self._on_reply(cast(_InFlight, event.payload), event.time)
            else:
                self._hop(cast(_Query, event.payload), event.time)

        outcomes: list[QueryOutcome] = []
        for query in self._queries:
            assert query.done_at is not None and query.resolution is not None
            outcomes.append(
                QueryOutcome(
                    query_id=query.qid,
                    domain=query.domain,
                    started_at=query.started_at,
                    completion_ms=query.done_at,
                    resolution=query.resolution,
                    cache_hits=query.cache_hits,
                    coalesced_waits=query.coalesced_waits,
                    upstream_sent=query.upstream_sent,
                )
            )
        finish = max(outcome.completion_ms or 0 for outcome in outcomes)
        return SimResult(
            outcomes=outcomes,
            zone_stats=self.zone_stats,
            finish_ms=finish,
        )
