"""Run the bundled scenarios through the service without a server.

Usage (from the repository root)::

    python examples/demo.py

It prints a short, human-readable summary of each response.  Start the service
with ``docker compose up --build`` and use ``examples/*.json`` with curl for the
HTTP examples in README.md.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.models import PlanRequest, SimulateRequest  # noqa: E402
from app.service import build_plan, build_simulation  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


def summarise(name: str, out: dict) -> None:
    print(f"=== {name} -> {out['status']}")
    print(out["message"])
    if out["order"]:
        for step in out["steps"]:
            print(
                f"  {step['step']}. {step['zone']}: "
                f"{step['addresses_before']} -> {step['addresses_after']}"
            )
    if out["fault_chains"]:
        for chain in out["fault_chains"]:
            print(f"  故障[{chain['category_label']}] {chain['chain']}")
    if out["blocked_zones"]:
        print(f"  安全前缀: {out['safe_prefix'] or '（无）'}")
        for blocked in out["blocked_zones"]:
            labels = "/".join(c["label"] for c in blocked["categories"])
            print(f"  若无安全顺序时先发布 {blocked['zone']} -> {labels}")
            for failure in blocked["failures"]:
                print(f"      {failure['chain']}")
    print()


def summarise_simulation(name: str, out: dict) -> None:
    print(f"=== {name} -> {out['mode']}")
    print(f"  共 {out['total_upstream_queries']} 次回源，模拟在 {out['finish_ms']} ms 结束")
    for zone in out["zones"]:
        peak = zone["peak"]
        print(
            f"  {zone['zone']} (RTT {zone['latency_ms']}ms): "
            f"回源 {zone['upstream_queries']} 次，"
            f"按秒 {[(b['second'], b['queries']) for b in zone['seconds']]}，"
            f"峰值秒 {peak['second']}（{peak['queries']} 次）"
        )
    for result in out["results"]:
        if result["resolved"]:
            status = "A " + ", ".join(result["addresses"])
        else:
            status = f"失败[{result['category_label']}] {result['message']}"
        print(
            f"  q{result['query_id']} @{result['started_at_ms']}ms "
            f"{result['domain']}: {status}"
        )
    print()


def main() -> None:
    for path in sorted(HERE.glob("0*.json")):
        data = json.loads(path.read_text())
        if "queries" in data:
            summarise_simulation(
                path.name, build_simulation(SimulateRequest.model_validate(data))
            )
        else:
            summarise(path.name, build_plan(PlanRequest.model_validate(data)))


if __name__ == "__main__":
    main()
