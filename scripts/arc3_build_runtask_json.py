"""scripts/arc3_build_runtask_json.py \u2014 emit run-task JSON files.

Companion helper for scripts/arc3_ecs_oneshot.ps1. Exists because
PowerShell 5.1's JSON layer (both ConvertTo-Json and System.Web.
JavaScriptSerializer) has shown enough sharp edges in this arc that
delegating to Python's stdlib json is the cleanest path. Python's
json.dump is deterministic, BOM-less, never collapses single-element
arrays, and never chases PSObject reflection chains into circular
references.

Reads inputs from a single JSON file (path passed as argv[1]) shaped
as:

  {
    "container_name": "luciel-prod-ops",
    "command":        ["sh", "-c", "..."],
    "environment":    [{"name":"K","value":"..."}, ...],
    "subnets":        ["subnet-...", ...],
    "security_groups":["sg-...", ...],
    "out_dir":        "C:\\\\Users\\\\...\\\\Temp\\\\arc3-runtask-<guid>"
  }

Writes two files in out_dir:

  - overrides.json : the --overrides argument for aws ecs run-task
  - network.json   : the --network-configuration argument

Both are BOM-less UTF-8.
"""
from __future__ import annotations

import json
import os
import sys


def _require_list(val, name: str) -> list:
    """PowerShell may have collapsed a 1-element list into a scalar/dict.
    Detect and reject so we never silently emit malformed run-task JSON.

    Accepts: already-correct list, OR a dict (auto-rewrap as 1-element
    list iff the dict has the expected env-pair shape), OR a string
    (auto-rewrap as 1-element list for subnets/SGs).
    """
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        # Likely a collapsed environment entry { 'name': ..., 'value': ... }.
        if set(val.keys()) == {"name", "value"}:
            return [val]
        raise ValueError(
            f"{name}: expected list, got dict with keys {sorted(val)}; "
            "PowerShell likely collapsed a single-element array."
        )
    if isinstance(val, str):
        # Likely a collapsed subnet/SG single-element.
        return [val]
    raise ValueError(f"{name}: expected list, got {type(val).__name__}: {val!r}")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: arc3_build_runtask_json.py <input.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        inp = json.load(f)

    out_dir = inp["out_dir"]
    os.makedirs(out_dir, exist_ok=True)

    env_list = _require_list(inp["environment"], "environment")
    # Each env entry must be a {name, value} dict.
    for i, e in enumerate(env_list):
        if not isinstance(e, dict) or set(e.keys()) != {"name", "value"}:
            raise ValueError(
                f"environment[{i}]: expected {{name, value}} dict, got {e!r}"
            )

    overrides = {
        "containerOverrides": [
            {
                "name":        inp["container_name"],
                "command":     _require_list(inp["command"], "command"),
                "environment": env_list,
            }
        ]
    }
    network = {
        "awsvpcConfiguration": {
            "subnets":        _require_list(inp["subnets"], "subnets"),
            "securityGroups": _require_list(inp["security_groups"], "security_groups"),
            "assignPublicIp": "ENABLED",
        }
    }

    overrides_path = os.path.join(out_dir, "overrides.json")
    network_path   = os.path.join(out_dir, "network.json")
    with open(overrides_path, "w", encoding="utf-8", newline="") as g:
        json.dump(overrides, g, ensure_ascii=True)
    with open(network_path, "w", encoding="utf-8", newline="") as g:
        json.dump(network, g, ensure_ascii=True)

    print(overrides_path)
    print(network_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
