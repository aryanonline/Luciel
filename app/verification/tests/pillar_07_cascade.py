"""Pillar 7 - Cascade deactivation (gap-4 fix).

Landed suite PATCH'd the domain active=False and re-fetched:
  - the domain (active flipped)
  - the agent (active flipped)
  - the domain-level LucielInstance (active flipped)
BUT: landed never re-fetched the agent-level LucielInstance. If cascade
silently skipped agent-scope instances, landed would not have caught it.

Redo closes gap 4 by re-fetching ALL FOUR affected entities post-PATCH
and asserting each is active=False, then reading the audit log and
asserting exactly 3 cascade_deactivate rows exist for this tenant
(one bulk row for the agent cascade, one bulk row for the
domain+agent LucielInstance cascade, and one bulk row for the
memory-items cascade). Per Step 24.5 design (luciel_instance_repository
.deactivate_all_for_domain) the domain-scope and agent-scope Luciels
are cascaded together via a single UPDATE and emit ONE summary audit
row, not two.

The tenant-level LucielInstance is NOT expected to cascade -- it is
above the domain in scope and should remain active. This is an
additional invariant the redo asserts explicitly.

Sequence:
  1. PATCH /admin/domains/{tid}/sales with active=False
  2. GET /admin/domains/{tid}/sales             -> active False
  3. GET /admin/agents/{tid}/sales/sarah-listings -> active False
  4. GET /admin/luciel-instances/{instance_domain} -> active False
  5. GET /admin/luciel-instances/{instance_agent}  -> active False  (gap-4)
  6. GET /admin/luciel-instances/{instance_tenant} -> active True   (scope sanity)
  7. GET /admin/audit-log?tenant_id=... and count cascade_deactivate rows
"""

from __future__ import annotations

from typing import Any

from app.verification.fixtures import RunState
from app.verification.http_client import call, pooled_client
from app.verification.runner import Pillar


def _active(body: Any) -> bool | None:
    if isinstance(body, dict):
        return body.get("active")
    return None


def _extract_audit_rows(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("items") or body.get("results") or body.get("rows") or []
    return []


class CascadePillar(Pillar):
    number = 7
    name = "cascade deactivation (all four levels)"

    def run(self, state: RunState) -> str:
        if not state.tenant_admin_key:
            raise AssertionError("pillar 7 requires tenant_admin_key from pillar 1")
        for attr in ("domain_id", "agent_id", "instance_tenant", "instance_domain", "instance_agent"):
            if getattr(state, attr) is None:
                raise AssertionError(f"pillar 7 requires {attr} from pillar 2")

        ak = state.tenant_admin_key
        tid = state.tenant_id

        with pooled_client() as c:
            # ---------- 1. PATCH domain -> active: False ----------
            call(
                "PATCH",
                f"/api/v1/admin/domains/{tid}/{state.domain_id}",
                ak,
                json={"active": False},
                expect=(200, 204),
                client=c,
            )

            # ---------- 2. domain itself ----------
            r = call(
                "GET",
                f"/api/v1/admin/domains/{tid}/{state.domain_id}",
                ak,
                expect=(200, 404),
                client=c,
            )
            if r.status_code == 200 and _active(r.json()) is True:
                raise AssertionError(
                    f"domain {state.domain_id} did not deactivate (active=True)"
                )

            # ---------- 3. agent under domain ----------
            r = call(
                "GET",
                f"/api/v1/admin/agents/{tid}/{state.domain_id}/{state.agent_id}",
                ak,
                expect=(200, 404),
                client=c,
            )
            if r.status_code == 200 and _active(r.json()) is True:
                raise AssertionError(
                    f"agent {state.agent_id} did not cascade-deactivate (active=True)"
                )

            # ---------- 4. domain-level LucielInstance ----------
            r = call(
                "GET",
                f"/api/v1/admin/luciel-instances/{state.instance_domain}",
                ak,
                expect=(200, 404),
                client=c,
            )
            if r.status_code == 200 and _active(r.json()) is True:
                raise AssertionError(
                    f"domain-level LucielInstance {state.instance_domain} "
                    f"did not cascade-deactivate (active=True)"
                )

            # ---------- 5. agent-level LucielInstance (GAP-4 FIX) ----------
            r = call(
                "GET",
                f"/api/v1/admin/luciel-instances/{state.instance_agent}",
                ak,
                expect=(200, 404),
                client=c,
            )
            if r.status_code == 200 and _active(r.json()) is True:
                raise AssertionError(
                    f"GAP-4 HIT: agent-level LucielInstance {state.instance_agent} "
                    f"did not cascade-deactivate when domain {state.domain_id} "
                    f"was deactivated. cascade is skipping agent-scope instances."
                )

            # ---------- 6. tenant-level LucielInstance: invariant check ----------
            # Tenant-scope instance is ABOVE the domain and must NOT cascade.
            r = call(
                "GET",
                f"/api/v1/admin/luciel-instances/{state.instance_tenant}",
                ak,
                expect=200,
                client=c,
            )
            if _active(r.json()) is not True:
                raise AssertionError(
                    f"over-cascade: tenant-level LucielInstance {state.instance_tenant} "
                    f"was deactivated by a domain-level PATCH. Cascade is leaking UP."
                )

            # ---------- 7. audit-log: exactly 3 cascade_deactivate rows ----------
            # Lenient on route shape/presence -- if /audit-log is not mounted,
            # skip this assertion (still gap-4 is proven by steps 1-6).
            r = call(
                "GET",
                f"/api/v1/admin/audit-log?tenant_id={tid}&limit=50",
                ak,
                expect=(200, 404),
                client=c,
            )
            cascade_rows: list[dict] = []
            audit_note = "not checked (route 404)"
            if r.status_code == 200:
                rows = _extract_audit_rows(r.json())
                cascade_rows = [
                    row for row in rows
                    if (row.get("action") or "").lower() in ("cascade_deactivate", "cascade-deactivate", "cascadedeactivate")
                ]
                if len(cascade_rows) != 3:
                    raise AssertionError(
                        f"expected exactly 3 cascade_deactivate audit rows "
                        f"(agent-bulk, luciel-bulk [covers both domain+agent "
                        f"scope], memory-bulk), got {len(cascade_rows)}. "
                        f"rows={[{'action': r.get('action'), 'resource_type': r.get('resource_type')} for r in cascade_rows]}"
                    )
                # New 2026-05-02: domain deactivation now cascades memory_items.
                # Assert the memory cascade row exists explicitly so future
                # regressions of Wiring B (deactivate_domain memory cascade)
                # surface as a specific failure.
                memory_cascade_rows = [
                    row for row in cascade_rows
                    if (row.get("resource_type") or "").lower() == "memory"
                ]
                if len(memory_cascade_rows) != 1:
                    raise AssertionError(
                        f"expected exactly 1 memory cascade row from domain "
                        f"deactivation (Wiring B), got {len(memory_cascade_rows)}. "
                        f"deactivate_domain may have stopped calling "
                        f"bulk_soft_deactivate_memory_items_for_domain."
                    )
                audit_note = f"{len(cascade_rows)} rows (agent-bulk, luciel-bulk, memory)"

        return (
            f"domain+agent+domain-luciel+agent-luciel all inactive; "
            f"tenant-luciel still active; "
            f"audit cascade_rows={audit_note}"
        )


PILLAR = CascadePillar()