-- =====================================================================
-- scripts/arc3_revoke_leaked_invites.sql
--
-- Arc 3 Work-Unit A.2a — idempotent revocation of user_invites rows whose
-- token_jti was captured in the CloudWatch backlog audit (Work-Unit A.1).
--
-- Closes the SQL leg of D-set-password-token-logged-plaintext-2026-05-17.
--
-- Scope: this script does ONE thing — flip user_invites.status from
-- 'pending' to 'revoked' for every row whose token_jti is in the input
-- file. It does NOT write the matching admin_audit_logs entries. That is
-- a SEPARATE step (Work-Unit A.2b) executed by
-- scripts/arc3_audit_leaked_invites_record.py because admin_audit_logs
-- carries a hash-chain integrity contract (row_hash + prev_row_hash) that
-- only AdminAuditRepository.record() satisfies — raw SQL INSERTs would
-- silently break the chain.
--
-- Idempotency: the UPDATE's WHERE clause includes status='pending', so
-- re-running with the same JTI list after the first run is a no-op (all
-- matching rows are already 'revoked'). Safe to re-run.
--
-- Output: the UPDATE's RETURNING clause emits a header + one row per
-- flipped invite to stdout in pipe-delimited form so the paired Python
-- helper can pick it up via stdin (no temp file race).
--
-- Usage (PowerShell 5.1):
--   $JtiFile = (Resolve-Path .\arc3-out\leaked-welcome-jtis.txt).Path
--   psql $env:DATABASE_URL `
--        -v ON_ERROR_STOP=1 `
--        -v jti_file=$JtiFile `
--        -f scripts\arc3_revoke_leaked_invites.sql `
--        > arc3-out\flipped-invites.psv
--
-- Dry-run (counts only, no UPDATE):
--   psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -v jti_file=$JtiFile `
--        -v dry_run=1 -f scripts\arc3_revoke_leaked_invites.sql
-- =====================================================================

\set ON_ERROR_STOP on
\timing on
\pset format unaligned
\pset fieldsep '|'

BEGIN;

-- ---------------------------------------------------------------------
-- (1) Load the leaked-JTI list (one jti per line) into a tx-local temp
--     table. \copy is client-side parse so the psql process reads the
--     file (no server-side superuser needed).
-- ---------------------------------------------------------------------
CREATE TEMP TABLE arc3_leaked_jtis (
    token_jti  text PRIMARY KEY
) ON COMMIT DROP;

\copy arc3_leaked_jtis(token_jti) FROM :'jti_file' WITH (FORMAT csv, HEADER false)

\echo '--- (1) loaded JTIs into temp table ---'
SELECT count(*) AS loaded_jti_count FROM arc3_leaked_jtis;

-- ---------------------------------------------------------------------
-- (2) Preview the match. ALWAYS prints to stderr-equivalent (psql \echo
--     and SELECT outputs go to the same fd, so we just \echo a header).
-- ---------------------------------------------------------------------
\echo '--- (2a) by-status breakdown of matches (includes non-pending) ---'
SELECT
    ui.status,
    count(*) AS row_count
FROM user_invites ui
JOIN arc3_leaked_jtis lk USING (token_jti)
GROUP BY ui.status
ORDER BY ui.status;

\echo '--- (2b) pending invites that will be flipped ---'
SELECT
    ui.id::text         AS invite_id,
    ui.tenant_id,
    ui.domain_id,
    ui.invited_email,
    ui.token_jti,
    ui.created_at
FROM user_invites ui
JOIN arc3_leaked_jtis lk USING (token_jti)
WHERE ui.status = 'pending'
ORDER BY ui.created_at;

-- ---------------------------------------------------------------------
-- (3) Bail out cleanly in dry-run mode.
-- ---------------------------------------------------------------------
\if :{?dry_run}
    \echo ''
    \echo '=== DRY RUN — no UPDATE executed. ROLLBACK in progress. ==='
    ROLLBACK;
\else

-- ---------------------------------------------------------------------
-- (4) Idempotent flip: PENDING -> REVOKED. RETURNING emits one row per
--     flipped invite to stdout in pipe-delimited form, header row first,
--     so the paired Python helper (Work-Unit A.2b) can consume it via
--     stdin redirect and write the audit-log entries through the
--     hash-chain-aware AdminAuditRepository.record().
-- ---------------------------------------------------------------------
\echo '--- (4) RETURNING from UPDATE follows; pipe-delimited ---'
UPDATE user_invites ui
   SET status     = 'revoked',
       updated_at = now()
  FROM arc3_leaked_jtis lk
 WHERE ui.token_jti = lk.token_jti
   AND ui.status    = 'pending'
RETURNING
    ui.id::text         AS invite_id,
    ui.tenant_id        AS tenant_id,
    ui.domain_id        AS domain_id,
    ui.invited_email    AS invited_email,
    ui.token_jti        AS token_jti;

COMMIT;
\echo '=== COMMIT — pending invites flipped to revoked. ==='
\echo '=== Next: run scripts\\arc3_audit_leaked_invites_record.py to write the audit-log hash-chain entries. ==='

\endif

-- =====================================================================
-- Re-run safety: a second pass loads the same JTI list, but the UPDATE
-- has WHERE status='pending'; previously-flipped rows are now 'revoked'
-- and skipped silently. No duplicate flips, no duplicate audit-log
-- entries (the Python helper checks for an existing audit row per
-- invite_id + reason before recording).
-- =====================================================================
