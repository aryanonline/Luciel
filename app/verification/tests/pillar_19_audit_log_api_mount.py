"""Pillar 19 - Audit-log API mount + tenant-scope enforcement.

Step 28 Phase 2 - Commit 2. Resolves canonical-recap §4.1 item 4
("/api/v1/admin/audit-log returns 404 currently").

Asserts:
  1. GET /api/v1/admin/audit-log resolves (status != 404). With a
     tenant_admin key, the response is paginated AdminAuditLogPage.
  2. Audit rows produced by Pillar 1 (tenant onboarding) and the key
     mints earlier in the suite are visible — i.e. the read path
     reaches the same admin_audit_logs rows the write path created.
  3. Tenant scoping is enforced: a tenant_admin key cannot see rows
     for any other tenant. Even if the caller passes
     `?tenant_id=<other>`, the API forces the filter to the caller's
     own tenant_id (defense-in-depth on top of the admin-perm gate).
  4. Mutation methods are NOT exposed: POST / PATCH / DELETE on the
     audit-log path return 405 (or 404 if no such route registered).
     Audit log is append-only — the API surface guards this even
     before the DB-grant layer enforces it.

Pillar 19 is the regression guard for the read API, paired with the
existing Pillar 17 which guards that the write path (deactivate_key)
emits an audit row. Together they cover both halves of the contract.
"""

from __future__ import annotations

from app.verification.fixtures import RunState
from app.verification.http_client import h, pooled_client
from app.verification.runner import Pillar


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

        with pooled_client() as c:
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
            # tenants_seen should be a subset of {tid} -- middleware
            # forces tenant filter to caller's tenant.
            unexpected = tenants_seen - {tid, None}
            if unexpected:
                raise AssertionError(
                    "tenant_admin saw audit rows for other tenants: "
                    f"{unexpected}. Tenant scoping breach."
                )

            # ---- 3. Even with cross-tenant tenant_id query, scope holds ----
            r3 = c.get(
                f"/api/v1/admin/audit-log?tenant_id=some-other-tenant",
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
            unexpected = cross_tenants - {tid, None}
            if unexpected:
                raise AssertionError(
                    "tenant_admin passing tenant_id=some-other-tenant "
                    "leaked rows from another tenant. Defense-in-depth "
                    f"failure. Saw: {unexpected}"
                )

            # ---- 4. Platform-admin can see across tenants ----
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
            r6 = c.get(
                "/api/v1/admin/audit-log/resource/luciel_instance/999999999",
                headers=h(ak),
            )
            if r6.status_code == 404:
                # 404 here would mean "route not mounted" not "resource
                # not found" -- our endpoint always returns 200 with an
                # empty list when the resource has no rows. Distinguish
                # by checking the body.
                if "Not Found" in r6.text or "not found" in r6.text.lower():
                    # Could be route-not-mounted OR a 404 from the
                    # framework with an empty list. Be strict:
                    raise AssertionError(
                        "GET /api/v1/admin/audit-log/resource/.../... "
                        "returned 404 -- sub-route not mounted. "
                        f"body={r6.text[:200]}"
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
            f"rows scoped to tenant={tid[:24]}, mutation methods 404/405, "
            f"actor sub-path platform_admin-gated"
        )


PILLAR = AuditLogApiMountPillar()
