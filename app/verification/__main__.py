"""Step 26 verification entry point.

Usage:
    python -m app.verification
    python -m app.verification --keep
    python -m app.verification --skip-migration
    python -m app.verification --json-report step26_report.json
    python -m app.verification --sweep-residue
    python -m app.verification --allow-degraded   (local dev only; never CI)

Exit code 0 iff every registered pillar ran at FULL mode. A pillar
that ran at DEGRADED mode (broker/worker unreachable, fallback path
taken) flips the exit to 1 unless ``--allow-degraded`` is passed.
A pillar that raised flips the exit to 1 regardless. Exit code 0 +
the JSON report artifact is the Step 26b production-redeploy gate.

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
  --allow-degraded            Treat DEGRADED pillars as passing. Intended
                              for local dev where Redis/SQS/Celery may be
                              intentionally absent. CI must never set
                              this -- the prod-redeploy gate requires
                              every pillar at FULL mode.

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
from app.verification.registry import (
    pre_teardown_pillars,
    teardown_integrity_pillar,
)
from app.verification.runner import MatrixReport, Outcome, PillarResult, SuiteRunner

# Step 29 Commit D: pillar registration moved to app.verification.registry
# so the new pytest harness (tests/verification/test_pillars.py) and this
# CLI entry point share one source of truth. The previous explicit imports
# of P1..P23 are now resolved inside registry.pre_teardown_pillars() and
# registry.teardown_integrity_pillar() with the same ordering. The verify
# matrix shape (pillar order, P9 placement, P10 deferred to post-teardown)
# is preserved bit-for-bit -- this is a pure extraction, not a behavior
# change, so luciel-verify:13 (the in-prod TD) will produce an identical
# report when run against the post-D code at E(iv) once luciel-verify:14
# ships.
#
# Step 29.y Cluster 8: tri-state runner contract. The CLI gains
# --allow-degraded and the FINAL banner gains a mode column so an
# operator can see at a glance which pillars ran at FULL vs DEGRADED.


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
    p.add_argument("--allow-degraded", action="store_true",
                   help=("treat DEGRADED pillars as passing (local dev only; "
                         "CI must never pass this -- prod gate requires FULL)"))
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
    if args.allow_degraded:
        print(f"  mode: --allow-degraded (DEGRADED pillars will not fail the run)")
    print()

    # ---- pre-teardown pillars 1..8, 11..23 (+ 9 if not skipped) ----
    # registry.pre_teardown_pillars(include_migration=) returns the same
    # list the legacy PRE_TEARDOWN_PILLARS literal produced, with P9
    # appended iff include_migration=True. Caller-side --skip-migration
    # maps to include_migration=False.
    runner = SuiteRunner()
    for pillar in pre_teardown_pillars(include_migration=not args.skip_migration):
        runner.register(pillar)
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
        post_runner = SuiteRunner().register(teardown_integrity_pillar())
        post_report = post_runner.run(state=state)
        print(post_report.render_human())
        # ---- merge pre + post into single matrix, emit JSON artifact ----
    final = _merge_reports(pre_report, post_report, state)

    print()
    print("=" * 72)
    print("FINAL STEP 26 MATRIX")
    print("=" * 72)
    for r in final.results:
        # Tri-state column: FULL / DEGRADED / FAIL. The legacy banner
        # used PASS/FAIL; we keep the column width identical so existing
        # log-parsing regexes that key on the bracketed token still match.
        mark = r.outcome.value
        print(f"  [{mark:<8s}] {r.number:2d}. {r.name:<50s} {r.elapsed_s:6.2f}s")
        if r.outcome == Outcome.DEGRADED:
            print(f"             reason: {r.detail}")
    print("=" * 72)
    print(
        f"RESULT: {final.full_count} FULL, "
        f"{final.degraded_count} DEGRADED, "
        f"{final.fail_count} FAIL "
        f"(of {final.total_count})"
    )
    if final.degraded_count and not args.allow_degraded:
        print(
            "GATE: failing run because DEGRADED pillars are present and "
            "--allow-degraded was not set. The prod-redeploy gate requires "
            "every pillar at FULL mode -- see Cluster 8 of Step 29.y."
        )
    print("=" * 72)

    if args.json_report:
        try:
            final.write_json(args.json_report)
            print(f"json report written: {args.json_report}")
        except Exception as exc:
            print(f"json report write FAILED: {exc}", file=sys.stderr)
            return 2

    return final.exit_code(allow_degraded=args.allow_degraded)


if __name__ == "__main__":
    sys.exit(main())
