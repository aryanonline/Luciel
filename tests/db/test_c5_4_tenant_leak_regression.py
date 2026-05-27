"""Arc 9 C5.4 -- Tenant-leak regression suite (cross-cutting).

PURPOSE
=======
The C3 / C4 / C5 RLS migrations each ship their own per-migration shape
tests. Those guard the *individual* migration files against accidental
edits. But there's a class of regression they DON'T catch:

    * A new customer-data table gets added with NO RLS policy at all.
    * An existing table's policy is dropped in a follow-up migration
      but not replaced.
    * The Wall-1 admin_id GUC convention or Wall-3 instance_id GUC
      convention drifts on one table.
    * The Wall-3 NULL-permissive asymmetric WITH CHECK shape is
      weakened to symmetric on one table (allowing a cross-instance
      write to slip through under empty GUC).
    * A Wall-3 downgrade DISABLES RLS on a table that also has a
      Wall-1 policy -- neutering the sibling policy on rollback.
    * The C2 / C4.1 listener doctrine (both GUCs set on every BEGIN)
      regresses.
    * The messages table loses its three-wall posture (orphan refusal +
      Wall-1 + Wall-3 + parent-scope inheritance).

This suite is the cross-cutting safety net. It enumerates the canonical
set of RLS-protected tables, then asserts every migration in that set
follows the shape conventions. New tables MUST be added to the inventory
explicitly -- which forces the implementer to think about RLS coverage.

DOCUMENTED VARIATIONS
=====================
Three Wall-1 tables deliberately deviate from the canonical
``admin_id = current_setting('app.admin_id', true)`` shape. The suite
recognises each variation explicitly:

    1. api_keys (C3.4) -- auth-perimeter table. USING = true
       (cryptographic key_hash defends reads), WITH CHECK is asymmetric
       with a 'platform' sentinel for cross-tenant platform keys.
       Policy name is ``tenant_isolation`` (no table prefix), because
       there is exactly one policy on the table and it predates the
       prefix convention.
    2. admin_widget_domains (C3.5e) -- Wall column is ``admin_id``
       (FK -> admins.id), not ``admin_id``. Strict shape; same GUC.
    3. instances (C3.5d) -- DB table is ``instances`` (model class is
       ``LucielInstance``). Wall column is ``admin_id``.

WHY STATIC-SHAPE (not live-Postgres)
====================================
This sandbox has no live Postgres. The actual cross-tenant denial under
real RLS is verified by the migration- and policy-level shape tests
(which assert the SQL is correct) plus the C2 / C4.1 listener tests
(which assert the GUCs get bound on every BEGIN). The pair is sufficient
to prove the *system* enforces isolation: GUCs are bound + policies use
those GUCs in their predicates + policies are attached to every customer-
data table => cross-tenant access denied.

CI runs the same suite. When CI gets a live Postgres profile, the same
file gains an opt-in @skipUnless(live_db_available) integration class.
That work is C7 wire-up.

RUN
===
    python -m pytest tests/db/test_c5_4_tenant_leak_regression.py -v
"""

from __future__ import annotations

import re
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path


VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "alembic" / "versions"
)


# ---------------------------------------------------------------------
# CANONICAL INVENTORY
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class Wall1Entry:
    """One Wall-1 table entry. Most pre-Arc-9.2 migrations wrote the
    column name ``tenant_id`` in their policy predicate (Arc 9.2 PR
    #101 renamed every customer-data ``tenant_id`` column to
    ``admin_id`` AFTER these migrations shipped; alembic migrations
    are immutable historical artifacts, so they still text-contain
    the pre-rename name even though the live column is now
    ``admin_id``).

    The ``wall_column`` field tracks the column name **as written in
    the migration source text**, not the current live column name.
    Two post-Arc-9.2 migrations (``instances`` and
    ``admin_widget_domains``) shipped after the rename and use
    ``admin_id`` directly; everything else still text-contains
    ``tenant_id``.
    """
    db_table: str                  # the actual table name in PG
    revision: str                  # alembic revision id
    wall_column: str = "tenant_id"  # column name AS WRITTEN in the migration source
    policy_name: str | None = None  # default: f"{db_table}_tenant_isolation"
    auth_perimeter: bool = False   # api_keys-style permissive USING
    rationale: str = ""            # required for any non-default field

    def expected_policy_name(self) -> str:
        return self.policy_name or f"{self.db_table}_tenant_isolation"


