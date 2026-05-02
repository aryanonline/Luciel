"""Step 26 verification entry point.

Usage:
    python -m app.verification
    python -m app.verification --keep
    python -m app.verification --skip-migration
    python -m app.verification --json-report step26_report.json
    python -m app.verification --sweep-residue

Exit code 0 iff every registered pillar passed. Exit code 0 + the
JSON report artifact is the Step 26b production-redeploy gate.

Flags:
  --keep                      Skip teardown (leaves the throwaway tenant
                              and all keys live for forensic inspection).
                              Implies --skip-teardown-integrity.
  --skip-migration            Skip pillar 9 (migration-integrity diff).
                              Useful for quick iteration when schema is
                              known-stable.
  --skip-teardown-integrity   Skip pillar 10. Automatically set by --keep.
  --json-report PATH          Write the machine-readable matrix to PATH.
                              This is the 26b gate artifact.
  --sweep-residue             Before starting, sweep prior-run residue
                              tenants (step26-verify-* older than 1h,
                              active=True). Non-destructive of fresh runs.

Prereqs:
  - uvicorn app.main:app --reload  (separate terminal)
  - $env:LUCIEL_PLATFORM_ADMIN_KEY = 'luc_sk_...'
  - DATABASE_URL in environment or .env (for pillars 9 and 10 subprocesses)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta

import httpx

from app.verification.fixtures import RunState, sweep_residue_tenants
from app.verification.http_client import BASE_URL, REQUEST_TIMEOUT, h
from app.verification.runner import MatrixReport, PillarResult, SuiteRunner
from app.verification.tests.pillar_01_onboarding import PILLAR as P1
from app.verification.tests.pillar_02_scope_hierarchy import PILLAR as P2
from app.verification.tests.pillar_03_ingestion import PILLAR as P3
from app.verification.tests.pillar_04_chat_key_binding import PILLAR as P4
from app.verification.tests.pillar_05_chat_resolution import PILLAR as P5
from app.verification.tests.pillar_06_retention import PILLAR as P6
from app.verification.tests.pillar_07_cascade import PILLAR as P7
from app.verification.tests.pillar_08_scope_negatives import PILLAR as P8
from app.verification.tests.pillar_09_migration_integrity import PILLAR as P9
from app.verification.tests.pillar_10_teardown_integrity import PILLAR as P10
from app.verification.tests.pillar_11_async_memory import PILLAR as P11
from app.verification.tests.pillar_12_identity_stability import PILLAR as P12
from app.verification.tests.pillar_13_cross_tenant_identity import PILLAR as P13
from app.verification.tests.pillar_14_departure_semantics import PILLAR as P14
from app.verification.tests.pillar_15_consent_route_no_double_prefix import PILLAR as P15
from app.verification.tests.pillar_16_memory_items_actor_user_id_not_null import PILLAR as P16
from app.verification.tests.pillar_17_api_key_deactivate_audit import PILLAR as P17
from app.verification.tests.pillar_18_tenant_cascade import PILLAR as P18


PRE_TEARDOWN_PILLARS = [P1, P2, P3, P4, P5, P6, P7, P8, P11, P12, P13, P14, P15, P16, P17, P18]


def _thorough_teardown(state: RunState) -> list[str]:
    """Enumerate and deactivate every live entity tied to this tenant.

    Returns a list of human-readable log lines. Silent on individual
    failures -- pillar 10 will catch anything left live.
    """
    log: list[str] = []
    tid = state.tenant_id
    pa = state.platform_admin_key
    if not tid or not pa:
        log.append("teardown: missing tenant_id or platform_admin_key, skipping")
        return log

    with httpx.Client(base_url=BASE_URL, timeout=REQUEST_TIMEOUT) as c:
        hk = h(pa)

        # 1. api_keys
        try:
            r = c.get("/api/v1/admin/api-keys?tenant_id=" + tid, headers=hk)
            if r.status_code == 200:
                body = r.json()
                items = body if isinstance(body, list) else body.get("items") or body.get("value") or []
                for k in items:
                    kid = k.get("id")
                    if k.get("active") and kid is not None:
                        r2 = c.delete("/api/v1/admin/api-keys/" + str(kid), headers=hk)
                        log.append(f"  api_key {kid} DELETE -> {r2.status_code}")
        except Exception as exc:
            log.append(f"  api_keys enumerate error: {exc}")

        # 2. luciel_instances (try both scope_owner_tenant_id and tenant_id)
        try:
            for q in (
                "scope_owner_tenant_id=" + tid,
                "tenant_id=" + tid,
            ):
                r = c.get("/api/v1/admin/luciel-instances?" + q, headers=hk)
                if r.status_code == 200:
                    body = r.json()
                    items = body if isinstance(body, list) else body.get("items") or body.get("value") or []
                    for inst in items:
                        iid = inst.get("id")
                        if inst.get("active") and iid is not None:
                            r2 = c.delete("/api/v1/admin/luciel-instances/" + str(iid), headers=hk)
                            log.append(f"  luciel {iid} DELETE -> {r2.status_code}")
                    if items:
                        break
        except Exception as exc:
            log.append(f"  luciel_instances enumerate error: {exc}")

        # 3. domains
        try:
            r = c.get("/api/v1/admin/domains?tenant_id=" + tid, headers=hk)
            if r.status_code == 200:
                body = r.json()
                items = body if isinstance(body, list) else body.get("items") or []
                for d in items:
                    did = d.get("domain_id")
                    if d.get("active") and did:
                        r2 = c.patch(
                            "/api/v1/admin/domains/" + tid + "/" + did,
                            headers=hk,
                            json={"active": False},
                        )
                        log.append(f"  domain {did} PATCH -> {r2.status_code}")
        except Exception as exc:
            log.append(f"  domains enumerate error: {exc}")

        # 4. tenant last
        try:
            r = c.patch(
                "/api/v1/admin/tenants/" + tid,
                headers=hk,
                json={"active": False},
            )
            log.append(f"  tenant {tid} PATCH -> {r.status_code}")
        except Exception as exc:
            log.append(f"  tenant PATCH error: {exc}")

    return log


def _merge_reports(pre: MatrixReport, post: MatrixReport | None, state: RunState) -> MatrixReport:
    """Combine pre-teardown and post-teardown reports into a single matrix."""
    merged = MatrixReport(
        tenant_id=state.tenant_id,
        base_url=BASE_URL,
        started_at=pre.started_at,
        finished_at=(post.finished_at if post else pre.finished_at),
    )
    merged.results.extend(pre.results)
    if post:
        merged.results.extend(post.results)
    return merged


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.verification")
    p.add_argument("--keep", action="store_true",
                   help="skip teardown (implies --skip-teardown-integrity)")
    p.add_argument("--skip-migration", action="store_true",
                   help="skip pillar 9 (migration integrity)")
    p.add_argument("--skip-teardown-integrity", action="store_true",
                   help="skip pillar 10 (teardown integrity)")
    p.add_argument("--json-report", metavar="PATH", default=None,
                   help="write JSON matrix report to PATH (Step 26b gate artifact)")
    p.add_argument("--sweep-residue", action="store_true",
                   help="before starting, sweep prior step26-verify-* residue (>1h old, active)")
    args = p.parse_args(argv)

    if args.keep:
        args.skip_teardown_integrity = True

    state = RunState()  # loads platform_admin_key + generates fresh tenant_id

    if args.sweep_residue:
        print("pre-run: sweeping prior residue (step26-verify-* older than 1h, active)")
        summary = sweep_residue_tenants(
            platform_admin_key=state.platform_admin_key,
            older_than=timedelta(hours=1),
        )
        print(f"  swept={len(summary['swept'])} "
              f"skipped={len(summary['skipped'])} "
              f"errors={len(summary['errors'])}")

    print()
    print(f"Step 26 Verification")
    print(f"  base: {BASE_URL}")
    print(f"  tenant: {state.tenant_id}")
    print()

    # ---- pre-teardown pillars 1..8 (+ 9 if not skipped) ----
    runner = SuiteRunner()
    for pillar in PRE_TEARDOWN_PILLARS:
        runner.register(pillar)
    if not args.skip_migration:
        runner.register(P9)
    pre_report = runner.run(state=state)

    print(pre_report.render_human())

    # ---- teardown ----
    teardown_log: list[str] = []
    if not args.keep:
        print()
        print("--- teardown ---")
        teardown_log = _thorough_teardown(state)
        for line in teardown_log:
            print(line)
    else:
        print()
        print(f"--keep: teardown skipped. tenant {state.tenant_id} and "
              f"{len(state.keys_to_deactivate)} keys left live for inspection.")

    # ---- post-teardown pillar 10 ----
    post_report: MatrixReport | None = None
    if not args.skip_teardown_integrity:
        print()
        print("--- post-teardown integrity ---")
        post_runner = SuiteRunner().register(P10)
        post_report = post_runner.run(state=state)
        print(post_report.render_human())
        # ---- merge pre + post into single matrix, emit JSON artifact ----
    final = _merge_reports(pre_report, post_report, state)

    print()
    print("=" * 72)
    print("FINAL STEP 26 MATRIX")
    print("=" * 72)
    for r in final.results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  [{mark}] {r.number:2d}. {r.name:<50s} {r.elapsed_s:6.2f}s")
    print("=" * 72)
    print(f"RESULT: {final.passed_count}/{final.total_count} pillars green")
    print("=" * 72)

    if args.json_report:
        try:
            final.write_json(args.json_report)
            print(f"json report written: {args.json_report}")
        except Exception as exc:
            print(f"json report write FAILED: {exc}", file=sys.stderr)
            return 2

    return final.exit_code()


if __name__ == "__main__":
    sys.exit(main())