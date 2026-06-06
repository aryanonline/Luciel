"""Unit 5 — align connection schema names to the §3.8.2 contract.

Architecture §3.8.2 names the secret-pointer column ``secret_ref`` and the
non-sensitive provider config ``non_secret_config``. The code (and the
``secret_cleanup_outbox`` mirror) historically called these
``credential_ref`` / ``config_json``. The full sweep renames the code,
tests, and docstrings; this migration renames the live columns:

* ``instance_connections.credential_ref``  -> ``secret_ref``
* ``instance_connections.config_json``     -> ``non_secret_config``
* ``secret_cleanup_outbox.credential_ref`` -> ``secret_ref``

The renames are GUARDED on the presence of the old column. A database
provisioned BEFORE this sweep carries the old column names and is renamed
here. A database built fresh from the current migration chain already
creates the columns under the new names (the historical create-migrations
were swept too), so the guard makes this a clean no-op there — keeping
``alembic upgrade base->head`` working either way. The downgrade is
likewise guarded and reverses all three.

Revision ID: unit5_rename_connection_secret_cols
Revises: unit4_drop_instance_status_inactive
"""
from __future__ import annotations

from alembic import op


revision = "unit5_rename_connection_secret_cols"
down_revision = "unit4_drop_instance_status_inactive"
branch_labels = None
depends_on = None


# (table, old_name, new_name)
_RENAMES = (
    ("instance_connections", "credential_ref", "secret_ref"),
    ("instance_connections", "config_json", "non_secret_config"),
    ("secret_cleanup_outbox", "credential_ref", "secret_ref"),
)


def _rename_if_present(table: str, from_col: str, to_col: str) -> None:
    """Rename ``from_col`` to ``to_col`` only when ``from_col`` exists and
    ``to_col`` does not — idempotent across fresh and pre-sweep databases."""
    conn = op.get_bind()
    has_from = conn.exec_driver_sql(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        f"AND table_name = '{table}' AND column_name = '{from_col}'"
    ).scalar()
    has_to = conn.exec_driver_sql(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        f"AND table_name = '{table}' AND column_name = '{to_col}'"
    ).scalar()
    if has_from and not has_to:
        op.alter_column(table, from_col, new_column_name=to_col)


def upgrade() -> None:
    for table, old_col, new_col in _RENAMES:
        _rename_if_present(table, old_col, new_col)


def downgrade() -> None:
    for table, old_col, new_col in _RENAMES:
        _rename_if_present(table, new_col, old_col)