@dataclass(frozen=True)
class Wall3Entry:
    """One Wall-3 table entry. All Wall-3 tables share the same shape:
    NULL-permissive USING, asymmetric WITH CHECK, ``luciel_instance_id``
    column, ``app.instance_id`` GUC. Downgrades MUST NOT disable RLS
    (would neuter the Wall-1 sibling on the same table)."""
    db_table: str
    revision: str


# Every customer-data table covered by Wall-1 RLS. When you add a new
# customer-data table, you MUST add its Wall-1 migration here.
WALL_1_ENTRIES: list[Wall1Entry] = [
    Wall1Entry("admin_audit_logs", "arc9_c3_1_rls_admin_audit_logs"),
    Wall1Entry("traces", "arc9_c3_2a_rls_traces"),
    Wall1Entry("memory_items", "arc9_c3_2b_rls_memory_items"),
    Wall1Entry("conversations", "arc9_c3_2c_rls_conversations"),
    # agent_configs entry REMOVED (Arc 10 Gap 7,
    # D-arc10-close-path-imports-deleted-agent-repository-2026-05-27):
    # the `agent_configs` table was DROPPED at
    # arc5_c_admin_instance_subtractive (Arc 5 Path A). Its RLS
    # migration arc9_c3_2d_rls_agent_configs.py is preserved on disk
    # as an immutable historical artifact, but it has no live target
    # table anymore so the cross-cutting inventory must not include
    # it. The cascade in app/services/admin_service.py was pruned in
    # the same Gap 7 PR.
    Wall1Entry("sessions", "arc9_c3_2e_rls_sessions"),
    Wall1Entry("subscriptions", "arc9_c3_2f_rls_subscriptions"),
    Wall1Entry("scope_assignments", "arc9_c3_2g_rls_scope_assignments"),
    Wall1Entry("knowledge_embeddings", "arc9_c3_3_rls_knowledge_embeddings"),
    Wall1Entry(
        db_table="api_keys",
        revision="arc9_c3_4_rls_api_keys",
        policy_name="tenant_isolation",  # legacy name, no table prefix
        auth_perimeter=True,
        rationale=(
            "Auth-perimeter table. USING is permissive (true) because "
            "the apikeyauthmiddleware must look up rows by key_hash "
            "before any tenant context exists. Cryptographic "
            "unguessability of the hash is the structural defence on "
            "reads. WITH CHECK is asymmetric strict with a 'platform' "
            "sentinel for cross-tenant platform keys. See module "
            "docstring on alembic/versions/arc9_c3_4_rls_api_keys.py."
        ),
    ),
    Wall1Entry("user_invites", "arc9_c3_5a_rls_user_invites"),
    Wall1Entry("user_consents", "arc9_c3_5b_rls_user_consents"),
    Wall1Entry("identity_claims", "arc9_c3_5c_rls_identity_claims"),
    Wall1Entry(
        db_table="instances",
        revision="arc9_c3_5d_rls_instances",
        wall_column="admin_id",
        policy_name="instances_tenant_isolation",
        rationale=(
            "DB table name is ``instances`` (the model class is "
            "LucielInstance). This migration shipped AFTER the Arc 9.2 "
            "tenant_id -> admin_id rename, so its source text uses "
            "``admin_id`` directly rather than the pre-rename "
            "``tenant_id`` that earlier C3 migrations carry."
        ),
    ),
    Wall1Entry(
        db_table="admin_widget_domains",
        revision="arc9_c3_5e_rls_admin_widget_domains",
        wall_column="admin_id",
        rationale=(
            "Shipped post Arc 9.2 rename; source text uses "
            "``admin_id`` directly (no pre-rename ``tenant_id`` "
            "residue to translate)."
        ),
    ),
    Wall1Entry("retention_policies", "arc9_c3_6a_rls_retention_policies"),
    Wall1Entry("deletion_logs", "arc9_c3_6b_rls_deletion_logs"),
    Wall1Entry("messages", "arc9_c5_1_rls_messages"),
]


