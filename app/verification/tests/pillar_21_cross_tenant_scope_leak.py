"""Pillar 21 - Cross-tenant scope-leak fuzz suite (P3-D).

Step 28 Phase 3 - C4. Resolves PHASE_3_COMPLIANCE_BACKLOG P3-D.

# Why this pillar exists

For a brokerage-grade business, the worst-case privacy breach is
cross-tenant data leakage: a token bound to tenant A reads or
mutates tenant B's resources. That violates PIPEDA P5 (technical
safeguards), SOC 2 CC6.1 (logical access controls), and is the
single most likely thing to end the business if it happens.

The codebase already has scope enforcement at every layer:
ScopePolicy.enforce_* at the API boundary, repository-level
WHERE clauses scoped by tenant_id, audit-log filtering. But we
have no SYSTEMATIC regression guard that fires if a future commit
introduces a scope-leak bug -- a forgotten enforce_* call, a
WHERE-clause omission, an IDOR-style integer-id endpoint that
isn't tenant-scoped, or a list endpoint that trusts a
caller-supplied tenant_id query param.

Pillar 21 builds that systematic guard. It provisions two
independent throwaway tenants -- "victim" and "attacker" -- mints
real resources (api_key, luciel_instance, agent) inside the
victim, then uses the attacker's admin key to attempt cross-
tenant access against the victim's resources via every shape of
attack vector: path-tenant, ID-only IDOR, tenant_id query param,
and body-tenant POST.

# What "scope leak" means here

For each fuzz case, we issue an HTTP call from the attacker's
token against a victim resource (or with a body claiming the
victim's tenant_id) and check the response status:

  - 403 Forbidden  -> PASS (scope policy fired)
  - 404 Not Found  -> PASS (resource invisible to attacker, also OK)
  - 405 Method Not Allowed -> PASS (route doesn't exist, vacuous)
  - 422 Unprocessable -> PASS (schema rejected before scope check;
                                 not a leak, just a weaker layer
                                 firing first)
  - 200 OK         -> FAIL  (attacker received victim's data --
                              this is the regression we hunt)
  - 500 Internal   -> FAIL  (unhandled exception masks scope intent
                              and indicates broken error handling)
  - 2xx other      -> FAIL  (resource was created/mutated cross-
                              tenant -- worst case)

For list endpoints, PASS requires the response to contain ZERO
items whose tenant_id matches the victim, regardless of status
code (the attacker's caller-supplied tenant_id query param must
NOT bypass scope filtering).

# Coverage matrix

The fuzz cases below span the four main scope-leak surfaces.

  Path-tenant cases (URL has {tenant_id}):
    - GET    /tenants/{victim}
    - PATCH  /tenants/{victim}
    - GET    /domains/{victim}/general
    - PATCH  /domains/{victim}/general
    - DELETE /domains/{victim}/general
    - GET    /agents/{victim}/{victim_agent}
    - PATCH  /agents/{victim}/{victim_agent}
    - DELETE /agents/{victim}/{victim_agent}
    - POST   /agents/{victim}/{victim_agent}/bind-user

  ID-only IDOR cases (path has integer pk only, no tenant_id):
    - DELETE /api-keys/{victim_key_id}
    - GET    /luciel-instances/{victim_luciel_pk}
    - PATCH  /luciel-instances/{victim_luciel_pk}
    - DELETE /luciel-instances/{victim_luciel_pk}
    - GET    /luciel-instances/{victim_luciel_pk}/knowledge
    - GET    /luciel-instances/{victim_luciel_pk}/chunking-config

  Query-param tenant cases (?tenant_id=victim):
    - GET    /api-keys?tenant_id=victim
    - GET    /tenants                 (list -- scope-filter)
    - GET    /domains?tenant_id=victim
    - GET    /memory-items?tenant_id=victim
    - GET    /luciel-instances?tenant_id=victim
    - GET    /agents?tenant_id=victim

  Body-tenant cases (POST body claims tenant_id=victim):
    - POST   /tenants            body.tenant_id = victim_tenant
    - POST   /domains            body.tenant_id = victim_tenant
    - POST   /api-keys           body.tenant_id = victim_tenant
    - POST   /agents             body.tenant_id = victim_tenant
    - POST   /knowledge/ingest   body.tenant_id = victim_tenant
    - POST   /luciel-instances   body.scope_owner_tenant_id = victim
    - POST   /scope-assignments  body.tenant_id = victim_tenant

# Failure mode

On any fuzz-case regression, the pillar raises AssertionError with
the FULL list of failed cases (case_id, attack_vector, method,
path, observed_status, body_excerpt). One run = one full report.
This means a single run pinpoints the exact endpoint that
regressed, no bisection needed.

# Why both attacker AND victim are throwaway tenants

Pillar 21 deliberately does NOT use state.tenant_id (Pillar 1's
victim). Two reasons:

  1. Ordering independence -- Pillar 21 runs even if Pillar 1
     fails (run-all-then-report). Sourcing both tenants from the
     pillar's own setup means the fuzz harness is hermetic.
  2. Symmetry -- using two FRESH tenants ensures NEITHER has
     any prior platform-admin shortcuts (no SSM bootstrap key
     overlap, no audit-log noise from earlier pillars).

# Self-cleanup

The pillar deactivates both tenants in a finally: block (best-
effort PATCH active=false; matches Pillar 13's teardown shape).
Fuzz attempts that succeeded in MUTATING victim state would
indicate a scope leak -- but since the pillar fails loudly in
that case, teardown of victim is academic; we still PATCH it
inactive so residue sweeper has less work.

After C4 lands, total pillar count goes 20 -> 21 GREEN.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from app.verification.fixtures import RunState
from app.verification.http_client import BASE_URL, REQUEST_TIMEOUT, h, pooled_client
from app.verification.runner import Pillar


P21_VICTIM_PREFIX = "step28-p21-victim-"
P21_ATTACKER_PREFIX = "step28-p21-attacker-"


# Status-code policy for the four attack-vector classes.
#
# 403/404/405/422 are all "policy fired or layered defense reduced
# the attack surface vacuously" -- none of them are leaks.
# 200 with cross-tenant data is the regression.
# 500 is suspect (masks intent) and we treat as failure.
# 2xx other than 200 (e.g. 201 from a body-tenant POST that
# actually created the resource cross-tenant) is the WORST case
# and obviously fails.
#
# We do NOT rely on a single allowlist tuple because list
# endpoints (query-param vector) need a different rule: 200 is
# OK as long as the response contains no victim items.
SAFE_DENY_CODES = (403, 404, 405, 422)
SUSPECT_CODES = (500, 502, 503, 504)


def _victim_tid() -> str:
    return f"{P21_VICTIM_PREFIX}{uuid.uuid4().hex[:8]}"


def _attacker_tid() -> str:
    return f"{P21_ATTACKER_PREFIX}{uuid.uuid4().hex[:8]}"


class FuzzCase:
    """One scope-leak fuzz case: an HTTP attempt with a verdict rule.

    ``vector`` is one of {"path", "idor", "query", "body"} -- used
    in the failure detail to make regressions self-categorizing.

    ``method`` and ``path`` are the request line; ``json_body`` is
    the optional request body (for POST/PATCH/PUT). ``token`` is
    the bearer to use (always the attacker's admin key for this
    pillar).

    ``verdict_fn(status, body) -> (passed, reason)`` is the rule
    that decides whether this case PASSED. Default rule:
    status in SAFE_DENY_CODES -> pass; else fail. List endpoints
    pass a custom verdict_fn that also inspects the body.
    """

    def __init__(
        self,
        case_id: str,
        vector: str,
        method: str,
        path: str,
        token: str,
        *,
        json_body: dict[str, Any] | None = None,
        verdict_fn=None,
    ) -> None:
        self.case_id = case_id
        self.vector = vector
        self.method = method
        self.path = path
        self.token = token
        self.json_body = json_body
        self.verdict_fn = verdict_fn or self._default_verdict

    @staticmethod
    def _default_verdict(status: int, body_text: str) -> tuple[bool, str]:
        if status in SAFE_DENY_CODES:
            return True, f"deny code {status}"
        if status in SUSPECT_CODES:
            return False, f"suspect/500-class status {status}"
        if 200 <= status < 300:
            return False, f"2xx leak: status {status}"
        # Any other 4xx (e.g. 400, 401, 415) -- treat as PASS,
        # because the request never produced victim data. We log
        # the code in detail so unusual responses are visible.
        if 400 <= status < 500:
            return True, f"4xx deny {status}"
        return False, f"unexpected status {status}"

    def execute(self, client: httpx.Client) -> tuple[bool, str, int]:
        """Run the case. Return (passed, reason, status_code)."""
        try:
            resp = client.request(
                self.method,
                self.path,
                headers=h(self.token),
                json=self.json_body,
            )
        except Exception as exc:
            return False, f"network error: {exc!r}", 0
        # Full body must be handed to the verdict function so that JSON
        # parsers in list-verdicts can inspect every item. Truncation is
        # for failure-message formatting only, never for verdict input.
        passed, reason = self.verdict_fn(resp.status_code, resp.text)
        return passed, reason, resp.status_code


def _list_verdict_no_victim_items(victim_tenant_id: str):
    """Verdict for list endpoints: 200 OK is fine IFF response
    contains no items belonging to the victim tenant.

    Specifically:
      - 200 + body has any item with tenant_id == victim -> FAIL
      - 200 + body has zero victim items                  -> PASS
      - 403/404/422                                        -> PASS
      - 500                                                -> FAIL

    Body shape varies by endpoint -- some return a flat list,
    others wrap in {items: [...]} or {results: [...]}.
    """
    def _verdict(status: int, body_text: str) -> tuple[bool, str]:
        if status in SAFE_DENY_CODES:
            return True, f"deny code {status}"
        if status in SUSPECT_CODES:
            return False, f"suspect/500-class status {status}"
        if status != 200:
            # 4xx other than the safe set: treat as PASS (we never
            # got victim data) but flag in the reason.
            return True, f"non-200 deny {status}"
        # 200 -- inspect body for victim-tenant items.
        import json as _json
        try:
            body = _json.loads(body_text)
        except Exception:
            # If we cannot parse, we cannot prove no leak. FAIL
            # loudly rather than silently passing.
            return False, f"200 with unparseable body: {body_text[:120]!r}"
        items: list[Any]
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            items = (
                body.get("items")
                or body.get("results")
                or body.get("rows")
                or []
            )
        else:
            items = []
        leaked = [
            it for it in items
            if isinstance(it, dict) and it.get("tenant_id") == victim_tenant_id
        ]
        if leaked:
            return False, (
                f"200 list leaked {len(leaked)} victim items "
                f"(first: {str(leaked[0])[:120]!r})"
            )
        return True, f"200 list, 0 victim items in {len(items)} returned"
    return _verdict


def _onboard_throwaway(c: httpx.Client, platform_admin_key: str, tid: str, label: str) -> str:
    """Onboard a fresh tenant via platform-admin; return the
    raw admin key for that tenant. Raises on failure."""
    body = {
        "tenant_id": tid,
        "display_name": f"Pillar 21 {label} {tid[-6:]}",
        "admin_display_name": f"p21-{label}-admin",
    }
    r = c.post(
        "/api/v1/admin/tenants/onboard",
        headers=h(platform_admin_key),
        json=body,
    )
    if r.status_code not in (200, 201):
        raise AssertionError(
            f"pillar 21 setup: onboard {label} tenant {tid} returned "
            f"{r.status_code}; body={r.text[:300]}"
        )
    j = r.json()
    admin_blob = j.get("admin_api_key") or j.get("admin_key") or {}
    if isinstance(admin_blob, dict):
        ak = admin_blob.get("raw_key") or admin_blob.get("key")
    else:
        ak = admin_blob
    if not isinstance(ak, str) or len(ak) < 20:
        raise AssertionError(
            f"pillar 21 setup: onboard {label} did not return usable "
            f"admin_api_key; got type={type(ak).__name__} keys={sorted(j.keys())}"
        )
    return ak


def _mint_victim_resources(
    c: httpx.Client,
    pa: str,
    victim_admin: str,
    victim_tenant: str,
) -> dict[str, Any]:
    """Mint a small set of real resources INSIDE the victim tenant
    so the fuzz cases have actual cross-tenant targets to attempt
    access against. Uses the victim's own admin key (not the
    platform_admin) so resources are properly scoped.

    Returns: {agent_id, api_key_id, luciel_pk}.

    Raises if any setup step fails -- the pillar cannot run without
    real victim resources.
    """
    out: dict[str, Any] = {}

    # 1. Mint a victim-side agent under the auto-created 'general'
    #    domain. We need a concrete agent_id slug for the path-
    #    tenant fuzz cases on /agents/{tenant}/{agent}.
    agent_slug = f"p21vic-{uuid.uuid4().hex[:6]}"
    r = c.post(
        "/api/v1/admin/agents",
        headers=h(victim_admin),
        json={
            "tenant_id": victim_tenant,
            "domain_id": "general",
            "agent_id": agent_slug,
            "display_name": "Pillar 21 victim agent",
        },
    )
    if r.status_code not in (200, 201):
        raise AssertionError(
            f"pillar 21 setup: mint victim agent failed: "
            f"{r.status_code} {r.text[:300]}"
        )
    out["agent_id"] = agent_slug

    # 2. Mint a victim-side luciel-instance at tenant scope. Its
    #    integer pk drives the IDOR fuzz cases on
    #    /luciel-instances/{pk}*.
    luciel_slug = f"p21vic-luc-{uuid.uuid4().hex[:6]}"
    r = c.post(
        "/api/v1/admin/luciel-instances",
        headers=h(victim_admin),
        json={
            "instance_id": luciel_slug,
            "display_name": "Pillar 21 victim luciel",
            "scope_level": "tenant",
            "scope_owner_tenant_id": victim_tenant,
        },
    )
    if r.status_code not in (200, 201):
        raise AssertionError(
            f"pillar 21 setup: mint victim luciel-instance failed: "
            f"{r.status_code} {r.text[:300]}"
        )
    luciel_body = r.json()
    luciel_pk = luciel_body.get("id") or luciel_body.get("pk")
    if not isinstance(luciel_pk, int):
        raise AssertionError(
            f"pillar 21 setup: victim luciel-instance response missing "
            f"integer id; got {luciel_body!r}"
        )
    out["luciel_pk"] = luciel_pk

    # 3. Mint a victim-side api_key. Its integer id drives the
    #    DELETE /api-keys/{key_id} IDOR fuzz case.
    r = c.post(
        "/api/v1/admin/api-keys",
        headers=h(victim_admin),
        json={
            "tenant_id": victim_tenant,
            "display_name": "Pillar 21 victim probe key",
            "permissions": ["chat", "sessions"],
            "rate_limit": 10,
        },
    )
    if r.status_code not in (200, 201):
        raise AssertionError(
            f"pillar 21 setup: mint victim api-key failed: "
            f"{r.status_code} {r.text[:300]}"
        )
    api_body = r.json()
    api_blob = api_body.get("api_key") or api_body
    api_key_id = (
        api_blob.get("id") if isinstance(api_blob, dict) else None
    )
    if not isinstance(api_key_id, int):
        raise AssertionError(
            f"pillar 21 setup: victim api-key response missing "
            f"integer id; got {api_body!r}"
        )
    out["api_key_id"] = api_key_id

    return out


def _build_fuzz_cases(
    *,
    attacker_admin: str,
    attacker_tenant: str,
    victim_tenant: str,
    victim_agent_id: str,
    victim_luciel_pk: int,
    victim_api_key_id: int,
) -> list[FuzzCase]:
    """Construct the comprehensive fuzz case matrix.

    Every case uses the attacker's admin key against a victim
    resource (or a body-tenant claim against the victim's tenant
    id). Cases are grouped by attack vector for readability; the
    runtime treats them uniformly.
    """
    list_v = _list_verdict_no_victim_items(victim_tenant)

    cases: list[FuzzCase] = []

    # ---- Path-tenant vector ----
    cases += [
        FuzzCase("path.tenant.get",      "path", "GET",
                 f"/api/v1/admin/tenants/{victim_tenant}", attacker_admin),
        FuzzCase("path.tenant.patch",    "path", "PATCH",
                 f"/api/v1/admin/tenants/{victim_tenant}", attacker_admin,
                 json_body={"display_name": "p21 attacker mutation attempt"}),
        FuzzCase("path.domain.get",      "path", "GET",
                 f"/api/v1/admin/domains/{victim_tenant}/general", attacker_admin),
        FuzzCase("path.domain.patch",    "path", "PATCH",
                 f"/api/v1/admin/domains/{victim_tenant}/general", attacker_admin,
                 json_body={"display_name": "p21 attacker domain mutation"}),
        FuzzCase("path.domain.delete",   "path", "DELETE",
                 f"/api/v1/admin/domains/{victim_tenant}/general", attacker_admin),
        FuzzCase("path.agent.get",       "path", "GET",
                 f"/api/v1/admin/agents/{victim_tenant}/{victim_agent_id}",
                 attacker_admin),
        FuzzCase("path.agent.patch",     "path", "PATCH",
                 f"/api/v1/admin/agents/{victim_tenant}/{victim_agent_id}",
                 attacker_admin,
                 json_body={"display_name": "p21 attacker agent mutation"}),
        FuzzCase("path.agent.delete",    "path", "DELETE",
                 f"/api/v1/admin/agents/{victim_tenant}/{victim_agent_id}",
                 attacker_admin),
        FuzzCase("path.agent.bind_user", "path", "POST",
                 f"/api/v1/admin/agents/{victim_tenant}/{victim_agent_id}/bind-user",
                 attacker_admin,
                 json_body={"user_id": "p21-attacker-user"}),
    ]

    # ---- ID-only IDOR vector ----
    cases += [
        FuzzCase("idor.api_key.delete",  "idor", "DELETE",
                 f"/api/v1/admin/api-keys/{victim_api_key_id}", attacker_admin),
        FuzzCase("idor.luciel.get",      "idor", "GET",
                 f"/api/v1/admin/luciel-instances/{victim_luciel_pk}",
                 attacker_admin),
        FuzzCase("idor.luciel.patch",    "idor", "PATCH",
                 f"/api/v1/admin/luciel-instances/{victim_luciel_pk}",
                 attacker_admin,
                 json_body={"display_name": "p21 attacker luciel mutation"}),
        FuzzCase("idor.luciel.delete",   "idor", "DELETE",
                 f"/api/v1/admin/luciel-instances/{victim_luciel_pk}",
                 attacker_admin),
        FuzzCase("idor.luciel.knowledge.list", "idor", "GET",
                 f"/api/v1/admin/luciel-instances/{victim_luciel_pk}/knowledge",
                 attacker_admin),
        FuzzCase("idor.luciel.chunking_config", "idor", "GET",
                 f"/api/v1/admin/luciel-instances/{victim_luciel_pk}/chunking-config",
                 attacker_admin),
    ]

    # ---- Query-param tenant vector ----
    # List endpoints with a tenant_id query param. PASS = no victim
    # items in the response (200 OK is fine if filtered correctly).
    cases += [
        FuzzCase("query.api_keys.tenant",      "query", "GET",
                 f"/api/v1/admin/api-keys?tenant_id={victim_tenant}",
                 attacker_admin, verdict_fn=list_v),
        FuzzCase("query.tenants.list",         "query", "GET",
                 "/api/v1/admin/tenants", attacker_admin,
                 verdict_fn=list_v),
        FuzzCase("query.domains.tenant",       "query", "GET",
                 f"/api/v1/admin/domains?tenant_id={victim_tenant}",
                 attacker_admin, verdict_fn=list_v),
        FuzzCase("query.memory_items.tenant",  "query", "GET",
                 f"/api/v1/admin/memory-items?tenant_id={victim_tenant}",
                 attacker_admin, verdict_fn=list_v),
        FuzzCase("query.luciels.tenant",       "query", "GET",
                 f"/api/v1/admin/luciel-instances?tenant_id={victim_tenant}",
                 attacker_admin, verdict_fn=list_v),
        FuzzCase("query.agents.tenant",        "query", "GET",
                 f"/api/v1/admin/agents?tenant_id={victim_tenant}",
                 attacker_admin, verdict_fn=list_v),
    ]

    # ---- Body-tenant POST vector ----
    # The attacker submits a POST with body claiming the victim's
    # tenant_id. Expect deny (or schema rejection -- 422).
    cases += [
        FuzzCase("body.tenants.create",       "body", "POST",
                 "/api/v1/admin/tenants", attacker_admin,
                 json_body={
                     "tenant_id": victim_tenant,
                     "display_name": "p21 attacker tenant claim",
                 }),
        FuzzCase("body.domains.create",       "body", "POST",
                 "/api/v1/admin/domains", attacker_admin,
                 json_body={
                     "tenant_id": victim_tenant,
                     "domain_id": f"p21-attk-{uuid.uuid4().hex[:6]}",
                     "display_name": "p21 attacker domain claim",
                 }),
        FuzzCase("body.api_keys.create",      "body", "POST",
                 "/api/v1/admin/api-keys", attacker_admin,
                 json_body={
                     "tenant_id": victim_tenant,
                     "display_name": "p21 attacker key claim",
                     "permissions": ["chat", "sessions"],
                     "rate_limit": 10,
                 }),
        FuzzCase("body.agents.create",        "body", "POST",
                 "/api/v1/admin/agents", attacker_admin,
                 json_body={
                     "tenant_id": victim_tenant,
                     "domain_id": "general",
                     "agent_id": f"p21attk-{uuid.uuid4().hex[:6]}",
                     "display_name": "p21 attacker agent claim",
                 }),
        FuzzCase("body.knowledge.ingest",     "body", "POST",
                 "/api/v1/admin/knowledge/ingest", attacker_admin,
                 json_body={
                     "tenant_id": victim_tenant,
                     "domain_id": "general",
                     "source_id": f"p21attk-src-{uuid.uuid4().hex[:6]}",
                     "title": "p21 attacker doc",
                     "content": "attempted cross-tenant ingest",
                 }),
        FuzzCase("body.luciel.create",        "body", "POST",
                 "/api/v1/admin/luciel-instances", attacker_admin,
                 json_body={
                     "instance_id": f"p21attk-luc-{uuid.uuid4().hex[:6]}",
                     "display_name": "p21 attacker luciel claim",
                     "scope_level": "tenant",
                     "scope_owner_tenant_id": victim_tenant,
                 }),
        FuzzCase("body.scope_assignments.create", "body", "POST",
                 "/api/v1/admin/scope-assignments", attacker_admin,
                 json_body={
                     "tenant_id": victim_tenant,
                     "domain_id": "general",
                     "agent_id": f"p21attk-{uuid.uuid4().hex[:6]}",
                     "user_id": "p21attk-user",
                     "role": "agent_admin",
                 }),
    ]

    return cases


class CrossTenantScopeLeakPillar(Pillar):
    number = 21
    name = "cross-tenant scope-leak fuzz suite (P3-D)"

    def run(self, state: RunState) -> str:
        if not state.platform_admin_key:
            raise AssertionError("pillar 21 requires platform_admin_key")

        pa = state.platform_admin_key
        victim_tenant = _victim_tid()
        attacker_tenant = _attacker_tid()
        victim_provisioned = False
        attacker_provisioned = False

        try:
            # ----- Setup phase -----
            with pooled_client() as c:
                victim_admin = _onboard_throwaway(
                    c, pa, victim_tenant, "victim",
                )
                victim_provisioned = True
                attacker_admin = _onboard_throwaway(
                    c, pa, attacker_tenant, "attacker",
                )
                attacker_provisioned = True

                victim_resources = _mint_victim_resources(
                    c, pa, victim_admin, victim_tenant,
                )

                cases = _build_fuzz_cases(
                    attacker_admin=attacker_admin,
                    attacker_tenant=attacker_tenant,
                    victim_tenant=victim_tenant,
                    victim_agent_id=victim_resources["agent_id"],
                    victim_luciel_pk=victim_resources["luciel_pk"],
                    victim_api_key_id=victim_resources["api_key_id"],
                )

                # ----- Fuzz phase -----
                # Run every case; collect failures; do not short-
                # circuit. One pass = full coverage report.
                failures: list[dict[str, Any]] = []
                pass_count = 0
                for case in cases:
                    passed, reason, status = case.execute(c)
                    if passed:
                        pass_count += 1
                    else:
                        failures.append({
                            "case_id": case.case_id,
                            "vector": case.vector,
                            "method": case.method,
                            "path": case.path,
                            "status": status,
                            "reason": reason,
                        })

                if failures:
                    # Build a self-describing detail. Every regressed
                    # case gets a line. The first 8 are inlined for
                    # readability; if there are more, the count is
                    # surfaced so the operator knows to read the JSON
                    # report.
                    lines = [
                        f"{len(failures)}/{len(cases)} fuzz cases LEAKED:",
                    ]
                    for f in failures[:8]:
                        lines.append(
                            f"  [{f['vector']}] {f['method']} {f['path']} "
                            f"-> {f['status']} ({f['reason']}) "
                            f"case={f['case_id']}"
                        )
                    if len(failures) > 8:
                        lines.append(f"  ... and {len(failures) - 8} more")
                    raise AssertionError("\n".join(lines))

                return (
                    f"victim={victim_tenant[-12:]} "
                    f"attacker={attacker_tenant[-12:]} "
                    f"fuzz_cases={len(cases)} all_denied "
                    f"(path/idor/query/body all clean)"
                )
        finally:
            # Self-cleanup: deactivate both tenants. Best-effort.
            for tid, was_provisioned in (
                (victim_tenant, victim_provisioned),
                (attacker_tenant, attacker_provisioned),
            ):
                if not was_provisioned:
                    continue
                try:
                    with httpx.Client(
                        base_url=BASE_URL, timeout=REQUEST_TIMEOUT,
                    ) as c2:
                        c2.patch(
                            f"/api/v1/admin/tenants/{tid}",
                            headers=h(pa),
                            json={"active": False},
                        )
                except Exception:
                    pass


PILLAR = CrossTenantScopeLeakPillar()
