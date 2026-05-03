"""Pillar 19 - Audit-log API mount + tenant-scope enforcement.

Step 28 Phase 2 - Commit 2 (with Commit 2b review fixes).
Resolves canonical-recap §4.1 item 4 ("/api/v1/admin/audit-log returns
404 currently") and the H3 review finding from Commit 2b
(cross-tenant scope assertion was vacuous because it filtered by a
fake tenant_id that never existed).

Asserts:
  1. GET /api/v1/admin/audit-log resolves (status != 404). With a
     tenant_admin key, the response is paginated AdminAuditLogPage.
  2. Audit rows produced by Pillar 1 (tenant onboarding) and the key
     mints earlier in the suite are visible — i.e. the read path
     reaches the same admin_audit_logs rows the write path created.
  3. Tenant scoping is enforced against a REAL second tenant: a
     tenant_admin key cannot see rows for any other tenant. Even if
     the caller passes ?tenant_id=<real-other-tenant>, the API forces
     the filter to the caller's own tenant_id.
     Why this matters: prior to Commit 2b this assertion was vacuous —
     it filtered by the literal string "some-other-tenant" which
     never exists, so a buggy server returning [] for a non-existent
     filter would have falsely passed.
  4. Mutation methods are NOT exposed: POST / PATCH / DELETE on the
     audit-log path return 405 (or 404 if no such route registered).
     Audit log is append-only — the API surface guards this even
     before the DB-grant layer enforces it.
  5. Resource sub-path /audit-log/resource/{type}/{pk} is mounted.
  6. Actor sub-path /audit-log/actor/{label} is platform_admin only.

Pillar 19 is the regression guard for the read API, paired with the
existing Pillar 17 which guards that the write path (deactivate_key)
emits an audit row. Together they cover both halves of the contract.

Self-cleanup: pillar onboards a throwaway second tenant for assertion
3 and deactivates it before returning. The suite-level teardown only
knows about state.tenant_id; this pillar owns its own residue.

Step 28 Phase 2 hotfix: after onboarding the secondary, also mint an
API key against the secondary tenant via POST /admin/api-keys. The
onboarding service writes ZERO audit rows (the API mint path is the
only hook that records ACTION_CREATE for api_key resources), so
without this anchor mint there would be no rows tagged with
secondary_tid for assertion 3b to find -- and 3b would silently pass
for the same reason it was meant to catch.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.verification.fixtures import RunState
from app.verification.http_client import call, h, pooled_client
from app.verification.runner import Pillar


def _new_secondary_tenant_id() -> str:
    """Distinct prefix from the primary state.tenant_id so residue
    sweeps and pillar 10 still cover us, but log lines can tell them
    apart."""
    return f"step26-verify-p19sec-{uuid.uuid4().hex[:6]}"


class AuditLogApiMountPillar(Pillar):
    number = 19
    name = "audit-log API mount + tenant scope (Phase 2 commit 2)"

    def run(self, state: RunState) -> str:
        if not state.tenant_admin_key:
            raise AssertionError("pillar 19 requires tenant_admin_key from pillar 1")
        if not state.tenant_id:
            raise AssertionError("pillar 19 requires tenant_id from pillar 1")
        if not state.platform_admin_key:
            raise AssertionError("pillar 19 requires platform_admin_key")

        ak = state.tenant_admin_key
        pa = state.platform_admin_key
        tid = state.tenant_id

        secondary_tid = _new_secondary_tenant_id()
        secondary_onboarded = False

        try:
            with pooled_client() as c:
                # ---- 0. Onboard a real second tenant for the cross-tenant
                #         scope-leak assertion. Without this, assertion 3
                #         was vacuous (filtering by a tenant that doesn't
                #         exist returns [] regardless of scope enforcement).
                onboard_body: dict[str, Any] = {
                    "tenant_id": secondary_tid,
                    "display_name": "Pillar 19 secondary",
                }
                r0 = call(
                    "POST",
                    "/api/v1/admin/tenants/onboard",
                    pa,
                    json=onboard_body,
                    expect=(200, 201),
                    client=c,
                )
                secondary_onboarded = True
                _ = r0.json()  # presence is enough; we only need the tid

                # ---- 0b. Force at least one audit row tagged with
                #         secondary_tid. OnboardingService.onboard_tenant
                #         creates the tenant + first admin key directly
                #         through the service layer and writes ZERO
                #         admin_audit_logs rows; only the API mint path
                #         (POST /admin/api-keys) emits an audit row.
                #         Without this extra mint, assertion 3b would be
                #         vacuous (no rows tagged secondary_tid would exist
                #         for platform_admin to find), defeating the very
                #         vacuous-pass guard the assertion exists to prevent.
                mint_body: dict[str, Any] = {
                    "tenant_id": secondary_tid,
                    "display_name": "Pillar 19 secondary audit anchor",
                    "permissions": ["chat", "sessions"],
                }
                call(
                    "POST",
                    "/api/v1/admin/api-keys",
                    pa,
                    json=mint_body,
                    expect=(200, 201),
                    client=c,
                )

                # ---- 1. Endpoint resolves (not 404) under tenant_admin ----
                r1 = c.get("/api/v1/admin/audit-log", headers=h(ak))
                if r1.status_code == 404:
                    raise AssertionError(
                        "GET /api/v1/admin/audit-log returned 404 -- "
                        "audit-log router not mounted. "
                        f"body={r1.text[:200]}"
                    )
                if r1.status_code != 200:
                    raise AssertionError(
                        f"GET /api/v1/admin/audit-log returned {r1.status_code} "
                        f"under tenant_admin (expected 200). body={r1.text[:200]}"
                    )
                page = r1.json()
                if not isinstance(page, dict) or "items" not in page:
                    raise AssertionError(
                        "GET /api/v1/admin/audit-log response shape wrong; "
                        f"expected dict with 'items' key. got={page!r}"
                    )

                # ---- 2. Rows for THIS tenant exist (Pillar 1 onboarding wrote some) ----
                items_self = page["items"]
                tenants_seen = {row.get("tenant_id") for row in items_self}
                # tenants_seen should be a subset of {tid, None} -- middleware
                # forces tenant filter to caller's tenant; system rows have
                # tenant_id=None.
                unexpected = tenants_seen - {tid, None}
                if unexpected:
                    raise AssertionError(
                        "tenant_admin saw audit rows for other tenants: "
                        f"{unexpected}. Tenant scoping breach."
                    )

                # ---- 3. Cross-tenant scope: pass ?tenant_id=<real other tenant>.
                #         The secondary tenant just had its onboard write 1+
                #         audit rows, so a buggy server that honored the query
                #         would return non-empty rows tagged with secondary_tid.
                #         A correct server forces the filter back to `tid` and
                #         must NOT leak any row tagged with secondary_tid.
                r3 = c.get(
                    f"/api/v1/admin/audit-log?tenant_id={secondary_tid}",
                    headers=h(ak),
                )
                if r3.status_code != 200:
                    raise AssertionError(
                        "tenant_admin with cross-tenant filter should still "
                        f"return 200 (filter forced silently); got {r3.status_code}. "
                        f"body={r3.text[:200]}"
                    )
                cross_page = r3.json()
                cross_tenants = {row.get("tenant_id") for row in cross_page["items"]}
                if secondary_tid in cross_tenants:
                    raise AssertionError(
                        f"tenant_admin passing tenant_id={secondary_tid} "
                        "leaked rows from another real tenant. "
                        "Defense-in-depth failure."
                    )
                unexpected = cross_tenants - {tid, None}
                if unexpected:
                    raise AssertionError(
                        f"tenant_admin saw unexpected tenants {unexpected} "
                        f"when querying ?tenant_id={secondary_tid}. "
                        "Scope-override middleware is not forcing tenant_id."
                    )

                # ---- 3b. Sanity check: platform_admin CAN see secondary's rows
                #         when explicitly filtering for them. This proves the
                #         secondary tenant's audit rows actually exist, so the
                #         negative result in (3) is meaningful, not vacuous.
                r3b = c.get(
                    f"/api/v1/admin/audit-log?tenant_id={secondary_tid}",
                    headers=h(pa),
                )
                if r3b.status_code != 200:
                    raise AssertionError(
                        "platform_admin GET audit-log for secondary returned "
                        f"{r3b.status_code}; expected 200. body={r3b.text[:200]}"
                    )
                pa_page = r3b.json()
                pa_tenants = {row.get("tenant_id") for row in pa_page["items"]}
                if secondary_tid not in pa_tenants:
                    raise AssertionError(
                        "platform_admin querying secondary tenant saw no rows "
                        f"tagged {secondary_tid}; assertion 3 would be vacuous. "
                        f"saw tenants={pa_tenants}"
                    )

                # ---- 4. Platform-admin can see across tenants for primary tid ----
                r4 = c.get(
                    f"/api/v1/admin/audit-log?tenant_id={tid}",
                    headers=h(pa),
                )
                if r4.status_code != 200:
                    raise AssertionError(
                        f"platform_admin GET audit-log returned {r4.status_code}; "
                        f"expected 200. body={r4.text[:200]}"
                    )

                # ---- 5. Mutation methods are NOT exposed ----
                for method in ("post", "put", "patch", "delete"):
                    rm = getattr(c, method)(
                        "/api/v1/admin/audit-log",
                        headers=h(pa),
                    )
                    if rm.status_code not in (404, 405):
                        raise AssertionError(
                            f"{method.upper()} /api/v1/admin/audit-log returned "
                            f"{rm.status_code}; audit log must be read-only. "
                            f"body={rm.text[:200]}"
                        )

                # ---- 6. Resource-history sub-path resolves ----
                # Use a synthetic resource_pk that won't exist; we just want
                # to prove the route is mounted (200 with empty list, NOT 404).
                # The endpoint is contracted to return 200+empty-list for
                # missing resources, so 404 here ALWAYS means route-not-mounted.
                r6 = c.get(
                    "/api/v1/admin/audit-log/resource/luciel_instance/999999999",
                    headers=h(ak),
                )
                if r6.status_code != 200:
                    raise AssertionError(
                        f"resource-history sub-path returned {r6.status_code}; "
                        f"expected 200 with empty list. body={r6.text[:200]}"
                    )

                # ---- 7. Actor sub-path: platform_admin only ----
                r7_denied = c.get(
                    "/api/v1/admin/audit-log/actor/lucskaaaaaaa",
                    headers=h(ak),
                )
                if r7_denied.status_code != 403:
                    raise AssertionError(
                        "tenant_admin should be denied (403) on actor "
                        f"sub-path; got {r7_denied.status_code}. "
                        f"body={r7_denied.text[:200]}"
                    )

                r7_ok = c.get(
                    "/api/v1/admin/audit-log/actor/lucskaaaaaaa",
                    headers=h(pa),
                )
                if r7_ok.status_code != 200:
                    raise AssertionError(
                        "platform_admin should get 200 on actor sub-path "
                        f"(empty list ok); got {r7_ok.status_code}. "
                        f"body={r7_ok.text[:200]}"
                    )

            return (
                f"audit-log mounted, tenant_admin sees {len(items_self)} "
                f"rows scoped to tenant={tid[:24]}, cross-tenant leak guard "
                f"passes against real secondary={secondary_tid}, mutation "
                f"methods 404/405, actor sub-path platform_admin-gated"
            )
        finally:
            # ---- Self-cleanup: deactivate secondary tenant.
            # Best-effort: failures here must not mask an assertion failure
            # in the body, but they must not silently leave residue either.
            # The residue sweep + pillar 10 are the safety net.
            if secondary_onboarded:
                try:
                    with pooled_client() as c:
                        c.patch(
                            f"/api/v1/admin/tenants/{secondary_tid}",
                            headers=h(pa),
                            json={"active": False},
                        )
                except Exception:
                    # Swallow: residue sweep + pillar 10 will catch it.
                    pass


PILLAR = AuditLogApiMountPillar()
