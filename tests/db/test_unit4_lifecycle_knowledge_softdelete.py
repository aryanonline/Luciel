"""Unit 4 (lifecycle) — live regression: knowledge soft-delete on Luciel
deactivation and restore on reactivation (Architecture §3.6.3 step 3 /
§3.6.4).

§3.6.3 step 3 requires that deactivating a Luciel sets its knowledge
sources to ``soft_deleted``; §3.6.4 requires reactivation to mark them
active again. The retriever filters ``soft_deleted_at IS NULL``, so this
is what makes a deactivated Luciel's knowledge non-retrievable during the
30-day grace window while remaining restorable.

Also pins the scoped-restore guarantee: a source the admin soft-deleted
*independently* BEFORE deactivating the Luciel must STAY deleted on
reactivation (we only reverse what the deactivation cascade stamped).

Opt-in convention: set LUCIEL_LIVE_POSTGRES_URL to run, otherwise skipped.
"""
from __future__ import annotations

import os
import unittest
import uuid

import sqlalchemy as sa


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run live lifecycle tests",
)
class TestUnit4LifecycleKnowledgeSoftDelete(unittest.TestCase):

    def setUp(self):
        from app.db.session import SessionLocal

        self.db = SessionLocal()
        self.admin_id = f"free-{uuid.uuid4().hex[:8]}"
        self.db.execute(sa.text(
            "INSERT INTO admins (id, name, tier, tier_source, active) "
            "VALUES (:a, :n, 'free', 'free_signup', true)"
        ), {"a": self.admin_id, "n": f"luciel-{self.admin_id}"})
        from app.models.instance import Instance
        inst = Instance(
            admin_id=self.admin_id,
            instance_slug=f"slug-{self.admin_id[:8]}",
            display_name="L",
        )
        self.db.add(inst)
        self.db.flush()
        self.instance_id = inst.id
        self.db.commit()

    def tearDown(self):
        # FK-safe teardown; preserve nothing (this is throwaway test data).
        for tbl in (
            "knowledge_chunks", "knowledge_sources", "admin_audit_logs",
            "conversations",
        ):
            self.db.execute(
                sa.text(f"DELETE FROM {tbl} WHERE admin_id = :a"),
                {"a": self.admin_id},
            )
        self.db.execute(
            sa.text("DELETE FROM instances WHERE admin_id = :a"),
            {"a": self.admin_id},
        )
        self.db.execute(
            sa.text("DELETE FROM admins WHERE id = :a"),
            {"a": self.admin_id},
        )
        self.db.commit()
        self.db.close()

    def _seed_source(self) -> int:
        self.db.execute(sa.text(
            "INSERT INTO knowledge_sources "
            "(admin_id, luciel_instance_id, filename, source_type, "
            " size_bytes, ingested_by, ingestion_status, created_at) "
            "VALUES (:a, :i, 't.txt', 'txt', 10, 't', 'ready', now())"
        ), {"a": self.admin_id, "i": self.instance_id})
        sid = self.db.execute(sa.text(
            "SELECT id FROM knowledge_sources WHERE luciel_instance_id = :i "
            "ORDER BY id DESC LIMIT 1"
        ), {"i": self.instance_id}).scalar()
        self.db.execute(sa.text(
            "INSERT INTO knowledge_chunks "
            "(admin_id, luciel_instance_id, source_id, content, "
            " knowledge_type, embedding, created_at, updated_at) "
            "VALUES (:a, :i, :s, 'hi', 'faq', :e, now(), now())"
        ), {"a": self.admin_id, "i": self.instance_id, "s": sid,
            "e": str([0.0] * 1536)})
        self.db.commit()
        return sid

    def _active_counts(self):
        s = self.db.execute(sa.text(
            "SELECT count(*) FROM knowledge_sources "
            "WHERE luciel_instance_id = :i AND soft_deleted_at IS NULL"
        ), {"i": self.instance_id}).scalar()
        c = self.db.execute(sa.text(
            "SELECT count(*) FROM knowledge_chunks "
            "WHERE luciel_instance_id = :i AND soft_deleted_at IS NULL"
        ), {"i": self.instance_id}).scalar()
        return s, c

    def test_deactivate_soft_deletes_knowledge_restore_revives_it(self):
        from app.repositories.instance_repository import InstanceRepository
        from app.repositories.admin_audit_repository import AuditContext

        self._seed_source()
        repo = InstanceRepository(self.db)
        ctx = AuditContext.system(label="unit4_test")

        self.assertEqual(self._active_counts(), (1, 1), "seed precondition")

        repo.delete_by_pk(self.instance_id, audit_ctx=ctx)
        self.assertEqual(
            self._active_counts(), (0, 0),
            "§3.6.3 step 3: deactivation must soft-delete knowledge",
        )

        repo.restore_by_pk(self.instance_id, audit_ctx=ctx)
        self.assertEqual(
            self._active_counts(), (1, 1),
            "§3.6.4: reactivation must mark knowledge active again",
        )

    def test_restore_does_not_revive_pre_deactivation_deletions(self):
        """A source the admin deleted BEFORE deactivating must stay
        deleted after reactivation (scoped-restore guarantee)."""
        from app.repositories.instance_repository import InstanceRepository
        from app.repositories.admin_audit_repository import AuditContext
        import time

        sid = self._seed_source()
        # Admin independently soft-deletes the source well before
        # deactivation (stamp an explicitly older soft_deleted_at).
        self.db.execute(sa.text(
            "UPDATE knowledge_sources SET soft_deleted_at = now() - interval '1 day' "
            "WHERE id = :s"
        ), {"s": sid})
        self.db.execute(sa.text(
            "UPDATE knowledge_chunks SET soft_deleted_at = now() - interval '1 day' "
            "WHERE source_id = :s"
        ), {"s": sid})
        self.db.commit()
        self.assertEqual(self._active_counts(), (0, 0), "pre-deleted precondition")

        repo = InstanceRepository(self.db)
        ctx = AuditContext.system(label="unit4_test")
        repo.delete_by_pk(self.instance_id, audit_ctx=ctx)
        repo.restore_by_pk(self.instance_id, audit_ctx=ctx)

        self.assertEqual(
            self._active_counts(), (0, 0),
            "scoped restore must NOT revive a source the admin deleted "
            "before deactivating the Luciel",
        )


if __name__ == "__main__":
    unittest.main()