# Every customer-data table covered by Wall-3 RLS.
WALL_3_ENTRIES: list[Wall3Entry] = [
    Wall3Entry("api_keys", "arc9_c4_3a_rls_instance_api_keys"),
    Wall3Entry("knowledge_embeddings", "arc9_c4_3b_rls_instance_knowledge_embeddings"),
    Wall3Entry("memory_items", "arc9_c4_3c_rls_instance_memory_items"),
    Wall3Entry("sessions", "arc9_c4_3d_rls_instance_sessions"),
    Wall3Entry("traces", "arc9_c4_3e_rls_instance_traces"),
    Wall3Entry("admin_audit_logs", "arc9_c4_3f_rls_instance_admin_audit_logs"),
    Wall3Entry("messages", "arc9_c5_2_rls_instance_messages"),
]


def _read_migration(revision: str) -> str:
    path = VERSIONS_DIR / f"{revision}.py"
    if not path.exists():
        raise FileNotFoundError(
            f"Migration {revision}.py not in {VERSIONS_DIR}. "
            "If this table was renamed or the migration moved, "
            "update WALL_1_ENTRIES / WALL_3_ENTRIES in "
            "tests/db/test_c5_4_tenant_leak_regression.py."
        )
    return path.read_text()


# =====================================================================
# CROSS-TENANT (WALL 1) LEAK GUARDS
# =====================================================================
class TestWall1CoverageInventory(unittest.TestCase):
    """Every customer-data table has a Wall-1 RLS migration on file."""

    def test_all_wall_1_migrations_exist(self):
        missing = []
        for e in WALL_1_ENTRIES:
            path = VERSIONS_DIR / f"{e.revision}.py"
            if not path.exists():
                missing.append((e.db_table, e.revision))
        self.assertEqual(
            missing, [],
            f"Wall-1 migrations missing on disk: {missing}",
        )

    def test_inventory_size_floor(self):
        self.assertGreaterEqual(
            len(WALL_1_ENTRIES), 17,
            "Wall-1 inventory shrank below the post-Gap-7 baseline of 17 "
            "tables (16 from C3 + messages from C5.1). Pre-Gap-7 baseline "
            "was 18 (included agent_configs); Arc 10 Gap 7 removed "
            "agent_configs after Arc 5C dropped the underlying table.",
        )

    def test_no_duplicate_revisions(self):
        revs = [e.revision for e in WALL_1_ENTRIES]
        self.assertEqual(
            len(revs), len(set(revs)),
            "Duplicate revision id in WALL_1_ENTRIES",
        )

    def test_documented_variations_have_rationale(self):
        """If a Wall-1 entry deviates from the default shape, it MUST
        carry a non-empty rationale string. Forces the next developer
        to read why before editing.

        Default after Arc 10 Gap 7 = (wall_column='tenant_id',
        no policy_name override, auth_perimeter=False). Pre-Arc-9.2
        C3 migrations all match this default because their source text
        carries the pre-rename ``tenant_id`` column name.
        """
        for e in WALL_1_ENTRIES:
            deviates = (
                e.wall_column != "tenant_id"
                or e.policy_name is not None
                or e.auth_perimeter
            )
            if deviates:
                self.assertTrue(
                    e.rationale.strip(),
                    f"{e.db_table}: deviating Wall-1 entry must carry "
                    "a non-empty rationale string.",
                )


