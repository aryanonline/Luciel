"""Pillar 24 - Forensic toggle route is the contract (Step 29.x).

# Why this pillar exists

Step 29 Commit C.5 introduced the first MUTATION on the Step 29 forensic
plane: a platform_admin POST at

    /api/v1/admin/forensics/luciel_instances_step29c/{instance_id}/toggle_active

P11 F10 EXERCISES this route in production every run (deactivate to set up
the worker-instance-deactivated probe; restore in the finally block). That
gives us four free signals already:

  - 200 + payload shape  (call(..., expect=200))
  - active flag really flipped on the wire
  - WORKER_INSTANCE_DEACTIVATED audit row materializes within 10s
  - happy-path 200 status from a platform_admin caller

What P11 F10 does NOT cover, and what makes this surface a real risk for
a future regression:

  G1. AUTHZ: A non-platform-admin caller (e.g. a tenant-admin key for the
      instance's own tenant) MUST get 403. Today the route guard is
      `_require_platform_admin_step29c()`. If a future commit drops or
      relaxes that gate, P11 F10 would still pass (it always uses the
      platform_admin key). The whole forensic plane's authz contract
      would silently degrade to "any authenticated caller can flip
      luciel_instances.active" -- a critical privilege-escalation path
      because a deactivated LucielInstance kills the worker pipeline for
      that scope.

  G2. AUDIT EMISSION: The route's atomic invariant is "audit row is
      flushed BEFORE the conditional UPDATE; both commit in one
      transaction so a failed audit insert prevents the mutation." If
      a future commit moves `audit_repo.record(..., autocommit=False)`
      AFTER the mutation, or skips it on the no-op branch, the route
      still returns 200 and P11 F10 still passes (P11 F10 only watches
      for the WORKER_INSTANCE_DEACTIVATED row, which is emitted by the
      celery worker, NOT by this route). The route's own audit row
      (action=`luciel_instance_forensic_toggle`) would silently vanish.
      We assert it is present after P11 F10 ran, with the correct
      payload shape (after_json.active matches the toggle direction).

  G3. NO-OP IDEMPOTENCY: The route deliberately writes an audit row
      EVERY call but skips the SQL UPDATE when requested == previous.
      That is a load-bearing semantic: it gives forensic auditors a
      complete trail of intent (every call is logged) without polluting
      `updated_at` for state-preserving calls (so dashboards that show
      "instance last touched at" reflect actual state changes, not
      every dry-run audit). If a future commit removes the
      `if requested_active != previous_active:` guard, every no-op
      call would bump `updated_at` and falsely advertise a state
      change that did not happen. We assert: two consecutive
      same-value calls each produce a new audit row but do NOT advance
      `updated_at` between them.

# Scope and ordering

Soft order dependency on Pillar 11. Reads:
  - state.tenant_id           (set by P1)
  - state.tenant_admin_key    (set by P1) -- G1 negative test
  - state.instance_agent      (set by P2; P11 F10 left active=True)
  - state.platform_admin_key  (env-loaded)

Runs read-only on the API surface for G1; writes 2 no-op audit rows for
G3 (both with active=current_value, so semantically a noop write pair).
Teardown handles audit-log cleanup at the tenant level -- no per-pillar
sweep needed.

# Why a direct DB read for updated_at

The C.1 forensic projection `LucielInstanceForensic` deliberately omits
`updated_at` (the harness has not previously needed it; adding it now
is a route-surface change that should NOT be entangled with adding the
regression guard for the existing route). For G3 we read `updated_at`
directly with the same DB-introspection pattern P22 uses
(`create_engine(DATABASE_URL)` against the worker DSN). Read-only,
single column, single row -- no privilege escalation.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import create_engine, text

from app.verification.fixtures import RunState
from app.verification.http_client import call, pooled_client
from app.verification.runner import Pillar


_TOGGLE_ACTION = "luciel_instance_forensic_toggle"
_FORENSIC_AUDIT_LOG_PATH = "/api/v1/admin/forensics/admin_audit_logs_step29c"


def _toggle_path(instance_id: int) -> str:
    return (
        f"/api/v1/admin/forensics/luciel_instances_step29c/"
        f"{instance_id}/toggle_active"
    )


def _instance_get_path(instance_id: int) -> str:
    return (
        f"/api/v1/admin/forensics/luciel_instances_step29c/{instance_id}"
    )


class LucielInstanceForensicTogglePillar(Pillar):
    number = 24
    name = "luciel_instance forensic toggle route is the contract"

    def run(self, state: RunState) -> str:
        # Preconditions: prior pillars must have populated these. We
        # raise with a precise message instead of letting a None propagate
        # into a confusing TypeError on the URL build.
        if not state.tenant_id:
            raise AssertionError("P24 needs state.tenant_id (set by P1)")
        if not state.tenant_admin_key:
            raise AssertionError(
                "P24 needs state.tenant_admin_key for G1 negative test (set by P1)"
            )
        if state.instance_agent is None:
            raise AssertionError(
                "P24 needs state.instance_agent (set by P2; "
                "P11 F10 leaves active=True at exit)"
            )
        if not state.platform_admin_key:
            raise AssertionError(
                "P24 needs state.platform_admin_key (env-loaded)"
            )

        instance_id = state.instance_agent
        results: list[str] = []

        # ----------------------------------------------------------------
        # G1. AUTHZ: tenant-admin key MUST get 403 from the toggle route.
        #
        # We do NOT need to inspect the body; the status-code allowlist
        # in call() raises AssertionError on anything outside {403}, which
        # is exactly the failure mode we want to surface if the gate is
        # ever relaxed (200 = privilege escalation; 401 = wrong gate
        # rejected the key for a non-authz reason; 422 = body shape
        # changed). We send a body shape that WOULD be valid for a real
        # call so the only reason for rejection is the authz gate.
        # ----------------------------------------------------------------
        with pooled_client() as c:
            call(
                "POST",
                _toggle_path(instance_id),
                state.tenant_admin_key,
                json={"active": False},
                expect=403,
                client=c,
            )
        results.append("G1 tenant-admin key got 403 from toggle route")

        # ----------------------------------------------------------------
        # G2. AUDIT EMISSION: P11 F10 has already toggled this instance
        # twice (deactivate, then restore in finally). Both calls MUST
        # have produced an `luciel_instance_forensic_toggle` audit row
        # with after_json.active matching the requested direction.
        #
        # Filter by (tenant_id, action) and limit=10 to be resilient to
        # any future pillar that also exercises this route in the same
        # tenant. We then narrow to rows whose luciel_instance_id ==
        # our instance_agent and assert >= 2 such rows.
        # ----------------------------------------------------------------
        with pooled_client() as c:
            r = call(
                "GET",
                _FORENSIC_AUDIT_LOG_PATH,
                state.platform_admin_key,
                params={
                    "tenant_id": state.tenant_id,
                    "action": _TOGGLE_ACTION,
                    "limit": 10,
                },
                expect=200,
                client=c,
            )
        all_rows: list[dict[str, Any]] = r.json().get("rows") or []
        rows = [
            row for row in all_rows
            if row.get("luciel_instance_id") == instance_id
        ]
        if len(rows) < 2:
            raise AssertionError(
                f"G2 expected >=2 {_TOGGLE_ACTION} audit rows for "
                f"instance_agent={instance_id} after P11 F10; got "
                f"{len(rows)} (total rows for tenant: {len(all_rows)})"
            )
        # Audit rows come back ordered by created_at DESC at the route
        # (standard pattern for admin_audit_logs_step29c). The OLDEST
        # P11-F10 entry (deactivate -> active=False) is rows[-1]; the
        # NEWEST (restore -> active=True) is rows[0]. We assert payload
        # SHAPE (after_json carries an `active` bool), not the specific
        # direction, because a future P11 F10 reorder must not silently
        # break this pillar -- the contract being guarded here is "the
        # route emits an audit row with after_json={'active': bool}",
        # which is direction-agnostic.
        for row in rows[:2]:
            after_json = row.get("after_json")
            if not isinstance(after_json, dict) or "active" not in after_json:
                raise AssertionError(
                    f"G2 audit row id={row.get('id')} has malformed "
                    f"after_json={after_json!r}; expected dict with "
                    f"'active' key. Route may have stopped emitting the "
                    f"after-payload."
                )
            if not isinstance(after_json["active"], bool):
                raise AssertionError(
                    f"G2 audit row id={row.get('id')} has non-bool "
                    f"after_json['active']={after_json['active']!r}"
                )
        results.append(
            f"G2 route emits audit rows ({len(rows)} found, payload shape ok)"
        )

        # ----------------------------------------------------------------
        # G3. NO-OP IDEMPOTENCY: read current `active` and `updated_at`,
        # POST {"active": current} twice, and assert:
        #   - both calls return 200
        #   - 2 new audit rows appeared (one per call)
        #   - `updated_at` did NOT advance between the two calls (the
        #     no-op branch was taken; SQL UPDATE was skipped)
        #
        # The forensic projection lacks `updated_at`, so the third
        # assertion uses a direct read against the DB (same pattern as
        # P22's grants probe; read-only, single column).
        # ----------------------------------------------------------------
        with pooled_client() as c:
            r = call(
                "GET",
                _instance_get_path(instance_id),
                state.platform_admin_key,
                expect=200,
                client=c,
            )
        current_active = bool(r.json().get("active"))

        # Snapshot updated_at BEFORE the no-op pair.
        updated_at_before = self._read_updated_at(instance_id)

        # Snapshot the audit row count for this (instance, action) pair
        # BEFORE the no-op pair so we can assert exactly +2 after.
        audit_count_before = self._count_toggle_rows(
            tenant_id=state.tenant_id,
            instance_id=instance_id,
            platform_admin_key=state.platform_admin_key,
        )

        # Two consecutive no-op writes.
        for i in range(2):
            with pooled_client() as c:
                r = call(
                    "POST",
                    _toggle_path(instance_id),
                    state.platform_admin_key,
                    json={"active": current_active},
                    expect=200,
                    client=c,
                )
            body_active = bool(r.json().get("active"))
            if body_active is not current_active:
                raise AssertionError(
                    f"G3 no-op call {i+1} returned active={body_active!r}, "
                    f"expected {current_active!r} (the route should be "
                    f"idempotent on same-value writes)"
                )

        updated_at_after = self._read_updated_at(instance_id)
        audit_count_after = self._count_toggle_rows(
            tenant_id=state.tenant_id,
            instance_id=instance_id,
            platform_admin_key=state.platform_admin_key,
        )

        if updated_at_after != updated_at_before:
            raise AssertionError(
                f"G3 updated_at advanced across two no-op writes: "
                f"before={updated_at_before!r} after={updated_at_after!r}. "
                f"The `if requested_active != previous_active:` guard is "
                f"missing -- no-op writes are doing real SQL UPDATEs."
            )

        new_audit_rows = audit_count_after - audit_count_before
        if new_audit_rows != 2:
            raise AssertionError(
                f"G3 expected exactly 2 new {_TOGGLE_ACTION} audit rows "
                f"after two no-op writes; got {new_audit_rows} "
                f"(before={audit_count_before}, after={audit_count_after}). "
                f"Route may have stopped emitting audit on the no-op branch."
            )
        results.append(
            "G3 no-op idempotency: 2 audit rows emitted, updated_at unchanged"
        )

        return " ; ".join(results)

    # ---- helpers ------------------------------------------------------

    @staticmethod
    def _count_toggle_rows(
        *, tenant_id: str, instance_id: int, platform_admin_key: str
    ) -> int:
        """Count `luciel_instance_forensic_toggle` audit rows for one
        instance via the forensic GET. limit=100 is the route default
        cap; >100 toggle rows for a throwaway tenant in a single run
        would itself indicate a bug, so the cap is acceptable here."""
        with pooled_client() as c:
            r = call(
                "GET",
                _FORENSIC_AUDIT_LOG_PATH,
                platform_admin_key,
                params={
                    "tenant_id": tenant_id,
                    "action": _TOGGLE_ACTION,
                    "limit": 100,
                },
                expect=200,
                client=c,
            )
        rows = r.json().get("rows") or []
        return sum(
            1 for row in rows
            if row.get("luciel_instance_id") == instance_id
        )

    @staticmethod
    def _read_updated_at(instance_id: int):
        """Direct DB read of luciel_instances.updated_at. Read-only,
        single column. Mirrors P22's _load_database_url_from_dotenv
        fallback so this works in local dev too."""
        db_url = (
            os.environ.get("DATABASE_URL")
            or LucielInstanceForensicTogglePillar._load_database_url_from_dotenv()
        )
        if not db_url:
            raise AssertionError(
                "P24 G3 needs DATABASE_URL (env or .env) to observe "
                "luciel_instances.updated_at; the forensic projection "
                "does not include it."
            )
        engine = create_engine(db_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT updated_at FROM luciel_instances WHERE id = :id"
                    ),
                    {"id": instance_id},
                ).one_or_none()
            if row is None:
                raise AssertionError(
                    f"P24 G3 luciel_instances row id={instance_id} not "
                    f"found in DB (expected to exist after P2 setup)"
                )
            return row.updated_at
        finally:
            engine.dispose()

    @staticmethod
    def _load_database_url_from_dotenv() -> str | None:
        """Walk up from CWD looking for a .env; return DATABASE_URL if
        present. Copied from P22 verbatim to avoid a cross-pillar
        import that would entangle test ordering."""
        from pathlib import Path
        here = Path.cwd().resolve()
        for candidate_dir in (here, *here.parents):
            env_path = candidate_dir / ".env"
            if env_path.is_file():
                try:
                    for raw in env_path.read_text(encoding="utf-8").splitlines():
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("DATABASE_URL=") and "://" in line:
                            val = line.split("=", 1)[1].strip()
                            if (val.startswith('"') and val.endswith('"')) or (
                                val.startswith("'") and val.endswith("'")
                            ):
                                val = val[1:-1]
                            return val
                except Exception:
                    continue
        return None


PILLAR = LucielInstanceForensicTogglePillar()
