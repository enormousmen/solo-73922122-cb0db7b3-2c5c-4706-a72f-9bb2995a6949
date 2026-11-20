"""Request models and validation for the release planner.

The service plans an ordering of whole-zone cutovers.  In every state DNS is
evaluated as an authoritative, cache-less resolver would see it: each zone is
either entirely on its old data or entirely on its new data.  There is no
partial propagation inside a zone and no TTL cache that could mask a broken
intermediate state.

Only A and CNAME records are supported.  CNAME chains are followed across the
submitted zones.  A chain that leaves the submitted zones is *not* assumed to
work: we hold no authoritative data for it, so it is reported as an
unresolvable target (add the external zone as an unchanged zone, i.e. with
``records == new_records``, to model it explicitly).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Conservative DNS name check: 1-63 char labels, dot separators, optional
# trailing root dot.  Wildcards and escape sequences are rejected because the
# planner reasons about concrete CNAME chains and cannot give a sound answer
# for pattern records.
_LABEL = r"(?!-)[A-Za-z0-9_-]{1,63}(?<!-)"
_NAME_RE = re.compile(rf"^(?:{_LABEL}\.)*{_LABEL}\.?$")
MAX_NAME_LENGTH = 253

MAX_ZONES = 8
MAX_RECORDS_PER_ZONE = 256
MAX_REQUIRED_DOMAINS = 64
MAX_QUERIES = 500


def normalize_name(name: str) -> str:
    """Canonical comparison form: lowercase, no trailing root dot."""
    return name.rstrip(".").lower()


def validate_name(name: str, what: str) -> str:
    """Validate a DNS name and return its canonical form."""
    if len(name.rstrip(".")) > MAX_NAME_LENGTH or not _NAME_RE.match(name):
        raise ValueError(f"invalid {what}: {name!r}")
    return normalize_name(name)


def in_zone(name: str, zone: str) -> bool:
    """True if ``name`` is ``zone`` itself or a subdomain of it."""
    return name == zone or name.endswith("." + zone)


def zone_covering(name: str, zone_names: list[str]) -> tuple[str, int] | None:
    """Return ``(owning zone, input index)`` for ``name``, or None."""
    best: tuple[str, int] | None = None
    for idx, z in enumerate(zone_names):
        if in_zone(name, z) and (best is None or len(z) > len(best[0])):
            best = (z, idx)
    return best


class Record(BaseModel):
    name: str
    type: Literal["A", "CNAME"]
    value: str
    # TTL is accepted for documentation/round-tripping but deliberately has no
    # effect: the plan is evaluated without caching.
    ttl: int = Field(default=300, ge=0, le=2_147_483_647)

    @field_validator("type", mode="before")
    @classmethod
    def _normalise_type(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        return validate_name(v, "DNS owner name")

    @model_validator(mode="after")
    def _valid_value(self) -> "Record":
        if self.type == "A":
            try:
                ipaddress.IPv4Address(self.value)
            except ValueError:
                raise ValueError(
                    f"A record for {self.name!r} must be an IPv4 address, "
                    f"got {self.value!r}"
                )
            self.value = str(ipaddress.IPv4Address(self.value))
        else:
            self.value = validate_name(
                self.value, f"CNAME target of {self.name!r}"
            )
        return self


def _check_rrset(zone_name: str, field: str, records: list[Record]) -> None:
    """Validate one authoritative snapshot of a zone's records."""
    owned: dict[str, tuple[str, set[str]]] = {}
    for rec in records:
        if not in_zone(rec.name, zone_name):
            raise ValueError(
                f"{field}: {rec.name!r} is not owned by zone {zone_name!r} "
                "(zones must be self-contained)"
            )
        if rec.name == zone_name and rec.type == "CNAME":
            raise ValueError(
                f"{field}: CNAME at the zone apex {zone_name!r} is not "
                "valid in authoritative DNS (the apex holds SOA/NS)"
            )
        previous = owned.get(rec.name)
        if previous is not None and previous[0] != rec.type:
            raise ValueError(
                f"{field}: {rec.name!r} has both A and CNAME records, "
                "which authoritative DNS forbids"
            )
        targets = previous[1] if previous else set()
        if rec.type == "CNAME":
            targets.add(rec.value)
            if len(targets) > 1:
                raise ValueError(
                    f"{field}: {rec.name!r} has conflicting CNAME targets"
                )
        owned[rec.name] = (rec.type, targets)

    # A name holding a CNAME cannot also have names below it.
    cnames = [n for n, (t, _targets) in owned.items() if t == "CNAME"]
    for cname in cnames:
        suffix = "." + cname
        if any(other != cname and other.endswith(suffix) for other in owned):
            raise ValueError(
                f"{field}: {cname!r} is a CNAME but also has names below "
                "it; authoritative DNS forbids that"
            )


