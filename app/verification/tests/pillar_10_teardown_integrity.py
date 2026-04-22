"""Pillar 10 - Teardown integrity (Step 26b.2 rewrite).

Step 26-redo landed Pillar 10 as a subprocess-spawned raw-SQL probe that
read DATABASE_URL from env. That worked against local (DATABASE_URL defaults
to local postgres via .env) but failed silently against prod: when running
against api.vantagemind.ai, the subprocess fell back to local DATABASE_URL
and saw 0 rows for a tenant that was actually created on prod.

Step 26b.2 replaces the subprocess with an HTTPS call to the new admin
endpoint GET /api/v1/admin/verification/teardown-integrity?tenant_id=X.
The endpoint runs the exact same probe logic server-side, so the caller
inherits whatever DB the server is connected to -- no env-var drift between
local-suite and remote-target environments.

Same assertion invariant as before: this run's throwaway tenant leaves no
active residue on the stack. Now enforced uniformly regardless of target.
"""

from __future__ import annotations

import json

from app.verification.fixtures import RunState
from app.verification.http_client import call, pooled_client
from app.verification.runner import Pillar


class TeardownIntegrityPillar(Pillar):
    number = 10
    name = "teardown integrity (zero residue for this tenant)"

    def run(self, state: RunState) -> str:
        if not state.tenant_id:
            raise AssertionError("pillar 10 requires tenant_id from RunState")

        # Single HTTPS call. Same pattern as pillars 1-8.
        # The endpoint is platform-admin-only; RunState.platform_admin_key
        # was loaded at suite startup from LUCIEL_PLATFORM_ADMIN_KEY.
        with pooled_client() as c:
            r = call(
                "GET",
                f"/api/v1/admin/verification/teardown-integrity?tenant_id={state.tenant_id}",
                state.platform_admin_key,
                expect=200,
                client=c,
            )

        rpt = r.json()
        violations = rpt.get("violations", [])
        observations = rpt.get("observations", {})

        if violations:
            raise AssertionError(
                f"teardown integrity violation: body={json.dumps(rpt)[:1500]}"
            )

        # PASS -- return concise summary matching prior format
        tc = observations.get("tenant_configs", {}) or {}
        ak = observations.get("api_keys", {}) or {}
        li = observations.get("luciel_instances", {}) or {}

        return (
            f"tenant={rpt.get('target_tenant')}: "
            f"tenant_configs total={tc.get('total')} live={tc.get('live')}; "
            f"luciel_instances live={li.get('live')}; "
            f"api_keys live={ak.get('live')}; "
            f"zero residue"
        )


PILLAR = TeardownIntegrityPillar()