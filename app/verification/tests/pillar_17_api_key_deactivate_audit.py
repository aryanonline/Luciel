"""Pillar 17 - api_key deactivate emits ACTION_DEACTIVATE audit row (D5).

Drift item D5 from Step 28 Phase 1. Pre-Commit-6, ApiKeyService.deactivate_key
performed `active=False; db.commit()` with no audit row, breaking
Invariant 4 (audit-before-commit) on the deactivation path. fix(28)
Commit 6 retrofitted the method to emit ACTION_DEACTIVATE via
AdminAuditRepository.record() in the same transaction as the active=False
UPDATE, mirroring the canonical pattern from rotate_keys_for_agent.

This pillar exercises the full request path -- HTTP DELETE -> endpoint ->
service -> repository -> DB -> audit-log GET -- so it catches regressions
at any layer:

  - if someone removes audit_ctx from the admin endpoint signature, no
    audit row lands and this pillar fails
  - if someone reverts the AdminAuditRepository.record() call inside
    deactivate_key, no row lands and this pillar fails
  - if someone moves db.commit() before the record() call (breaking
    Invariant 4), the row may still land but in a separate txn -- a
    follow-up D-tracker can extend this pillar to assert txn-locality

Asserts:
  1. POST /api/v1/admin/api-keys mints a throwaway tenant-scoped key
     and returns 201 with an `id` field.
  2. DELETE /api/v1/admin/api-keys/{id} returns 204.
  3. GET /api/v1/admin/audit-log?tenant_id=... returns 200 with at least
     two rows for that key_id: one with action='create' (from mint, this
     has been emitted since Step 24.5) and exactly one with
     action='deactivate' AND resource_type='api_key' (D5 regression guard).

Lenient on /audit-log being mounted: if the route 404s, this pillar
degrades to "smoke-tested but audit assertion skipped" rather than
failing -- matching Pillar 7's posture.

Uses tenant_admin_key from Pillar 1. Read-only effective: the throwaway
key is created and deactivated within this pillar, leaving no live key
behind. The audit rows themselves persist (intentional -- they're the
audit trail).
"""

from __future__ import annotations

from typing import Any

from app.verification.fixtures import RunState
from app.verification.http_client import call, h, pooled_client
from app.verification.runner import Pillar


def _extract_audit_rows(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("items") or body.get("results") or body.get("rows") or []
    return []


class ApiKeyDeactivateAuditPillar(Pillar):
    number = 17
    name = "api_key deactivate emits ACTION_DEACTIVATE audit row (D5)"

    def run(self, state: RunState) -> str:
        if not state.tenant_admin_key:
            raise AssertionError("pillar 17 requires tenant_admin_key from pillar 1")
        if not state.tenant_id:
            raise AssertionError("pillar 17 requires tenant_id from pillar 1")

        ak = state.tenant_admin_key
        tid = state.tenant_id

        with pooled_client() as c:
            # ---------- 1. mint a throwaway tenant-scoped api_key ----------
            mint_payload = {
                "tenant_id": tid,
                "display_name": f"pillar17-d5-{tid[-8:]}",
                "permissions": ["chat", "sessions"],
            }
            r_mint = c.post(
                "/api/v1/admin/api-keys",
                headers=h(ak),
                json=mint_payload,
            )
            if r_mint.status_code != 201:
                raise AssertionError(
                    f"pillar 17 mint failed: POST /api/v1/admin/api-keys "
                    f"returned {r_mint.status_code}, expected 201. "
                    f"body={r_mint.text[:300]}"
                )
            mint_body = r_mint.json()
            key_id = mint_body.get("id") or (mint_body.get("api_key") or {}).get("id")
            if not isinstance(key_id, int):
                raise AssertionError(
                    f"pillar 17 mint response missing integer id; "
                    f"body keys={list(mint_body.keys())}"
                )

            # ---------- 2. DELETE the key (D5 path under test) ----------
            r_del = c.delete(
                f"/api/v1/admin/api-keys/{key_id}",
                headers=h(ak),
            )
            if r_del.status_code != 204:
                raise AssertionError(
                    f"pillar 17 delete failed: DELETE /api/v1/admin/api-keys/{key_id} "
                    f"returned {r_del.status_code}, expected 204. "
                    f"body={r_del.text[:300]}"
                )

            # ---------- 3. audit-log: assert exactly 1 deactivate row ----------
            r_audit = call(
                "GET",
                f"/api/v1/admin/audit-log?tenant_id={tid}&limit=200",
                ak,
                expect=(200, 404),
                client=c,
            )
            if r_audit.status_code == 404:
                # Lenient -- matches Pillar 7's posture. The HTTP layer
                # smoke-tested clean (mint 201, delete 204); audit
                # assertion skipped because /audit-log isn't mounted.
                return (
                    f"key_id={key_id} mint=201 delete=204; "
                    f"audit assertion skipped (/audit-log -> 404)"
                )

            rows = _extract_audit_rows(r_audit.json())
            deactivate_rows = [
                row for row in rows
                if (row.get("action") or "").lower() == "deactivate"
                and (row.get("resource_type") or "").lower() == "api_key"
                and row.get("resource_pk") == key_id
            ]
            if len(deactivate_rows) != 1:
                raise AssertionError(
                    f"D5 regression: expected exactly 1 audit row with "
                    f"action='deactivate' resource_type='api_key' "
                    f"resource_pk={key_id}, got {len(deactivate_rows)}. "
                    f"deactivate_key() may not be emitting via "
                    f"AdminAuditRepository.record() anymore."
                )

            # Bonus assertion: the create-side row has been emitted since
            # Step 24.5, so we should also see exactly 1 'create' row for
            # this key. Catches regressions in create_api_key's audit emit.
            create_rows = [
                row for row in rows
                if (row.get("action") or "").lower() == "create"
                and (row.get("resource_type") or "").lower() == "api_key"
                and row.get("resource_pk") == key_id
            ]
            if len(create_rows) != 1:
                raise AssertionError(
                    f"audit lifecycle gap: expected exactly 1 audit row "
                    f"with action='create' resource_type='api_key' "
                    f"resource_pk={key_id}, got {len(create_rows)}. "
                    f"create_api_key audit emit may have regressed."
                )

        return (
            f"key_id={key_id} mint=201 delete=204; "
            f"audit rows: 1 create + 1 deactivate (D5 holds)"
        )


PILLAR = ApiKeyDeactivateAuditPillar()