class Zone(BaseModel):
    """One authoritative zone described by its current and next state.

    ``records`` is the zone as it exists now; ``new_records`` is the zone
    after this zone's whole-zone cutover.  A zone with identical old and new
    data models an unchanged zone that is still authoritative for the names
    in it (useful when such a zone is a CNAME target).  All owner names must
    belong to the zone, which is what makes "who owns name X" unambiguous.
    """

    name: str
    records: list[Record] = Field(default_factory=list, max_length=MAX_RECORDS_PER_ZONE)
    new_records: list[Record] = Field(
        default_factory=list, max_length=MAX_RECORDS_PER_ZONE
    )

    @field_validator("name")
    @classmethod
    def _valid_zone(cls, v: str) -> str:
        return validate_name(v, "zone name")

    @model_validator(mode="after")
    def _check_zone_records(self) -> "Zone":
        _check_rrset(self.name, "records", self.records)
        _check_rrset(self.name, "new_records", self.new_records)
        return self


class PlanRequest(BaseModel):
    zones: list[Zone] = Field(min_length=1, max_length=MAX_ZONES)
    required_domains: list[str] = Field(
        min_length=1, max_length=MAX_REQUIRED_DOMAINS
    )

    @field_validator("required_domains")
    @classmethod
    def _valid_domains(cls, v: list[str]) -> list[str]:
        out = [validate_name(name, "required domain") for name in v]
        if len(set(out)) != len(out):
            raise ValueError("required_domains contains duplicates")
        return out

    @model_validator(mode="after")
    def _check_topology(self) -> "PlanRequest":
        validate_zone_topology(self.zones)
        _check_domains_covered(self.required_domains, [z.name for z in self.zones])
        return self


def validate_zone_topology(zones: list[Zone] | list[SimZone]) -> None:
    """Zones must be unique and non-nested (no zone inside another)."""
    names = [z.name for z in zones]
    if len(set(names)) != len(names):
        raise ValueError("duplicate zone names")
    for a in names:
        for b in names:
            if a != b and in_zone(a, b):
                raise ValueError(f"zones must be non-nested: {a!r} is inside {b!r}")


def _check_domains_covered(domains: list[str], zone_names: list[str]) -> None:
    for domain in domains:
        if zone_covering(domain, zone_names) is None:
            raise ValueError(
                f"domain {domain!r} is not inside any submitted zone; the "
                "planner only has authoritative data for the submitted zones"
            )


# --------------------------------------------------------------------------- #
# Cache-restart simulation
# --------------------------------------------------------------------------- #


class SimZone(BaseModel):
    """An authoritative zone taking part in the cache simulation.

    The simulation always evaluates the zone's *live* data (``records``),
    i.e. the state right after the recursive resolver restarts.  ``new_records``
    is accepted and validated for symmetry with ``/plan`` but is not used:
    there is no cutover during the measurement window.
    """

    name: str
    records: list[Record] = Field(default_factory=list, max_length=MAX_RECORDS_PER_ZONE)
    new_records: list[Record] = Field(
        default_factory=list, max_length=MAX_RECORDS_PER_ZONE
    )
    # Fixed round-trip time to this authority in milliseconds.  It must be a
    # positive integer; floats such as 1.5 are rejected rather than truncated.
    latency_ms: int = Field(strict=True, gt=0, le=60_000)

    @field_validator("name")
    @classmethod
    def _valid_zone(cls, v: str) -> str:
        return validate_name(v, "zone name")

    @model_validator(mode="after")
    def _check_zone_records(self) -> "SimZone":
        _check_rrset(self.name, "records", self.records)
        _check_rrset(self.name, "new_records", self.new_records)
        return self


class Query(BaseModel):
    """One A query issued to the recursive resolver at ``at_ms``.

    ``at_ms`` is the number of milliseconds since the resolver restarted and
    must be a non-negative integer.  Repeating a name at different times is
    allowed (that is how cache reuse and TTL expiry are exercised).
    """

    name: str
    at_ms: int = Field(strict=True, ge=0, le=2_147_483_647)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        return validate_name(v, "query name")


class SimulateRequest(BaseModel):
    """Zones in ``/plan`` format plus a per-zone upstream latency, and queries."""

    zones: list[SimZone] = Field(min_length=1, max_length=MAX_ZONES)
    queries: list[Query] = Field(min_length=1, max_length=MAX_QUERIES)

    @model_validator(mode="after")
    def _check_topology(self) -> "SimulateRequest":
        validate_zone_topology(self.zones)
        # Query names may legitimately fall outside the submitted zones; such a
        # chain is reported as ``external_target`` exactly like in /plan, where
        # a CNAME may leave the submitted zones.
        return self