class TestWall1PolicyShape(unittest.TestCase):
    """For every Wall-1 migration, assert the policy SQL has the
    canonical (or documented-deviation) shape.

    Canonical:
        ENABLE ROW LEVEL SECURITY
        CREATE POLICY <table>_tenant_isolation
          USING (<wall_column> = current_setting('app.admin_id', true))
          WITH CHECK (<wall_column> = current_setting('app.admin_id', true))

    Auth-perimeter (api_keys):
        CREATE POLICY tenant_isolation
          USING (true)
          WITH CHECK (
            (admin_id IS NULL AND current_setting('app.admin_id', true) = 'platform')
            OR admin_id = current_setting('app.admin_id', true)
          )
    """

    def _check_canonical(self, e: Wall1Entry) -> None:
        body = _read_migration(e.revision).lower()
        table = e.db_table.lower()
        col = e.wall_column.lower()

        # ENABLE RLS on the actual DB table name
        self.assertRegex(
            body,
            rf"alter\s+table\s+{re.escape(table)}\s+enable\s+row\s+level\s+security",
            f"{table}: missing ENABLE ROW LEVEL SECURITY",
        )

        # CREATE POLICY with expected name
        self.assertIn(
            f"create policy {e.expected_policy_name().lower()}",
            body,
            f"{table}: missing CREATE POLICY {e.expected_policy_name()}",
        )

        # Canonical predicate present on the wall column
        predicate = f"{col} = current_setting('app.admin_id', true)"
        self.assertIn(
            predicate,
            body,
            f"{table}: Wall-1 predicate '{predicate}' not found",
        )

        # No Wall-3 GUC leaking into Wall-1 file
        upgrade_section = body.split("def downgrade", 1)[0]
        self.assertNotIn(
            "current_setting('app.instance_id'",
            upgrade_section,
            f"{table}: Wall-1 migration leaks Wall-3 GUC into its policy",
        )

    def _check_auth_perimeter(self, e: Wall1Entry) -> None:
        body = _read_migration(e.revision).lower()
        table = e.db_table.lower()
        col = e.wall_column.lower()  # source-as-written, see Wall1Entry docstring

        self.assertRegex(
            body,
            rf"alter\s+table\s+{re.escape(table)}\s+enable\s+row\s+level\s+security",
            f"{table}: missing ENABLE ROW LEVEL SECURITY",
        )

        self.assertIn(
            f"create policy {e.expected_policy_name().lower()}",
            body,
            f"{table}: missing CREATE POLICY {e.expected_policy_name()}",
        )

        # USING is permissive (literal `true` inside USING block)
        self.assertRegex(
            body,
            r"using\s*\(\s*true\s*\)",
            f"{table}: auth-perimeter USING is not `using (true)`",
        )

        # WITH CHECK references the 'platform' sentinel and the wall-column
        # equality. Both branches required. We look for the column name AS
        # WRITTEN in the migration source (pre-Arc-9.2 migrations carry
        # ``tenant_id`` text even though the live column is now
        # ``admin_id`` post-PR-#101).
        self.assertIn(
            f"{col} is null",
            body,
            f"{table}: auth-perimeter WITH CHECK is missing the "
            f"NULL-{col} branch (platform sentinel)",
        )
        self.assertIn(
            "= 'platform'",
            body,
            f"{table}: auth-perimeter WITH CHECK is missing the "
            "'platform' sentinel value",
        )
        self.assertIn(
            f"{col} = current_setting('app.admin_id', true)",
            body,
            f"{table}: auth-perimeter WITH CHECK is missing the "
            f"non-null {col} equality branch",
        )

    def _dispatch(self, e: Wall1Entry) -> None:
        if e.auth_perimeter:
            self._check_auth_perimeter(e)
        else:
            self._check_canonical(e)

    # One @test method per table -- makes regressions easy to read in CI.
    def test_admin_audit_logs(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "admin_audit_logs"))

    def test_traces(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "traces"))

    def test_memory_items(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "memory_items"))

    def test_conversations(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "conversations"))

    # test_agent_configs: REMOVED (Arc 10 Gap 7,
    # D-arc10-close-path-imports-deleted-agent-repository-2026-05-27).
    # The `agent_configs` table was DROPPED at Arc 5 Path A; the entry
    # was removed from WALL_1_ENTRIES so dispatching to it would raise
    # AttributeError on _by_table.

    def test_sessions(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "sessions"))

    def test_subscriptions(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "subscriptions"))

    def test_scope_assignments(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "scope_assignments"))

    def test_knowledge_embeddings(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "knowledge_embeddings"))

    def test_api_keys_auth_perimeter(self):
        e = _by_table(WALL_1_ENTRIES, "api_keys")
        self.assertTrue(
            e.auth_perimeter,
            "api_keys entry must be marked auth_perimeter=True",
        )
        self._dispatch(e)

    def test_user_invites(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "user_invites"))

    def test_user_consents(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "user_consents"))

    def test_identity_claims(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "identity_claims"))

    def test_instances_admin_id_column(self):
        e = _by_table(WALL_1_ENTRIES, "instances")
        self.assertEqual(e.wall_column, "admin_id")
        self._dispatch(e)

    def test_admin_widget_domains_admin_id_column(self):
        e = _by_table(WALL_1_ENTRIES, "admin_widget_domains")
        self.assertEqual(e.wall_column, "admin_id")
        self._dispatch(e)

    def test_retention_policies(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "retention_policies"))

    def test_deletion_logs(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "deletion_logs"))

    def test_messages(self):
        self._dispatch(_by_table(WALL_1_ENTRIES, "messages"))


