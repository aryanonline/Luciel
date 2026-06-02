"""Arc 15 WU4 — shape guard for the instance_connections migration.

Backend-free AST/text shape test (no live DB; mirrors the WU1
config-pillars shape test + the C3.2 RLS shape-test pattern). Pins:

  1. revision id matches filename + chains to arc15_a_instance_config_pillars
  2. both PG enums created with create_type=False + explicit .create()
     and carry the canonical vocabularies
  3. all §3.8.2 columns present
  4. partial unique index over (admin_id, instance_id, connection_type,
     provider) WHERE revoked_at IS NULL
  5. RLS posture mirrors arc12_wu2 (ENABLE + FORCE + PERMISSIVE policy on
     app.admin_id, fail-closed)
  6. downgrade drops table + both enums and reverses RLS
  7. the module imports cleanly
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

VERSIONS_DIR = Path(__file__).parent.parent.parent / "alembic" / "versions"
REV_ID = "arc15_b_instance_connections"
DOWN_REV = "arc15_a_instance_config_pillars"

CONN_TYPE_VALUES = (
    "calendar",
    "email_sender",
    "sms_sender",
    "crm",
    "property_source",
    "outbound_webhook",
)
CONN_STATUS_VALUES = (
    "unconfigured",
    "connected",
    "error",
    "expired",
)
COLUMNS = (
    "admin_id",
    "instance_id",
    "connection_type",
    "provider",
    "config_json",
    "credential_ref",
    "status",
    "last_verified_at",
    "created_at",
    "updated_at",
    "revoked_at",
)


def _path() -> Path:
    return VERSIONS_DIR / f"{REV_ID}.py"


def _text() -> str:
    return _path().read_text()


def _load():
    spec = importlib.util.spec_from_file_location(f"_t_{REV_ID}", str(_path()))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_file_exists() -> None:
    assert _path().exists()


def test_revision_matches_filename() -> None:
    m = re.search(r'^revision\s*=\s*"([^"]+)"', _text(), re.MULTILINE)
    assert m and m.group(1) == REV_ID


def test_chains_to_arc15_a() -> None:
    m = re.search(r'^down_revision\s*=\s*"([^"]+)"', _text(), re.MULTILINE)
    assert m and m.group(1) == DOWN_REV


def test_both_enums_created_with_canonical_values() -> None:
    text = _text()
    for value in CONN_TYPE_VALUES + CONN_STATUS_VALUES:
        assert f'"{value}"' in text, value
    assert "create_type=False" in text
    assert ".create(" in text


def test_all_columns_present() -> None:
    text = _text()
    for col in COLUMNS:
        assert f'"{col}"' in text, col


def test_config_json_is_jsonb_and_documented_non_secret() -> None:
    text = _text()
    assert "JSONB" in text
    # Honesty invariant must be documented at the column.
    assert "NON-SECRET" in text.upper() or "non-secret" in text


def test_partial_unique_index_over_active_rows() -> None:
    text = _text()
    assert "uq_instance_connections_active" in text
    assert "revoked_at IS NULL" in text
    for col in ("admin_id", "instance_id", "connection_type", "provider"):
        assert f'"{col}"' in text


def test_rls_posture_mirrors_arc12_wu2() -> None:
    up = _text().split("def upgrade")[1].split("def downgrade")[0]
    assert "ENABLE ROW LEVEL SECURITY" in up
    assert "FORCE ROW LEVEL SECURITY" in up
    assert "AS PERMISSIVE" in up
    assert "current_setting('app.admin_id', true)" in up
    assert "USING" in up and "WITH CHECK" in up


def test_downgrade_reverses_table_index_and_rls() -> None:
    down = _text().split("def downgrade")[1]
    assert "DROP POLICY IF EXISTS" in down
    assert "NO FORCE ROW LEVEL SECURITY" in down
    assert "DISABLE ROW LEVEL SECURITY" in down
    assert "drop_table" in down
    assert ".drop(" in down  # both enums dropped


def test_module_imports_clean() -> None:
    module = _load()
    assert module.revision == REV_ID
    assert module.down_revision == DOWN_REV
    assert callable(module.upgrade)
    assert callable(module.downgrade)
