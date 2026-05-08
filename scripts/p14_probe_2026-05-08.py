"""P14 FAIL probe (Step 29.y close-out F.1.b investigation).

Reproduces the exact Pillar 14 mint -> consent.grant sequence that
returned 403 cross_tenant_denied in verify task bdff874f, but with
explicit introspection at every step so we can determine whether
the bug is in:
  (a) K2 mint -- the stored api_keys.tenant_id is wrong
  (b) auth resolution -- request.state.tenant_id is wrong despite (a) being right
  (c) test code -- some variable is mixed up
  (d) consent route -- the comparison logic is wrong

Run inside the prod-ops Fargate task with the platform-admin key in
LUCIEL_PLATFORM_ADMIN_KEY (already wired via SSM secret). Network
path: ops task subnet -> public IGW -> ALB -> backend ECS task.

Usage (from inside the ops container):
    python /tmp/p14_probe.py 2>&1 | tee /tmp/p14_probe.log

Output: structured PASS/FAIL line per check, plus the raw JSON
response of every API call so we can inspect the tenant binding.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import httpx

BASE_URL = os.environ.get("LUCIEL_BASE_URL", "https://api.vantagemind.ai")
PA_KEY = os.environ.get("LUCIEL_PLATFORM_ADMIN_KEY")
if not PA_KEY:
    print("FATAL: LUCIEL_PLATFORM_ADMIN_KEY not set in environment")
    sys.exit(2)


def h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def line(label: str, *, ok: bool, detail: str = "") -> None:
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {label}: {detail}")


def show(label: str, resp: httpx.Response) -> dict:
    print(f"\n--- {label} ---")
    print(f"  status: {resp.status_code}")
    try:
        body = resp.json()
        print(f"  body: {json.dumps(body, indent=2, default=str)[:1500]}")
        return body
    except Exception:
        print(f"  text: {resp.text[:1500]}")
        return {}


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    t1_id = f"p14probe-t1-{suffix}"
    t2_id = f"p14probe-t2-{suffix}"
    domain_id = "general"

    print(f"P14 FAIL probe {suffix}")
    print(f"  base_url: {BASE_URL}")
    print(f"  t1_id: {t1_id}")
    print(f"  t2_id: {t2_id}")

    with httpx.Client(base_url=BASE_URL, timeout=30.0, verify=True) as c:
        # 1. Onboard T1
        r = c.post(
            "/api/v1/admin/tenants/onboard",
            headers=h(PA_KEY),
            json={
                "tenant_id": t1_id,
                "display_name": "P14 Probe T1",
                "default_domain_id": domain_id,
                "default_domain_display_name": "General",
            },
        )
        body = show("onboard T1", r)
        if r.status_code not in (200, 201):
            line("onboard T1", ok=False, detail=f"status {r.status_code}")
            return 1
        t1_admin_key = body.get("admin_raw_key") or body.get("admin_api_key", {}).get("raw_key")
        line("onboard T1", ok=bool(t1_admin_key), detail=f"got admin key: {bool(t1_admin_key)}")

        # 2. Onboard T2
        r = c.post(
            "/api/v1/admin/tenants/onboard",
            headers=h(PA_KEY),
            json={
                "tenant_id": t2_id,
                "display_name": "P14 Probe T2",
                "default_domain_id": domain_id,
                "default_domain_display_name": "General",
            },
        )
        body = show("onboard T2", r)
        if r.status_code not in (200, 201):
            line("onboard T2", ok=False, detail=f"status {r.status_code}")
            return 1
        t2_admin_key = body.get("admin_raw_key") or body.get("admin_api_key", {}).get("raw_key")
        t2_admin_id = body.get("admin_api_key", {}).get("id")
        line("onboard T2", ok=bool(t2_admin_key), detail=f"got admin key id={t2_admin_id}")

        # 3. Read back T2 admin key tenant binding (sanity check on the onboard path)
        if t2_admin_id is not None:
            r = c.get(
                f"/api/v1/admin/api-keys?tenant_id={t2_id}",
                headers=h(PA_KEY),
            )
            body = show("list T2 admin keys", r)
            t2_admin_match = next(
                (k for k in (body if isinstance(body, list) else []) if k.get("id") == t2_admin_id),
                None,
            )
            if t2_admin_match:
                line(
                    "T2 admin key stored tenant",
                    ok=t2_admin_match.get("tenant_id") == t2_id,
                    detail=f"db.tenant_id={t2_admin_match.get('tenant_id')!r} expected={t2_id!r}",
                )

        # 4. Create agent A2 in T2 (proves t2_admin_key has T2 scope)
        agent_slug = f"p14probe-a2-{suffix[:4]}"
        r = c.post(
            "/api/v1/admin/agents",
            headers=h(t2_admin_key),
            json={
                "tenant_id": t2_id,
                "domain_id": domain_id,
                "agent_id": agent_slug,
                "display_name": "P14 Probe A2",
                "contact_email": f"p14-{suffix}@example.com",
            },
        )
        show("create agent A2 via t2_admin_key", r)
        line("create A2", ok=r.status_code in (200, 201), detail=f"status {r.status_code}")

        # 5. Mint K2 chat key in T2 via t2_admin_key
        r = c.post(
            "/api/v1/admin/api-keys",
            headers=h(t2_admin_key),
            json={
                "tenant_id": t2_id,
                "domain_id": domain_id,
                "agent_id": agent_slug,
                "display_name": "P14 Probe K2",
                "permissions": ["chat", "sessions"],
            },
        )
        body = show("mint K2 via t2_admin_key", r)
        if r.status_code not in (200, 201):
            line("mint K2", ok=False, detail=f"status {r.status_code}")
            return 1
        k2_raw = body.get("raw_key")
        k2_id = body.get("api_key", {}).get("id")
        k2_stored_tenant = body.get("api_key", {}).get("tenant_id")
        line(
            "K2 stored tenant_id (mint response)",
            ok=k2_stored_tenant == t2_id,
            detail=f"db.tenant_id={k2_stored_tenant!r} expected={t2_id!r}",
        )

        # 6. Read K2 back from list endpoint to confirm DB state
        r = c.get(
            f"/api/v1/admin/api-keys?tenant_id={t2_id}",
            headers=h(PA_KEY),
        )
        body = show("list T2 keys (post-mint)", r)
        k2_match = next(
            (k for k in (body if isinstance(body, list) else []) if k.get("id") == k2_id),
            None,
        )
        if k2_match:
            line(
                "K2 stored tenant_id (DB readback)",
                ok=k2_match.get("tenant_id") == t2_id,
                detail=f"db.tenant_id={k2_match.get('tenant_id')!r} domain_id={k2_match.get('domain_id')!r} agent_id={k2_match.get('agent_id')!r}",
            )

        # 7. Issue a benign request with K2 to learn what tenant the auth
        #    middleware resolves -- /api/v1/sessions returns 200/201 only
        #    if the body tenant_id matches the key's resolved scope.
        sess_body = {
            "user_id": f"p14probe-end-{suffix}",
            "tenant_id": t2_id,
            "domain_id": domain_id,
        }
        r = c.post(
            "/api/v1/sessions",
            headers=h(k2_raw),
            json=sess_body,
        )
        show("POST /sessions with K2 (tenant_id=t2)", r)
        line(
            "K2 -> /sessions for T2",
            ok=r.status_code in (200, 201),
            detail=f"status {r.status_code} -- if 403 then auth middleware resolves K2 to a different tenant than DB stores",
        )

        # 8. The actual failing call from P14
        r = c.post(
            "/api/v1/consent/grant",
            headers=h(k2_raw),
            json={
                "user_id": f"p14probe-end-{suffix}",
                "tenant_id": t2_id,
                "consent_type": "memory_storage",
                "collection_method": "explicit_form",
            },
        )
        body = show("POST /consent/grant with K2 (tenant_id=t2) -- THE FAILING CALL", r)
        if r.status_code in (200, 201):
            line("consent.grant K2->T2", ok=True, detail="200/201 -- bug not reproduced")
        else:
            detail = json.dumps(body, default=str)[:500] if body else r.text[:500]
            line(
                "consent.grant K2->T2",
                ok=False,
                detail=f"status {r.status_code} body={detail}",
            )

        # 9. Cleanup -- deactivate K2 + admin keys + tear down tenants
        print("\n--- cleanup ---")
        if k2_id:
            cr = c.delete(f"/api/v1/admin/api-keys/{k2_id}", headers=h(PA_KEY))
            print(f"  deactivate K2 id={k2_id}: status {cr.status_code}")
        # Note: tenant teardown via admin/tenants/{id} DELETE if present;
        # otherwise leave for periodic cleanup. Probe is read-mostly.

    print("\nProbe complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