def _by_table(entries, table_name):
    """Helper: look up a Wall1Entry/Wall3Entry by db_table."""
    for e in entries:
        if e.db_table == table_name:
            return e
    raise KeyError(f"No inventory entry for table '{table_name}'")


# =====================================================================
# CROSS-SESSION (WALL 3) LEAK GUARDS
# =====================================================================
class TestWall3CoverageInventory(unittest.TestCase):

    def test_all_wall_3_migrations_exist(self):
        missing = []
        for e in WALL_3_ENTRIES:
            path = VERSIONS_DIR / f"{e.revision}.py"
            if not path.exists():
                missing.append((e.db_table, e.revision))
        self.assertEqual(missing, [], f"Wall-3 missing: {missing}")

    def test_inventory_size_floor(self):
        self.assertGreaterEqual(
            len(WALL_3_ENTRIES), 7,
            "Wall-3 inventory shrank below baseline of 7 tables.",
        )


class TestWall3PolicyShape(unittest.TestCase):
    """Every Wall-3 migration uses the NULL-permissive asymmetric shape.

        CREATE POLICY <table>_instance_isolation
          USING (
            luciel_instance_id::text = current_setting('app.instance_id', true)
            OR luciel_instance_id IS NULL
          )
          WITH CHECK (
            luciel_instance_id::text = current_setting('app.instance_id', true)
            OR (luciel_instance_id IS NULL
                AND current_setting('app.instance_id', true) = '')
          )
    """

    def _check(self, e: Wall3Entry) -> None:
        body = _read_migration(e.revision).lower()
        table = e.db_table.lower()

        self.assertRegex(
            body,
            rf"alter\s+table\s+{re.escape(table)}\s+enable\s+row\s+level\s+security",
            f"{table}: Wall-3 missing ENABLE ROW LEVEL SECURITY",
        )

        self.assertIn(
            f"create policy {table}_instance_isolation",
            body,
            f"{table}: missing CREATE POLICY {table}_instance_isolation",
        )

        self.assertIn(
            "luciel_instance_id::text = current_setting('app.instance_id', true)",
            body,
            f"{table}: Wall-3 missing canonical luciel_instance_id::text predicate",
        )

        self.assertIn(
            "or luciel_instance_id is null",
            body,
            f"{table}: Wall-3 missing NULL-permissive carveout on USING",
        )

        self.assertIn(
            "and current_setting('app.instance_id', true) = ''",
            body,
            f"{table}: Wall-3 WITH CHECK missing asymmetric empty-GUC guard",
        )

        # Integer ::text cast must be present
        self.assertIn("luciel_instance_id::text", body)

    def test_api_keys(self):
        self._check(_by_table(WALL_3_ENTRIES, "api_keys"))

    def test_knowledge_embeddings(self):
        self._check(_by_table(WALL_3_ENTRIES, "knowledge_embeddings"))

    def test_memory_items(self):
        self._check(_by_table(WALL_3_ENTRIES, "memory_items"))

    def test_sessions(self):
        self._check(_by_table(WALL_3_ENTRIES, "sessions"))

    def test_traces(self):
        self._check(_by_table(WALL_3_ENTRIES, "traces"))

    def test_admin_audit_logs(self):
        self._check(_by_table(WALL_3_ENTRIES, "admin_audit_logs"))

    def test_messages(self):
        self._check(_by_table(WALL_3_ENTRIES, "messages"))


