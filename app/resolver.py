"""Cache-less authoritative resolution over the submitted zones.

A "state" is the set of zone indexes already cut over to their new records.
Every lookup reflects exactly that state: no TTLs, no negative caching, no
partial zone contents.  Each unresolved chain is classified so that a duty
operator can tell a missing record apart from a CNAME cycle:

* ``missing_record`` - the chain reaches a name that has no A/CNAME RRset in
  its authoritative zone (``NODATA`` if the name exists, ``NXDOMAIN`` if it
  does not);
* ``cname_loop`` - the chain revisits a name;
* ``external_target`` - the chain leaves the submitted zones, so we hold no
  authoritative data for it;
* ``chain_too_long`` - more than ``MAX_CNAME_HOPS`` aliases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .models import Record, Zone, zone_covering

MAX_CNAME_HOPS = 32

Outcome = Literal["cname", "a", "nodata", "nxdomain", "external", "loop", "too_long"]
FailureCategory = Literal[
    "missing_record", "cname_loop", "external_target", "chain_too_long"
]


@dataclass
class TraceEntry:
    name: str
    zone: str | None
    outcome: Outcome
    target: str | None = None
    addresses: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    domain: str
    addresses: list[str]
    trace: list[TraceEntry]
    category: FailureCategory | None = None
    detail: str | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.category is None

    def chain_text(self) -> str:
        parts = [self.domain]
        for entry in self.trace:
            if entry.outcome == "cname":
                parts.append(f" --CNAME--> {entry.target}")
            elif entry.outcome == "loop":
                parts.append(f" => 回到 {entry.name}，成环")
            elif entry.outcome == "nodata":
                parts.append(" => 无记录（NODATA）")
            elif entry.outcome == "nxdomain":
                parts.append(" => 无记录（NXDOMAIN）")
            elif entry.outcome == "external":
                parts.append(" => 不在此次发布的权威区内，无数据")
            elif entry.outcome == "too_long":
                parts.append(" => CNAME 链过长")
            elif entry.outcome == "a":
                parts.append(" => A " + ", ".join(entry.addresses))
        return "".join(parts)


def _record_map(records: list[Record]) -> dict[str, list[Record]]:
    out: dict[str, list[Record]] = {}
    for rec in records:
        out.setdefault(rec.name, []).append(rec)
    return out


# --------------------------------------------------------------------------- #
# Failure constructors shared with the cache simulator
#
# They append the final trace entry and build the same category/detail/message
# that the planner reports, so both APIs describe a broken chain identically.
# --------------------------------------------------------------------------- #


def loop_resolution(
    domain: str, name: str, zone: str | None, trace: list[TraceEntry]
) -> Resolution:
    trace.append(TraceEntry(name=name, zone=zone, outcome="loop"))
    return Resolution(
        domain=domain,
        addresses=[],
        trace=trace,
        category="cname_loop",
        message=f"{domain} 的 CNAME 链成环，无法解析出 IPv4 地址",
    )


def external_resolution(
    domain: str, name: str, trace: list[TraceEntry]
) -> Resolution:
    trace.append(TraceEntry(name=name, zone=None, outcome="external"))
    return Resolution(
        domain=domain,
        addresses=[],
        trace=trace,
        category="external_target",
        message=(
            f"{domain} 的解析链到达 {name}，该名称不在任何已提交的"
            "权威区内，无法确认其 IPv4 地址"
        ),
    )


def missing_resolution(
    domain: str,
    name: str,
    zone_name: str,
    exists: bool,
    trace: list[TraceEntry],
) -> Resolution:
    outcome: Outcome = "nodata" if exists else "nxdomain"
    trace.append(TraceEntry(name=name, zone=zone_name, outcome=outcome))
    reason = (
        "该名称存在但没有 A/CNAME 记录（NODATA）"
        if exists
        else "该名称不存在（NXDOMAIN）"
    )
    return Resolution(
        domain=domain,
        addresses=[],
        trace=trace,
        category="missing_record",
        detail=outcome,
        message=f"{domain} 的解析链在 {name} 断开：区 {zone_name} 中{reason}",
    )


def too_long_resolution(
    domain: str, name: str, trace: list[TraceEntry]
) -> Resolution:
    trace.append(TraceEntry(name=name, zone=None, outcome="too_long"))
    return Resolution(
        domain=domain,
        addresses=[],
        trace=trace,
        category="chain_too_long",
        message=f"{domain} 的 CNAME 链超过 {MAX_CNAME_HOPS} 跳，视为不可解析",
    )


class Resolver:
    def __init__(self, zones: list[Zone]) -> None:
        self.zones = zones
        self.zone_names = [z.name for z in zones]
        self._old = [_record_map(z.records) for z in zones]
        self._new = [_record_map(z.new_records) for z in zones]

    def _zone_data(self, idx: int, released: frozenset[int]) -> dict[str, list[Record]]:
        return self._new[idx] if idx in released else self._old[idx]

    def _lookup(
        self, name: str, released: frozenset[int]
    ) -> tuple[str | None, list[Record], bool]:
        """Return (zone, records at name, whether name exists at all)."""
        owner = zone_covering(name, self.zone_names)
        if owner is None:
            return None, [], False
        zone_name, idx = owner
        data = self._zone_data(idx, released)
        records = data.get(name, [])
        if name == zone_name:
            # The apex always exists (SOA/NS), even without an A/CNAME RRset.
            return zone_name, records, True
        # Empty non-terminal: some deeper name exists below this one.
        suffix = "." + name
        exists = any(owner_name.endswith(suffix) for owner_name in data)
        return zone_name, records, exists

    def lookup(
        self, name: str, released: frozenset[int] = frozenset()
    ) -> tuple[str | None, list[Record], bool]:
        """Public form of :meth:`_lookup`, used by the cache simulator."""
        return self._lookup(name, released)

    def resolve(self, domain: str, released: frozenset[int]) -> Resolution:
        trace: list[TraceEntry] = []
        visited: set[str] = set()
        name = domain
        for _ in range(MAX_CNAME_HOPS + 1):
            if name in visited:
                loop_zone, _recs, _exists = self._lookup(name, released)
                return loop_resolution(domain, name, loop_zone, trace)
            visited.add(name)

            zone_name, records, exists = self._lookup(name, released)
            if zone_name is None:
                return external_resolution(domain, name, trace)
            if not records:
                return missing_resolution(domain, name, zone_name, exists, trace)

            cname = next((r for r in records if r.type == "CNAME"), None)
            if cname is not None:
                trace.append(
                    TraceEntry(
                        name=name, zone=zone_name, outcome="cname", target=cname.value
                    )
                )
                name = cname.value
                continue

            addresses = sorted({r.value for r in records if r.type == "A"})
            trace.append(
                TraceEntry(name=name, zone=zone_name, outcome="a", addresses=addresses)
            )
            return Resolution(domain=domain, addresses=addresses, trace=trace)

        return too_long_resolution(domain, name, trace)

    def resolve_all(
        self, domains: list[str], released: frozenset[int]
    ) -> list[Resolution]:
        return [self.resolve(d, released) for d in domains]
