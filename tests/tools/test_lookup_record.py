"""Arc 17 — lookup_record live record-source tests.

Covers the live implementation (replacing the old INTERIM body):
  * record_id match, filters AND-match, query substring, combined,
  * clean no-match (success=True, empty),
  * unknown filter column (empty, no crash),
  * result cap / truncation,
  * missing connection row / missing store_ref (honest failure),
  * s3:// store_ref with the live flag OFF → honest deploy-gated failure
    via the resolver, with the real S3 impl NOT invoked.
  * domain-agnostic vocabulary guard (no vertical wording in the live
    code path).

Uses an in-memory SQLite session with just the columns the tool reads,
and ``LocalFileRecordSource`` / a temp CSV for the data. NO AWS.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

REPO_ROOT = Path(__file__).resolve().parents[2]

_CSV = (
    "id,name,city,status\n"
    "rec_1,Alice,Toronto,active\n"
    "rec_2,Bob,Vancouver,active\n"
    "rec_3,Carol,Toronto,inactive\n"
)


# =====================================================================
# Session + connection-row fixture
# =====================================================================


def _build_session_with_connection(store_ref: str | None, *, with_row: bool = True):
    """Minimal SQLite session carrying an instance_connections row for
    the (admin, instance, record_source) tuple the tool looks up."""
    from sqlalchemy import (
        Column,
        DateTime,
        Integer,
        String,
        Text,
        MetaData,
        Table,
        create_engine,
        func,
    )
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    md = MetaData()
    Table(
        "instance_connections",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("admin_id", String(100), nullable=False),
        Column("instance_id", Integer, nullable=False),
        Column("connection_type", String(64), nullable=False),
        Column("provider", String(64), nullable=False),
        Column("config_json", Text, nullable=True),
        Column("credential_ref", String(255), nullable=True),
        Column("status", String(32), nullable=False),
        Column("last_health_check_at", DateTime(timezone=True), nullable=True),
        Column("created_at", DateTime(timezone=True), server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), server_default=func.now()),
        Column("revoked_at", DateTime(timezone=True), nullable=True),
        # rescand_connections_schema additions (§3.8.2):
        Column("status_detail", Text, nullable=True),
        Column("created_by_user_id", String(36), nullable=True),
    )
    md.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()

    if with_row:
        from app.models.instance_connection import InstanceConnection

        config = {"store_ref": store_ref} if store_ref is not None else {}
        row = InstanceConnection(
            admin_id="tenant-a",
            instance_id=7,
            connection_type="record_source",
            provider="csv_upload",
            config_json=config,
            status="connected",
        )
        session.add(row)
        session.commit()
    return session


def _ctx(session):
    from app.tools.base import ToolContext

    return ToolContext(admin_id="tenant-a", instance_id=7, session=session)


def _run(input_dict, session):
    from app.tools.implementations.lookup_record_tool import LookupRecordTool

    tool = LookupRecordTool()
    return asyncio.run(tool.execute(input_dict, _ctx(session)))


def _csv_file(tmp_path: Path) -> str:
    p = tmp_path / "records.csv"
    p.write_text(_CSV)
    return str(p)


# =====================================================================
# Pure query-helper coverage (LocalFileRecordSource, no DB)
# =====================================================================


def _local_source(tmp_path):
    from app.integrations.record_source.local_source import (
        LocalFileRecordSource,
    )

    return LocalFileRecordSource(_csv_file(tmp_path))


def test_record_id_match(tmp_path):
    rows, truncated = _local_source(tmp_path).query(record_id="rec_2")
    assert [r["name"] for r in rows] == ["Bob"]
    assert truncated is False


def test_record_id_match_case_insensitive(tmp_path):
    rows, _ = _local_source(tmp_path).query(record_id="REC_1")
    assert [r["name"] for r in rows] == ["Alice"]


def test_filters_and_match(tmp_path):
    rows, _ = _local_source(tmp_path).query(
        filters={"city": "Toronto", "status": "active"}
    )
    assert [r["name"] for r in rows] == ["Alice"]


def test_query_substring(tmp_path):
    rows, _ = _local_source(tmp_path).query(query="vancouver")
    assert [r["name"] for r in rows] == ["Bob"]


def test_combined_criteria_and(tmp_path):
    # city=Toronto (rec_1, rec_3) AND query "carol" → only rec_3.
    rows, _ = _local_source(tmp_path).query(
        filters={"city": "Toronto"}, query="carol"
    )
    assert [r["id"] for r in rows] == ["rec_3"]


def test_no_match_returns_empty(tmp_path):
    rows, truncated = _local_source(tmp_path).query(record_id="nope")
    assert rows == []
    assert truncated is False


def test_unknown_filter_column_empty_no_crash(tmp_path):
    rows, _ = _local_source(tmp_path).query(filters={"nonexistent": "x"})
    assert rows == []


def test_result_cap_truncation(tmp_path):
    from app.integrations.record_source.base import query_rows

    rows = [{"id": str(i), "v": "same"} for i in range(120)]
    matched, truncated = query_rows(rows, query="same", cap=50)
    assert len(matched) == 50
    assert truncated is True


def test_empty_input_returns_bounded_sample(tmp_path):
    from app.integrations.record_source.base import query_rows

    rows = [{"id": str(i)} for i in range(120)]
    matched, truncated = query_rows(rows, cap=50)
    assert len(matched) == 50
    assert truncated is True


def test_no_id_column_matches_first_column():
    from app.integrations.record_source.base import query_rows

    rows = [{"sku": "A1", "name": "Widget"}, {"sku": "B2", "name": "Gadget"}]
    matched, _ = query_rows(rows, record_id="B2")
    assert [r["name"] for r in matched] == ["Gadget"]


# =====================================================================
# Full tool execute() coverage (DB session + connection row)
# =====================================================================


def test_execute_record_id_hit(tmp_path):
    session = _build_session_with_connection(_csv_file(tmp_path))
    out = _run({"record_id": "rec_1"}, session)
    assert out["success"] is True
    assert len(out["results"]) == 1
    assert out["results"][0]["name"] == "Alice"
    assert "record source" in out["output"].lower()


def test_execute_filters_hit(tmp_path):
    session = _build_session_with_connection(_csv_file(tmp_path))
    out = _run({"filters": {"city": "Toronto"}}, session)
    assert out["success"] is True
    assert {r["id"] for r in out["results"]} == {"rec_1", "rec_3"}


def test_execute_clean_no_match_is_success(tmp_path):
    session = _build_session_with_connection(_csv_file(tmp_path))
    out = _run({"record_id": "missing"}, session)
    assert out["success"] is True
    assert out["results"] == []
    assert "no matching" in out["output"].lower()


def test_execute_unknown_column_no_crash(tmp_path):
    session = _build_session_with_connection(_csv_file(tmp_path))
    out = _run({"filters": {"ghost": "x"}}, session)
    assert out["success"] is True
    assert out["results"] == []


def test_execute_truncation_flag(tmp_path):
    big = "id,v\n" + "".join(f"r{i},same\n" for i in range(120))
    p = tmp_path / "big.csv"
    p.write_text(big)
    session = _build_session_with_connection(str(p))
    out = _run({"query": "same"}, session)
    assert out["success"] is True
    assert len(out["results"]) == 50
    assert out["truncated"] is True
    assert "truncated" in out["output"].lower()


def test_execute_no_session_honest_failure():
    out = _run({"record_id": "rec_1"}, None)
    assert out["success"] is False
    assert out["results"] == []
    assert "session" in out["output"].lower()


def test_execute_missing_connection_row_honest_failure():
    session = _build_session_with_connection(None, with_row=False)
    out = _run({"record_id": "rec_1"}, session)
    assert out["success"] is False
    assert out["results"] == []
    assert "no live record source" in out["output"].lower()


def test_execute_missing_store_ref_honest_failure():
    session = _build_session_with_connection(None, with_row=True)
    out = _run({"record_id": "rec_1"}, session)
    assert out["success"] is False
    assert out["results"] == []
    assert "store_ref" in out["output"].lower()


# =====================================================================
# s3:// store_ref with the live flag OFF → honest deploy-gated failure;
# the real S3 impl is NOT constructed/invoked.
# =====================================================================


def test_s3_store_ref_deploy_gated_when_flag_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "record_source_live_enabled", False)

    # Tripwire: if the resolver were to construct/invoke the S3 impl,
    # fetch_rows would raise loudly rather than silently faking success.
    import app.integrations.record_source.s3_source as s3mod

    def _boom(self):  # pragma: no cover - must never be called
        raise AssertionError("S3RecordSource.fetch_rows must NOT be invoked")

    monkeypatch.setattr(s3mod.S3RecordSource, "fetch_rows", _boom)

    session = _build_session_with_connection("s3://bucket/records.csv")
    out = _run({"record_id": "rec_1"}, session)

    assert out["success"] is False
    assert out["results"] == []
    msg = out["output"].lower()
    assert "s3" in msg or "object storage" in msg
    assert "deploy-gated" in msg or "not reachable" in msg
    # Honest — NOT the retired interim wording.
    assert "not_yet_available" not in out
    assert "owning_arc" not in out


def test_resolver_does_not_construct_s3_when_flag_off(monkeypatch):
    from app.core.config import settings
    from app.integrations.record_source.resolver import (
        RecordSourceUnavailableError,
        resolve_record_source,
    )

    monkeypatch.setattr(settings, "record_source_live_enabled", False)
    with pytest.raises(RecordSourceUnavailableError):
        resolve_record_source("s3://bucket/key.csv", settings)


# =====================================================================
# Domain-agnostic guard — no vertical-specific vocabulary
# =====================================================================


def test_live_path_carries_no_vertical_vocabulary():
    """The tool + the record_source package must stay domain-agnostic
    (Locked Decision #5): no real-estate / vertical wording."""
    # Vertical (real-estate) vocabulary. ``property`` is deliberately
    # excluded — it collides with the ``@property`` decorator and is not
    # itself a vertical commitment; the real-estate sense is caught by
    # ``listing`` / ``mls`` / ``realtor`` etc.
    banned = re.compile(
        r"\b(real[\s-]?estate|realtor|\bmls\b|listing|"
        r"mortgage|brokerage|appraisal)\b",
        re.IGNORECASE,
    )
    files = [
        REPO_ROOT / "app/tools/implementations/lookup_record_tool.py",
        REPO_ROOT / "app/integrations/record_source/base.py",
        REPO_ROOT / "app/integrations/record_source/local_source.py",
        REPO_ROOT / "app/integrations/record_source/s3_source.py",
        REPO_ROOT / "app/integrations/record_source/resolver.py",
        REPO_ROOT / "app/integrations/record_source/__init__.py",
    ]
    for f in files:
        src = f.read_text()
        hit = banned.search(src)
        assert hit is None, f"{f.name} carries vertical vocabulary: {hit!r}"