class TestWall3DowngradeDoesNotDisableRLS(unittest.TestCase):
    """A Wall-3 downgrade that runs ``ALTER TABLE ... DISABLE ROW LEVEL
    SECURITY`` neuters any Wall-1 sibling policy on the same table.
    Every Wall-3 downgrade MUST drop only its own policy.

    This regression catcher fired on the C4.3c/d/e downgrades during
    C5.4 development -- those migrations carried a stale "no C3 policy
    on this table" comment and were silently disabling RLS. Fixed in
    the same PR as this test file.
    """

    def _check(self, e: Wall3Entry) -> None:
        text = _read_migration(e.revision)
        table = e.db_table.lower()
        downgrade_idx = text.find("def downgrade")
        self.assertGreater(downgrade_idx, -1, f"{table}: no downgrade()")
        body = text[downgrade_idx:].lower()

        self.assertIn(
            f"drop policy if exists {table}_instance_isolation",
            body,
            f"{table}: Wall-3 downgrade does not DROP its own policy",
        )

        self.assertNotIn(
            f"alter table {table} disable row level security",
            body,
            f"{table}: Wall-3 downgrade DISABLES RLS -- this would "
            "neuter the Wall-1 sibling policy on the same table. "
            "Drop only this Wall-3 policy on rollback.",
        )

    def test_api_keys(self):
        self._check(_by_table(WALL_3_ENTRIES, "api_keys"))

    def test_knowledge_embeddings(self):
        self._check(_by_table(WALL_3_ENTRIES, "knowledge_embeddings"))

    def test_memory_items(self):
        self._check(_by_table(WALL_3_ENTRIES, "memory_items"))

    def test_sessions(self):
        self._check(_by_table(WALL_3_ENTRIES, "sessions"))

    def test_traces(self):
        self._check(_by_table(WALL_3_ENTRIES, "traces"))

    def test_admin_audit_logs(self):
        self._check(_by_table(WALL_3_ENTRIES, "admin_audit_logs"))

    def test_messages(self):
        self._check(_by_table(WALL_3_ENTRIES, "messages"))


# =====================================================================
# WALL 4 -- INTRA-TENANT SESSION ISOLATION ON MESSAGES
# =====================================================================
class TestWall4MessagesPosture(unittest.TestCase):
    """The messages table is the only v1 surface with full Wall 4:
        L1 in-app   : add_message refuses orphan inserts + inherits parent scope
        L2 Wall-1   : messages_tenant_isolation (strict)
        L2 Wall-3   : messages_instance_isolation (NULL-permissive asymmetric)
        L3 GUCs     : C2+C4.1 listener emits both app.admin_id + app.instance_id

    L2 walls anchored by Wall_1 / Wall_3 tests above. L3 listener
    anchored by tests/db/test_tenant_context.py. THIS class anchors L1.
    """

    REPO_FILE = "app/repositories/session_repository.py"
    MODEL_FILE = "app/models/message.py"

    def _read(self, rel_path: str) -> str:
        path = Path(__file__).parent.parent.parent / rel_path
        self.assertTrue(path.exists(), f"{rel_path} missing")
        return path.read_text()

    def test_session_repository_fetches_parent_via_db_get(self):
        body = self._read(self.REPO_FILE)
        self.assertIn(
            "self.db.get(SessionModel",
            body,
            "SessionRepository.add_message must fetch its parent session "
            "via self.db.get(SessionModel, session_id) inside the same "
            "RLS-scoped txn.",
        )

    def test_session_repository_raises_on_missing_parent(self):
        body = self._read(self.REPO_FILE)
        self.assertIn(
            "raise ValueError",
            body,
            "SessionRepository.add_message must raise (ValueError) "
            "when its parent session is missing under the current RLS scope.",
        )

    def test_message_model_has_tenant_id_not_null(self):
        body = self._read(self.MODEL_FILE)
        self.assertIn("admin_id", body, "MessageModel missing admin_id")
        has_explicit_not_null = (
            re.search(
                r"admin_id[^\n]*nullable\s*=\s*false",
                body.lower(),
            ) is not None
        )
        has_typed_not_null = (
            re.search(
                r"admin_id\s*:\s*Mapped\[\s*str\s*\]",
                body,
            ) is not None
        )
        self.assertTrue(
            has_explicit_not_null or has_typed_not_null,
            "MessageModel.admin_id must be NOT NULL.",
        )

    def test_message_model_luciel_instance_id_not_null(self):
        """messages.luciel_instance_id must be NOT NULL.

        Architecture v1 §3.7.3 (Wall 3 — Cross-Instance Within an
        Admin) is unambiguous: "every customer-data row carries
        instanceid as a non-null indexed column. Default retrieval
        scope WHERE adminid = x AND instanceid = y." The messages
        table is customer-data; therefore luciel_instance_id is
        NOT NULL.

        This test previously asserted the field was NULLABLE, which
        directly contradicted Wall 3. Updated under the founder
        directive: when a test and the business document conflict,
        the business document wins.
        """
        body = self._read(self.MODEL_FILE)
        self.assertIn("luciel_instance_id", body)
        has_typed_not_null = (
            re.search(
                r"luciel_instance_id\s*:\s*Mapped\[\s*int\s*\]",
                body,
            ) is not None
        )
        has_explicit_not_null = (
            re.search(
                r"luciel_instance_id[^\n]*nullable\s*=\s*false",
                body.lower(),
            ) is not None
        )
        self.assertTrue(
            has_typed_not_null or has_explicit_not_null,
            "MessageModel.luciel_instance_id must be NOT NULL per "
            "Architecture v1 §3.7.3 (Wall 3: every customer-data row "
            "carries instanceid as a non-null indexed column).",
        )

    def test_add_message_inherits_parent_scope(self):
        body = self._read(self.REPO_FILE)
        self.assertRegex(
            body, r"\.admin_id",
            "add_message must read admin_id off the parent session.",
        )
        self.assertRegex(
            body, r"\.luciel_instance_id",
            "add_message must read luciel_instance_id off the parent session.",
        )


