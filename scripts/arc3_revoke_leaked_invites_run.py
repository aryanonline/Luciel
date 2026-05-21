"""scripts/arc3_revoke_leaked_invites_run.py — Arc 3 Work-Unit A.2a driver.

Thin Python driver for scripts/arc3_revoke_leaked_invites.sql. Exists
because the partner's Windows shell does not have `psql` on PATH, and
installing the Postgres client tooling is more side-effect than this
one-shot remediation justifies. The backend venv already pulls in
psycopg (v3, see pyproject.toml), which is the same driver the live
ECS task uses — so this driver hits the DB through the EXACT same
client surface as production.

Behavior mirror with scripts/arc3_revoke_leaked_invites.sql:

  (1) Load leaked JTI list from arc3-out/leaked-welcome-jtis.txt into
      a temp table inside a single transaction.
  (2a) Print by-status breakdown of all matching rows (load-bearing
       evidence: shows how many already-accepted or already-revoked
       rows we will correctly LEAVE ALONE).
  (2b) Print full row dump of every PENDING row that WOULD be flipped
       (with invited_email so partner can spot any unexpected address).
  (3)  DRY-RUN MODE: ROLLBACK; print summary; exit.
       LIVE MODE: run the UPDATE with status='pending' guard, capture
                  the RETURNING set to arc3-out/flipped-invites.psv in
                  the SAME pipe-delimited shape the .sql file produced
                  (so the paired scripts/arc3_audit_leaked_invites_record.py
                  consumes it without changes).

Idempotency contract is unchanged: the UPDATE's WHERE clause includes
status='pending', so a second live run on the same JTI list is a no-op.

Usage:

  # Dry-run (no UPDATE, just preview):
  python scripts\\arc3_revoke_leaked_invites_run.py `
         --jti-file arc3-out\\leaked-welcome-jtis.txt `
         --dry-run

  # Live flip:
  python scripts\\arc3_revoke_leaked_invites_run.py `
         --jti-file arc3-out\\leaked-welcome-jtis.txt `
         --out arc3-out\\flipped-invites.psv

Env: DATABASE_URL must be set. The script reads it from the same env
as the backend (no .env auto-load — keep prod creds explicit).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

PSV_HEADER_COLS = (
    "invite_id",
    "tenant_id",
    "domain_id",
    "invited_email",
    "token_jti",
)


def _load_jtis(path: Path) -> list[str]:
    jtis: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            j = raw.strip()
            if not j:
                continue
            jtis.append(j)
    # Dedup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for j in jtis:
        if j not in seen:
            seen.add(j)
            out.append(j)
    return out


def _print_table(rows: list[tuple], headers: tuple[str, ...]) -> None:
    if not rows:
        print(f"  (no rows)  [{', '.join(headers)}]")
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell) if cell is not None else ""))
    sep = "  ".join("-" * w for w in widths)
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + sep)
    for r in rows:
        cells = [(str(c) if c is not None else "") for c in r]
        print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jti-file", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--out",
        default="arc3-out/flipped-invites.psv",
        help="Path to write pipe-delimited RETURNING rows (live mode only).",
    )
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set in env.", file=sys.stderr)
        return 2

    jti_path = Path(args.jti_file)
    if not jti_path.is_file():
        print(f"jti file not found: {jti_path}", file=sys.stderr)
        return 2

    jtis = _load_jtis(jti_path)
    print(f"=== Arc 3 Work-Unit A.2a: revoke leaked invites ===")
    print(f"mode      : {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"jti-file  : {jti_path} ({len(jtis)} unique JTIs)")
    print(f"out-file  : {args.out if not args.dry_run else '(dry-run, no write)'}")
    print()

    # psycopg3: a single connection, single transaction, autocommit OFF.
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # (1) temp table + load
            cur.execute(
                "CREATE TEMP TABLE arc3_leaked_jtis (token_jti text PRIMARY KEY) "
                "ON COMMIT DROP"
            )
            cur.executemany(
                "INSERT INTO arc3_leaked_jtis(token_jti) VALUES (%s) "
                "ON CONFLICT (token_jti) DO NOTHING",
                [(j,) for j in jtis],
            )
            cur.execute("SELECT count(*) FROM arc3_leaked_jtis")
            loaded = cur.fetchone()[0]
            print(f"--- (1) loaded JTIs into temp table: {loaded} ---")
            print()

            # (2a) by-status breakdown
            print("--- (2a) by-status breakdown of matches ---")
            cur.execute(
                """
                SELECT ui.status::text AS status, count(*) AS row_count
                  FROM user_invites ui
                  JOIN arc3_leaked_jtis lk USING (token_jti)
                 GROUP BY ui.status
                 ORDER BY ui.status
                """
            )
            breakdown = cur.fetchall()
            _print_table(breakdown, ("status", "row_count"))
            print()

            # (2b) full pending preview
            print("--- (2b) pending invites that WOULD be flipped ---")
            cur.execute(
                """
                SELECT ui.id::text         AS invite_id,
                       ui.tenant_id::text  AS tenant_id,
                       ui.domain_id::text  AS domain_id,
                       ui.invited_email    AS invited_email,
                       ui.token_jti        AS token_jti,
                       ui.created_at       AS created_at
                  FROM user_invites ui
                  JOIN arc3_leaked_jtis lk USING (token_jti)
                 WHERE ui.status = 'pending'
                 ORDER BY ui.created_at
                """
            )
            preview = cur.fetchall()
            _print_table(
                preview,
                ("invite_id", "tenant_id", "domain_id", "invited_email", "token_jti", "created_at"),
            )
            print()
            print(f"  -> {len(preview)} row(s) would flip pending -> revoked")
            print()

            if args.dry_run:
                print("=== DRY RUN — no UPDATE executed. ROLLBACK. ===")
                conn.rollback()
                return 0

            # LIVE: idempotent flip
            cur.execute(
                """
                UPDATE user_invites ui
                   SET status     = 'revoked',
                       updated_at = now()
                  FROM arc3_leaked_jtis lk
                 WHERE ui.token_jti = lk.token_jti
                   AND ui.status    = 'pending'
             RETURNING ui.id::text        AS invite_id,
                       ui.tenant_id::text AS tenant_id,
                       ui.domain_id::text AS domain_id,
                       ui.invited_email   AS invited_email,
                       ui.token_jti       AS token_jti
                """
            )
            flipped = cur.fetchall()
            conn.commit()

            # Write pipe-delimited RETURNING capture for the paired
            # audit-record helper (same shape the .sql file emits).
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8", newline="") as g:
                g.write("|".join(PSV_HEADER_COLS) + "\n")
                for r in flipped:
                    cells = [(str(c) if c is not None else "") for c in r]
                    g.write("|".join(cells) + "\n")

            print("=== COMMIT — pending invites flipped to revoked. ===")
            print(f"  flipped_rows   : {len(flipped)}")
            print(f"  RETURNING file : {out_path}")
            print()
            print("Next: python scripts\\arc3_audit_leaked_invites_record.py "
                  f"{out_path}")
            return 0


if __name__ == "__main__":
    sys.exit(main())
