"""
Arc 7 Commit 2 Slice 7 — Schema verification probe.

Confirms post-migration prod RDS state:
  1. alembic_version = arc7_a_retire_billing_model
  2. subscriptions.billing_model is GONE
  3. admin_tier_overrides.billing_model is GONE
  4. No CHECK constraints referencing billing_model
  5. No indexes on billing_model
  6. Arc 8 email tables (email_send_event, email_suppression) present (sanity)

Exit code 0 = all checks pass; 1 = any failure (logged).
"""
import os
import sys
import sqlalchemy as sa

DATABASE_URL = os.environ["DATABASE_URL"]
# sqlalchemy needs postgresql:// not postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = sa.create_engine(DATABASE_URL)
failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    if ok:
        passes.append(f"PASS  {name}: {detail}")
    else:
        failures.append(f"FAIL  {name}: {detail}")


with engine.connect() as conn:
    # 1. Alembic head
    row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
    head = row[0] if row else None
    check(
        "alembic_head",
        head == "arc7_a_retire_billing_model",
        f"got={head!r} expected='arc7_a_retire_billing_model'",
    )

    # 2 & 3. billing_model columns gone
    for table in ("subscriptions", "admin_tier_overrides"):
        row = conn.execute(
            sa.text(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:t AND column_name='billing_model'
                """
            ),
            {"t": table},
        ).fetchone()
        check(
            f"{table}.billing_model_dropped",
            row is None,
            f"row={row}",
        )

    # 4. No CHECK constraints referencing billing_model
    rows = conn.execute(
        sa.text(
            """
            SELECT conname, conrelid::regclass::text AS table_name
            FROM pg_constraint
            WHERE contype='c' AND pg_get_constraintdef(oid) ILIKE '%billing_model%'
            """
        )
    ).fetchall()
    check(
        "no_billing_model_check_constraints",
        len(rows) == 0,
        f"found={[(r[0], r[1]) for r in rows]}",
    )

    # 5. No indexes on billing_model
    rows = conn.execute(
        sa.text(
            """
            SELECT indexname, tablename FROM pg_indexes
            WHERE schemaname='public' AND indexdef ILIKE '%billing_model%'
            """
        )
    ).fetchall()
    check(
        "no_billing_model_indexes",
        len(rows) == 0,
        f"found={[(r[0], r[1]) for r in rows]}",
    )

    # 6. Arc 8 email tables present
    for tbl in ("email_send_event", "email_suppression"):
        row = conn.execute(
            sa.text(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='public' AND table_name=:t
                """
            ),
            {"t": tbl},
        ).fetchone()
        check(
            f"arc8_table_{tbl}_present",
            row is not None,
            f"row={row}",
        )

print("=" * 70)
print("ARC 7 COMMIT 2 SLICE 7 — SCHEMA VERIFICATION PROBE")
print("=" * 70)
for p in passes:
    print(p)
for f in failures:
    print(f)
print("=" * 70)
print(f"PASSES: {len(passes)}  FAILURES: {len(failures)}")
print("=" * 70)

sys.exit(0 if not failures else 1)
