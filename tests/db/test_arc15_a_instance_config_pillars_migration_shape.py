"""Arc 15 WU1 — shape guard for the instance-config-pillars migration.

Backend-free AST/text shape test (no live DB; mirrors the C3.2 RLS
shape-test pattern). Pins:

  1. revision id matches filename + chains to arc14_u4_leads
  2. the personality_preset PG enum is created with the 5 canonical
     values and create_type=False (matches instance_status pattern)
  3. all six WU1/WU3 columns are added: website, personality_preset,
     personality_axes, business_context, lead_routing, escalation_config
  4. system_prompt_additions is NOT dropped (deprecation, not removal)
  5. downgrade drops the six columns + the enum type
  6. the module imports cleanly (catches syntax / bad-import drift)
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

VERSIONS_DIR = Path(__file__).parent.parent.parent / "app" / "migrations" / "versions"
REV_ID = "arc15_a_instance_config_pillars"
DOWN_REV = "arc14_u4_leads"
PRESET_VALUES = (
    "warm_concierge",
    "professional_advisor",
    "friendly_expert",
    "trusted_authority",
    "custom",
)
COLUMNS = (
    "website",
    "personality_preset",
    "personality_axes",
    "business_context",
    "lead_routing",
    "escalation_config",
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


def test_chains_to_arc14_u4_leads() -> None:
    m = re.search(r'^down_revision\s*=\s*"([^"]+)"', _text(), re.MULTILINE)
    assert m and m.group(1) == DOWN_REV


def test_preset_enum_created_with_canonical_values() -> None:
    text = _text()
    for value in PRESET_VALUES:
        assert f'"{value}"' in text, value
    # create_type=False + explicit .create() (the instance_status pattern)
    assert "create_type=False" in text
    assert ".create(" in text


def test_all_pillar_columns_added() -> None:
    text = _text()
    for col in COLUMNS:
        assert f'add_column' in text
        assert f'"{col}"' in text, col


def test_jsonb_columns_use_jsonb() -> None:
    text = _text()
    assert "JSONB" in text


def test_system_prompt_additions_not_dropped() -> None:
    # Deprecation, not removal — the column must survive this migration.
    assert "drop_column" in _text()  # there ARE drops (in downgrade)
    assert 'drop_column(_TABLE, "system_prompt_additions"' not in _text()
    assert "system_prompt_additions" not in _text().split("def upgrade")[1].split(
        "def downgrade"
    )[0]


def test_downgrade_drops_all_columns_and_enum() -> None:
    down = _text().split("def downgrade")[1]
    for col in COLUMNS:
        assert f'"{col}"' in down, col
    assert ".drop(" in down


def test_module_imports_clean() -> None:
    module = _load()
    assert module.revision == REV_ID
    assert module.down_revision == DOWN_REV
    assert callable(module.upgrade)
    assert callable(module.downgrade)
