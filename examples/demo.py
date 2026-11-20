"""Run the three bundled scenarios through the planner without a server.

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

from app.models import PlanRequest  # noqa: E402
from app.service import build_plan  # noqa: E402

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


def main() -> None:
    for path in sorted(HERE.glob("0*.json")):
        request = PlanRequest.model_validate(json.loads(path.read_text()))
        summarise(path.name, build_plan(request))


if __name__ == "__main__":
    main()
