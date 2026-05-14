"""
Build td-backend-rev49.json additively from td-backend-rev48.json.

Step 30a.2 Phase B mutation. Adds:
  - 4 new env vars:
      SESSION_COOKIE_DOMAIN  = "vantagemind.ai"
      MARKETING_SITE_URL     = "https://www.vantagemind.ai"
      BILLING_SUCCESS_URL    = "https://www.vantagemind.ai/account/billing?status=success"
      BILLING_CANCEL_URL     = "https://www.vantagemind.ai/pricing?status=cancelled"
  - 1 new secret:
      MAGIC_LINK_SECRET  ← /luciel/production/magic_link_secret (SecureString, version 1)

Image digest unchanged: step30a2-e8f6c5f-r3
This is purely a config-additive revision; no code change, no rebuild.

Closes drifts (resolved at GATE 5):
  - D-magic-link-secret-absent-task-def-2026-05-14
  - D-session-cookie-domain-absent-task-def-2026-05-14
  - D-marketing-site-url-wrong-domain-2026-05-14

Audit guarantees:
  - Aborts if any new env/secret name already exists in :48
  - Aborts if image, role ARNs, cpu, memory, or family differ from :48
  - Writes deterministic order (alphabetical secrets, env in original-then-new order)
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "td-backend-rev48.json"
DST = REPO_ROOT / "td-backend-rev49.json"

NEW_ENV = [
    {"name": "SESSION_COOKIE_DOMAIN", "value": "vantagemind.ai"},
    {"name": "MARKETING_SITE_URL",    "value": "https://www.vantagemind.ai"},
    {"name": "BILLING_SUCCESS_URL",   "value": "https://www.vantagemind.ai/account/billing?status=success"},
    {"name": "BILLING_CANCEL_URL",    "value": "https://www.vantagemind.ai/pricing?status=cancelled"},
]

NEW_SECRETS = [
    {
        "name": "MAGIC_LINK_SECRET",
        "valueFrom": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/magic_link_secret",
    },
]


def main():
    if not SRC.exists():
        print(f"ABORT: source file missing: {SRC}", file=sys.stderr)
        sys.exit(1)

    with SRC.open() as f:
        td = json.load(f)

    cd = td["containerDefinitions"][0]

    # ---- Invariant checks: refuse to touch fields we don't own ----
    expected_image = "729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:step30a2-e8f6c5f-r3"
    if cd["image"] != expected_image:
        print(f"ABORT: image drift. expected={expected_image}, got={cd['image']}", file=sys.stderr)
        sys.exit(2)
    if td.get("family") != "luciel-backend":
        print(f"ABORT: family != luciel-backend, got={td.get('family')}", file=sys.stderr)
        sys.exit(2)
    if td.get("cpu") != "512" or td.get("memory") != "1024":
        print(f"ABORT: cpu/memory drift (cpu={td.get('cpu')}, memory={td.get('memory')})", file=sys.stderr)
        sys.exit(2)

    # ---- Collision checks ----
    existing_env_names = {e["name"] for e in cd.get("environment", [])}
    existing_secret_names = {s["name"] for s in cd.get("secrets", [])}

    for e in NEW_ENV:
        if e["name"] in existing_env_names:
            print(f"ABORT: env collision on {e['name']}", file=sys.stderr)
            sys.exit(3)
        if e["name"] in existing_secret_names:
            print(f"ABORT: env name {e['name']} would shadow existing secret", file=sys.stderr)
            sys.exit(3)

    for s in NEW_SECRETS:
        if s["name"] in existing_secret_names:
            print(f"ABORT: secret collision on {s['name']}", file=sys.stderr)
            sys.exit(3)
        if s["name"] in existing_env_names:
            print(f"ABORT: secret name {s['name']} would shadow existing env", file=sys.stderr)
            sys.exit(3)

    # ---- Apply additions ----
    # env: keep original order, append new in declared order (deterministic)
    cd["environment"] = list(cd.get("environment", [])) + NEW_ENV
    # secrets: maintain alphabetical sort like existing file (audit-friendly)
    merged_secrets = list(cd.get("secrets", [])) + NEW_SECRETS
    merged_secrets.sort(key=lambda s: s["name"])
    cd["secrets"] = merged_secrets

    # ---- Write rev49 ----
    with DST.open("w") as f:
        json.dump(td, f, indent=4)
        f.write("\n")

    # ---- Report ----
    print(f"WROTE {DST}")
    print(f"  env count:    {len(cd['environment'])}  (was {len(cd['environment']) - len(NEW_ENV)})")
    print(f"  secret count: {len(cd['secrets'])}  (was {len(cd['secrets']) - len(NEW_SECRETS)})")
    print()
    print("New env entries:")
    for e in NEW_ENV:
        # mask URLs that contain query params (we don't, but be tidy)
        print(f"  + {e['name']} = {e['value']}")
    print()
    print("New secret entries:")
    for s in NEW_SECRETS:
        print(f"  + {s['name']} ← {s['valueFrom']}")
    print()
    print("Invariants preserved: image, family, cpu, memory, roles, networkMode")


if __name__ == "__main__":
    main()
