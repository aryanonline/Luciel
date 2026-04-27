"""Pillar 15 - Consent route resolves at /api/v1/consent (D16 regression guard).

Drift item D16 from Step 24.5b canonical recap, originally seeded in Step 26b
backlog. The consent router historically declared prefix="/api/v1/consent"
(absolute) while being mounted under api_router which already adds /api/v1,
producing /api/v1/api/v1/consent/grant|withdraw|status. fix(28) Commit 4
changed the prefix to "/consent" to match every other v1 router.

Asserts:
  1. POST /api/v1/consent/grant resolves (status != 404). The actual status
     may be 401/403/422 depending on auth/payload validity, but the route
     must EXIST. A 404 means the prefix is wrong.
  2. POST /api/v1/api/v1/consent/grant returns 404 (regression guard). If
     this ever returns non-404, the double-prefix bug is back.

Uses the pre-existing tenant_admin_key from pillar 1 for auth, and a minimal
payload. Behavioral correctness of consent grant/withdraw/status is out of
scope here -- this pillar exclusively guards the route-resolution contract.
"""

from __future__ import annotations

from app.verification.fixtures import RunState
from app.verification.http_client import h, pooled_client
from app.verification.runner import Pillar


class ConsentRouteNoDoublePrefixPillar(Pillar):
    number = 15
    name = "consent route no double prefix (D16)"

    def run(self, state: RunState) -> str:
        if not state.tenant_admin_key:
            raise AssertionError("pillar 15 requires tenant_admin_key from pillar 1")
        if not state.tenant_id:
            raise AssertionError("pillar 15 requires tenant_id from pillar 1")

        ak = state.tenant_admin_key
        tid = state.tenant_id
        payload = {
            "user_id": f"step28-d16-pillar15-{tid[-8:]}",
            "tenant_id": tid,
            "consent_given": True,
        }

        with pooled_client() as c:
            # 1. Correct path must resolve (any non-404 is acceptable).
            r_correct = c.post(
                "/api/v1/consent/grant",
                headers=h(ak),
                json=payload,
            )
            if r_correct.status_code == 404:
                raise AssertionError(
                    "consent route /api/v1/consent/grant returned 404 -- "
                    "prefix is wrong (D16 regression). "
                    f"body={r_correct.text[:200]}"
                )

            # 2. Double-prefix path must NOT resolve.
            r_double = c.post(
                "/api/v1/api/v1/consent/grant",
                headers=h(ak),
                json=payload,
            )
            if r_double.status_code != 404:
                raise AssertionError(
                    "double-prefix path /api/v1/api/v1/consent/grant "
                    f"returned {r_double.status_code}, expected 404. "
                    "D16 has regressed -- consent router prefix is "
                    "absolute again. "
                    f"body={r_double.text[:200]}"
                )

        return (
            f"correct=/api/v1/consent/grant -> {r_correct.status_code} (resolved), "
            f"double=/api/v1/api/v1/consent/grant -> 404 (guarded)"
        )


PILLAR = ConsentRouteNoDoublePrefixPillar()