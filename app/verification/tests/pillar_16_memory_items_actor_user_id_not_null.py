"""Pillar 16 - memory_items.actor_user_id NOT NULL constraint (D11).

Drift item D11 from the Step 24.5b canonical recap. Step 24.5b's additive
migration (4e989b9392c0_add_memory_items_actor_user_id.py) explicitly
deferred the NOT NULL flip to a post-24.5b sweep. Step 28 Phase 1
Commit 5 closed that deferral via:

  - Pre-flight orphan sweep (10 historical local-dev rows, audit row 2086)
  - Hand-written migration 4e4c2c0fb572_d11_memory_items_actor_user_id_
    not_null.py flipping nullable=True -> nullable=False

This pillar is the regression guard. Asserts two contracts:

  1. SCHEMA LAYER: information_schema.columns reports
     is_nullable='NO' for memory_items.actor_user_id. Catches accidental
     future migration that relaxes the constraint.

  2. ENFORCEMENT LAYER: a direct INSERT INTO memory_items with
     actor_user_id=NULL raises sqlalchemy.exc.IntegrityError. Catches the
     case where the constraint exists in metadata but isn't actually
     enforced by Postgres (paranoid but cheap).

The enforcement-layer test runs inside a SAVEPOINT that always rolls
back, so no test data lands in the table regardless of pass/fail.

Read-only effective. Safe to run pre- or post-teardown.
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.verification.fixtures import RunState
from app.verification.runner import Pillar


_SCHEMA_SQL = """
SELECT is_nullable
FROM information_schema.columns
WHERE table_name = 'memory_items'
  AND column_name = 'actor_user_id'
"""


_INSERT_NULL_SQL = """
INSERT INTO memory_items (
    tenant_id, category, content, actor_user_id, created_at
) VALUES (
    :tenant_id, :category, :content, NULL, NOW()
)
"""


class MemoryItemsActorUserIdNotNullPillar(Pillar):
    number = 16
    name = "memory_items.actor_user_id NOT NULL (D11)"

    def run(self, state: RunState) -> str:
        db_url = os.environ.get("DATABASE_URL") or self._load_database_url_from_dotenv()
        if not db_url:
            raise AssertionError(
                "DATABASE_URL not found in environment nor in project .env file. "
                "Either export it or ensure .env is readable from the project root."
            )

        engine = create_engine(db_url)

        # 1. SCHEMA LAYER -- information_schema must report NO
        with engine.connect() as conn:
            row = conn.execute(text(_SCHEMA_SQL)).one_or_none()
            if row is None:
                raise AssertionError(
                    "memory_items.actor_user_id column not found in "
                    "information_schema. Migration 4e989b9392c0 may not "
                    "have run -- D11 cannot be enforced."
                )
            if row.is_nullable != "NO":
                raise AssertionError(
                    f"memory_items.actor_user_id is_nullable={row.is_nullable!r} "
                    f"-- expected 'NO'. D11 NOT NULL constraint is not in "
                    f"effect. Migration 4e4c2c0fb572 may have been "
                    f"downgraded or never applied."
                )

        # 2. ENFORCEMENT LAYER -- direct INSERT with NULL must raise IntegrityError
        sentinel_tenant = f"step28-d11-pillar16-{uuid.uuid4().hex[:8]}"
        raised = False
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(_INSERT_NULL_SQL),
                    {
                        "tenant_id": sentinel_tenant,
                        "category": "fact",
                        "content": "pillar 16 enforcement probe -- should never persist",
                    },
                )
        except IntegrityError:
            raised = True
        except Exception as exc:
            raise AssertionError(
                f"pillar 16 enforcement probe raised unexpected exception type: "
                f"{type(exc).__name__}: {exc}"
            )

        if not raised:
            # Insert succeeded -- constraint is NOT enforced. Clean up
            # the row so this pillar leaves the DB as it found it.
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM memory_items WHERE tenant_id = :tid"),
                    {"tid": sentinel_tenant},
                )
            raise AssertionError(
                "pillar 16 enforcement probe: NULL actor_user_id INSERT "
                "succeeded. Schema reports NOT NULL but Postgres did not "
                "reject the row. This is a critical drift -- the constraint "
                "is metadata-only and not actually enforced."
            )

        engine.dispose()

        return (
            "schema is_nullable=NO; "
            "INSERT (..., actor_user_id=NULL) raised IntegrityError as expected"
        )

    @staticmethod
    def _load_database_url_from_dotenv() -> str | None:
        """Walk up from CWD looking for a .env file; return DATABASE_URL if present."""
        from pathlib import Path
        here = Path.cwd().resolve()
        for candidate_dir in (here, *here.parents):
            env_path = candidate_dir / ".env"
            if env_path.is_file():
                try:
                    for raw in env_path.read_text(encoding="utf-8").splitlines():
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("DATABASE_URL=") and "://" in line:
                            val = line.split("=", 1)[1].strip()
                            if (val.startswith('"') and val.endswith('"')) or (
                                val.startswith("'") and val.endswith("'")
                            ):
                                val = val[1:-1]
                            return val
                except Exception:
                    continue
        return None


PILLAR = MemoryItemsActorUserIdNotNullPillar()