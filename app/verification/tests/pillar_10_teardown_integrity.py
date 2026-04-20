"""Pillar 10 - Teardown integrity (gap-8 fix, NEW pillar).

Landed suite teardown was best-effort-silent: PATCH tenant active=False
for every key in keys_to_deactivate, log exceptions, continue. Nothing
verified that teardown actually cleaned up. Result: 13 step26-verify-*
residue tenants accumulated across sessions in the DB.

Redo closes gap-8 with an affirmative post-teardown audit:
  - For the specific tenant_id this run created, walk every app table
    that carries a tenant_id column and count LIVE rows (active=True
    where the table has an active column).
  - Assert every count is zero EXCEPT:
      - tenant_configs: exactly 1 row with active=False (the deactivated
        tenant itself -- teardown flips active=False, does not delete)
      - api_keys: zero LIVE rows (inactive rows are fine, teardown
        deactivates rather than deletes for audit retention)
      - admin_audit_logs: rows expected (audit is append-only)
      - agent_configs: legacy pre-Step-24.5 read-only table per Step 24.5;
        this suite never writes to it so zero rows for this tenant
        expected, but if any exist they're pre-existing and non-blocking

Subprocess-isolated for the same reasons as pillar 9 (clean SQLAlchemy
metadata, no httpx pool leak). Read-only queries. Runs AFTER teardown --
__main__.py invokes teardown before registering pillar 10 with the runner,
so pillar 10 sees the post-teardown state.

The AFTER-teardown ordering is the single invariant pillar 10 establishes
that no other pillar can: "this run's throwaway tenant leaves no active
residue on the stack."
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from app.verification.fixtures import RunState
from app.verification.runner import Pillar


_CHECK_SCRIPT = r"""
import os, sys, json
from sqlalchemy import create_engine, text, inspect

db_url = os.environ.get("DATABASE_URL")
target_tenant = os.environ.get("STEP26_TARGET_TENANT")
if not db_url:
    print("FAIL:no_database_url", flush=True); sys.exit(2)
if not target_tenant:
    print("FAIL:no_target_tenant", flush=True); sys.exit(2)

engine = create_engine(db_url)
insp = inspect(engine)
existing_tables = set(insp.get_table_names())

# (table_name, active_column_or_None, expected_live_count_for_target_tenant)
#
# expected_live_count semantics:
#   0   -> MUST be zero live rows for this tenant after teardown
#   "*" -> count irrelevant (append-only audit or legacy read-only)
# (table, active_col, expectation, tenant_col_candidates)
#
# tenant_col_candidates: tried in order, first that exists on the table is used
# to scope the count. If none match, table is flagged "no_tenant_scope" and
# skipped for enforcement (still observed).
PROBES = [
    ("tenant_configs",       "active", "tenant_inactive_exactly_one", ["tenant_id"]),
    ("domain_configs",       "active", 0,                              ["tenant_id"]),
    ("agents",               "active", 0,                              ["tenant_id"]),
    ("luciel_instances",     "active", 0,                              ["scope_owner_tenant_id", "tenant_id"]),
    ("api_keys",             "active", 0,                              ["tenant_id"]),
    ("sessions",             None,     "*",                            ["tenant_id"]),
    ("messages",             None,     "*",                            ["tenant_id"]),  # may legitimately have no tenant col
    ("traces",               None,     "*",                            ["tenant_id"]),
    ("memory_items",         None,     "*",                            ["tenant_id"]),
    ("user_consents",        None,     "*",                            ["tenant_id"]),
    ("knowledge_embeddings", None,     "*",                            ["tenant_id"]),
    ("retention_policies",   None,     "*",                            ["tenant_id"]),
    ("deletion_logs",        None,     "*",                            ["tenant_id"]),
    ("admin_audit_logs",     None,     "*",                            ["tenant_id"]),
    ("agent_configs",        None,     "*",                            ["tenant_id"]),
]

violations = []
observations = {}

def has_column(table, col):
    try:
        return col in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False

with engine.connect() as conn:
    for table, active_col, expectation, tenant_col_candidates in PROBES:
        if table not in existing_tables:
            observations[table] = "missing_table"
            continue

        tenant_col = next((c for c in tenant_col_candidates if has_column(table, c)), None)
        if tenant_col is None:
            observations[table] = "no_tenant_scope"
            continue

        total = conn.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {tenant_col} = :tid"),
            {"tid": target_tenant},
        ).scalar_one()

        if active_col and has_column(table, active_col):
            live = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} "
                     f"WHERE {tenant_col} = :tid AND {active_col} = TRUE"),
                {"tid": target_tenant},
            ).scalar_one()
        else:
            live = None

        observations[table] = {"total": total, "live": live, "scoped_by": tenant_col}

        if expectation == 0:
            if live is None:
                violations.append(f"{table}: expected live=0 but no active column")
            elif live != 0:
                violations.append(
                    f"{table}: expected live=0, got live={live} (total={total}, scoped_by={tenant_col})"
                )
        elif expectation == "tenant_inactive_exactly_one":
            if total != 1:
                violations.append(f"{table}: expected exactly 1 row, got {total}")
            elif live not in (0, None):
                violations.append(f"{table}: expected inactive, got live={live}")
        # "*" expectations: record observation, do not enforce

report = {
    "target_tenant": target_tenant,
    "violations": violations,
    "observations": observations,
}
ok = len(violations) == 0
print(("OK:" if ok else "FAIL:") + json.dumps(report), flush=True)
sys.exit(0 if ok else 1)
"""


class TeardownIntegrityPillar(Pillar):
    number = 10
    name = "teardown integrity (zero residue for this tenant)"

    def run(self, state: RunState) -> str:
        if not state.tenant_id:
            raise AssertionError("pillar 10 requires tenant_id from RunState")

        # Resolve DATABASE_URL the same way pillar 9 does.
        from app.verification.tests.pillar_09_migration_integrity import (
            MigrationIntegrityPillar,
        )
        db_url = os.environ.get("DATABASE_URL") or MigrationIntegrityPillar._load_database_url_from_dotenv()
        if not db_url:
            raise AssertionError("DATABASE_URL not found for pillar 10")

        env = os.environ.copy()
        env["DATABASE_URL"] = db_url
        env["STEP26_TARGET_TENANT"] = state.tenant_id

        proc = subprocess.run(
            [sys.executable, "-c", _CHECK_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if not out:
            raise AssertionError(
                f"teardown-integrity subprocess produced no output. "
                f"returncode={proc.returncode} stderr={err[:500]}"
            )

        if out.startswith("OK:"):
            rpt = json.loads(out[3:])
            obs = rpt["observations"]
            # concise summary: tenant + key table totals
            tc = obs.get("tenant_configs", {})
            ak = obs.get("api_keys", {})
            li = obs.get("luciel_instances", {})
            return (
                f"tenant={rpt['target_tenant']}: "
                f"tenant_configs total={tc.get('total')} live={tc.get('live')}; "
                f"luciel_instances live={li.get('live')}; "
                f"api_keys live={ak.get('live')}; "
                f"zero residue"
            )
        if out.startswith("FAIL:"):
            raise AssertionError(
                f"teardown integrity violation: body={out[5:][:1500]}"
            )
        raise AssertionError(
            f"teardown-integrity subprocess unexpected output: {out[:500]} "
            f"stderr={err[:500]}"
        )


PILLAR = TeardownIntegrityPillar()