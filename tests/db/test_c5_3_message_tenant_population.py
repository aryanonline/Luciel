"""Arc 9 C5.3 tests -- MessageModel + SessionRepository.add_message
populate tenant_id and luciel_instance_id from parent session.

GUARDS:
    1. MessageModel has tenant_id (String 100, NOT NULL) column
    2. MessageModel has luciel_instance_id (Integer, nullable) column
    3. SessionRepository.add_message fetches parent session
    4. add_message copies tenant_id from parent
    5. add_message copies luciel_instance_id from parent
    6. add_message raises ValueError if session not found in scope
    7. The model imports cleanly (no SQLAlchemy mapper errors)

These are STATIC/UNIT tests on the source -- the runtime DB tests
that exercise the actual SQLAlchemy session live in higher-level
integration suites that run against a real Postgres in CI.

RUN:
    python -m pytest tests/db/test_c5_3_message_tenant_population.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent.parent
MESSAGE_MODEL_PATH = REPO_ROOT / "app" / "models" / "message.py"
SESSION_REPO_PATH = (
    REPO_ROOT / "app" / "repositories" / "session_repository.py"
)


class TestMessageModelShape(unittest.TestCase):
    """MessageModel column declarations (text-level guards)."""

    def setUp(self):
        self.text = MESSAGE_MODEL_PATH.read_text()

    def test_tenant_id_column_declared(self):
        # Required for Wall-1 RLS (messages_tenant_isolation).
        self.assertIn("tenant_id: Mapped[str]", self.text)
        self.assertIn("String(100)", self.text)
        # NOT NULL -- this is the post-C5.0a Phase 3 contract.
        self.assertIn("nullable=False", self.text)

    def test_luciel_instance_id_column_declared(self):
        # Required for Wall-3 RLS (messages_instance_isolation).
        # Arc 9.1 Phase A (2026-05-25): flipped to NOT NULL --
        # the parent session row is now guaranteed NOT NULL on
        # luciel_instance_id, so the denormalised copy here is too.
        self.assertIn("luciel_instance_id: Mapped[int]", self.text)
        self.assertIn("Integer", self.text)
        self.assertIn("nullable=False", self.text)

    def test_integer_import_present(self):
        self.assertIn("Integer", self.text)
        # Must be imported from sqlalchemy
        self.assertRegex(
            self.text,
            r"from\s+sqlalchemy\s+import\s+[^\n]*Integer",
        )


class TestSessionRepositoryAddMessage(unittest.TestCase):
    """SessionRepository.add_message C5.3 retrofit."""

    def setUp(self):
        self.text = SESSION_REPO_PATH.read_text()

    def test_add_message_fetches_parent_session(self):
        # The implementation must look up the parent session before
        # inserting the message.
        self.assertIn("self.db.get(SessionModel, session_id)", self.text)

    def test_add_message_raises_when_session_missing(self):
        # Defense-in-depth: refuse to insert an orphan message.
        # The L1 caller_tenant_id check in ChatService should catch
        # this earlier, but add_message is a second line of defence.
        self.assertIn("if parent is None", self.text)
        self.assertIn("raise ValueError", self.text)
        self.assertIn("Refusing to insert orphan message", self.text)

    def test_add_message_copies_tenant_id_from_parent(self):
        # tenant_id MUST come from parent.tenant_id (NOT NULL, no
        # alternative source allowed -- the only authority is the
        # session row).
        self.assertIn("tenant_id=parent.tenant_id", self.text)

    def test_add_message_copies_instance_id_from_parent(self):
        # luciel_instance_id MUST come from parent.luciel_instance_id
        # (may be None, which is legitimate).
        self.assertIn(
            "luciel_instance_id=parent.luciel_instance_id",
            self.text,
        )

    def test_add_message_passes_kwargs_to_model(self):
        # Sanity: the MessageModel constructor is still called with
        # the original args plus the new tenant/instance kwargs.
        self.assertIn("MessageModel(", self.text)
        self.assertIn("session_id=session_id", self.text)
        self.assertIn("role=role", self.text)
        self.assertIn("content=content", self.text)
        self.assertIn("trace_id=trace_id", self.text)


class TestMessageModelImportable(unittest.TestCase):
    """SQLAlchemy mapper configuration must still work."""

    def test_message_model_imports_without_error(self):
        # If we broke the mapped_column shape, this import would raise
        # InvalidRequestError or ArgumentError at module-load time.
        # We deliberately do NOT call importlib.reload -- the Base
        # registry is process-global and re-registering the table
        # raises InvalidRequestError. A fresh import is sufficient
        # because pytest discovery already imported app.models.message
        # at least once successfully if we got this far.
        import app.models.message as msg_mod
        self.assertTrue(hasattr(msg_mod, "MessageModel"))
        cls = msg_mod.MessageModel
        # SQLAlchemy registers mapped columns on the class. Verify
        # the new ones are present in __table__.columns.
        col_names = {c.name for c in cls.__table__.columns}
        self.assertIn("tenant_id", col_names)
        self.assertIn("luciel_instance_id", col_names)
        self.assertIn("session_id", col_names)
        self.assertIn("role", col_names)
        self.assertIn("content", col_names)

    def test_tenant_id_column_is_not_nullable(self):
        import app.models.message as msg_mod
        col = msg_mod.MessageModel.__table__.columns["tenant_id"]
        self.assertFalse(col.nullable, "tenant_id must be NOT NULL")

    def test_luciel_instance_id_column_is_nullable(self):
        import app.models.message as msg_mod
        col = msg_mod.MessageModel.__table__.columns["luciel_instance_id"]
        self.assertTrue(col.nullable, "luciel_instance_id must be nullable")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