# =====================================================================
# CROSS-WALL CONSISTENCY -- GUC naming + listener pairing
# =====================================================================
class TestGUCNamingDiscipline(unittest.TestCase):
    """Every Wall-1 policy MUST use app.admin_id. Every Wall-3 policy
    MUST use app.instance_id. Drift onto e.g. 'app.admin_id' would
    silently disconnect the policy from the C2/C4.1 listener (which
    only sets app.admin_id + app.instance_id) -- the policy would
    always see empty GUC and behave wrongly."""

    def test_every_wall_1_uses_app_admin_id(self):
        offenders = []
        for e in WALL_1_ENTRIES:
            body = _read_migration(e.revision).lower()
            upgrade_section = body.split("def downgrade", 1)[0]
            if "current_setting('app.admin_id'" not in upgrade_section:
                offenders.append((e.db_table, e.revision))
        self.assertEqual(
            offenders, [],
            f"Wall-1 migrations not referencing app.admin_id: {offenders}",
        )

    def test_every_wall_3_uses_app_instance_id(self):
        offenders = []
        for e in WALL_3_ENTRIES:
            body = _read_migration(e.revision).lower()
            upgrade_section = body.split("def downgrade", 1)[0]
            if "current_setting('app.instance_id'" not in upgrade_section:
                offenders.append((e.db_table, e.revision))
        self.assertEqual(
            offenders, [],
            f"Wall-3 migrations not referencing app.instance_id: {offenders}",
        )


class TestListenerPairsBothGUCs(unittest.TestCase):
    """The C2 listener sets app.admin_id; C4.1 added the paired
    app.instance_id call. The pair forms the L3 binding that activates
    Wall-1 + Wall-3 in production. Detailed behavior covered by
    tests/db/test_tenant_context.py; here we anchor the source-level
    invariant: both GUC names appear in the listener module."""

    def test_listener_emits_both_guc_names(self):
        session_path = Path(__file__).parent.parent.parent / "app" / "db" / "session.py"
        self.assertTrue(session_path.exists())
        body = session_path.read_text()
        self.assertIn(
            "app.admin_id", body,
            "session.py listener missing app.admin_id GUC binding.",
        )
        self.assertIn(
            "app.instance_id", body,
            "session.py listener missing app.instance_id GUC binding "
            "-- C4.1 pairing broken.",
        )


class TestMasterFlagGatesListener(unittest.TestCase):
    """The whole RLS context-injection mechanism is gated behind
    settings.rls_tenant_context_enabled. While that flag is False
    (the v1 default), the listener no-ops. The flag MUST exist;
    losing it would unblock a half-deployed Wall against unprepared
    call sites."""

    def test_flag_present_in_settings(self):
        config_path = Path(__file__).parent.parent.parent / "app" / "core" / "config.py"
        self.assertTrue(config_path.exists())
        body = config_path.read_text()
        self.assertIn(
            "rls_tenant_context_enabled", body,
            "Master flag rls_tenant_context_enabled missing -- "
            "the listener gate is gone.",
        )


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
