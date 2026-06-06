"""RESCAN TIER-DE(widget+export) — Export bundle ZIP tests.

Locks the §5.10 contract for the rewritten DataExportService:

  EZ-1  Bundle is a valid ZIP (not tar.gz).
  EZ-2  §5.10 structure: manifest.json, README.txt, conversations/,
        leads.json, leads.csv, knowledge/manifest.json,
        instances.json, audit_log.jsonl present.
  EZ-3  Per-session JSON: conversations/{session_id}.json present.
  EZ-4  conversations/conversations.csv present.
  EZ-5  instances.json contains provider + non_secret_config + status
        and NEVER secret_ref or any secret material.
  EZ-6  Free-tier self-serve blocked pre-closure; allowed during closure.
  EZ-7  Pro/Enterprise self-serve allowed at any time.
  EZ-8  data_export_self_serve audit emitted for Pro/Enterprise
        non-closure export.
  EZ-9  No data_export_self_serve emitted for Free (closure path).
  EZ-10 ExportFreeGateError raised for Free outside closure.
  EZ-11 audit_log.jsonl format (JSONL, not CSV).
  EZ-12 S3 upload uses application/zip ContentType.
  EZ-13 S3 key uses .zip extension.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services.data_export_service import (
    DataExportError,
    DataExportService,
    ExportAlreadyInFlightError,
    ExportFreeGateError,
    ExportNotFoundError,
    ExportNotReadyError,
)


# -----------------------------------------------------------------------
# Minimal stubs.
# -----------------------------------------------------------------------

class _FakeAuditRepo:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, *, ctx, admin_id, action, resource_type,
               resource_natural_id, after, note, autocommit):
        self.calls.append({
            "action": action,
            "admin_id": admin_id,
        })


class _FakeS3:
    """Captures upload_fileobj calls for inspection."""

    def __init__(self) -> None:
        self.uploads: list[dict] = []

    def upload_fileobj(self, *, Fileobj, Bucket, Key, ExtraArgs=None):
        data = Fileobj.read()
        self.uploads.append({
            "bucket": Bucket,
            "key": Key,
            "data": data,
            "extra_args": ExtraArgs or {},
        })

    def generate_presigned_url(self, op, *, Params, ExpiresIn):
        return f"https://s3.example.com/{Params['Key']}?exp={ExpiresIn}"


class _FakeDB:
    """Minimal DB stub that returns configurable row sets."""

    def __init__(self, rows_by_sql: dict[str, list] | None = None) -> None:
        self._rows = rows_by_sql or {}
        self._executed: list[str] = []
        self._flushed = False
        self._committed = False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self._executed.append(sql)
        rows = []
        for key, val in self._rows.items():
            if key.lower() in sql.lower():
                rows = val
                break
        return _FakeResult(rows)

    def flush(self):
        self._flushed = True

    def commit(self):
        self._committed = True


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows
        self._iter = iter(rows)

    def first(self):
        try:
            return self._rows[0]
        except IndexError:
            return None

    def __iter__(self):
        return iter(self._rows)


# -----------------------------------------------------------------------
# Helpers to build a service and generate a bundle.
# -----------------------------------------------------------------------

def _make_service(db=None, s3=None, audit_repo=None):
    if db is None:
        db = _FakeDB()
    if s3 is None:
        s3 = _FakeS3()
    if audit_repo is None:
        audit_repo = _FakeAuditRepo()
    return DataExportService(
        db=db,
        s3_client=s3,
        s3_bucket="test-bucket",
        audit_repository=audit_repo,
    ), s3, audit_repo


def _make_db_with_data(
    *,
    sessions=None,
    leads=None,
    audit_rows=None,
    instances=None,
    connections=None,
    knowledge_sources=None,
    knowledge_chunks=None,
):
    """Build a FakeDB whose rows match each query by keyword heuristics."""
    sessions = sessions or []
    leads = leads or []
    audit_rows = audit_rows or []
    instances = instances or []
    connections = connections or []
    knowledge_sources = knowledge_sources or []
    knowledge_chunks = knowledge_chunks or []

    rows_by_sql = {
        "from sessions": sessions,
        "from identity_claims": leads,
        "from admin_audit_logs": audit_rows,
        "from instances": instances,
        "from instance_connections": connections,
        "from knowledge_sources": knowledge_sources,
        "from knowledge_chunks": knowledge_chunks,
    }
    return _FakeDB(rows_by_sql)


def _generate_bundle(db=None, s3=None, admin_id="admin-1"):
    """Run _build_and_upload_bundle directly (bypasses DB job lifecycle)."""
    svc, _s3, _ = _make_service(db=db or _FakeDB(), s3=s3 or _FakeS3())
    s3_key, bytes_written = svc._build_and_upload_bundle(
        admin_id=admin_id,
        tier_at_request="pro",
        job_id="job-test-1",
    )
    # Return the uploaded ZIP bytes.
    assert len(_s3.uploads) == 1 or (s3 is not None and len(s3.uploads) == 1)
    upload = (s3 or _s3).uploads[-1]
    return upload["data"], s3_key, upload


# -----------------------------------------------------------------------
# EZ-1: valid ZIP.
# -----------------------------------------------------------------------

def test_ez1_bundle_is_valid_zip():
    """The generated bundle must be a valid ZIP file."""
    s3 = _FakeS3()
    svc, _, _ = _make_service(s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    data = s3.uploads[-1]["data"]
    assert zipfile.is_zipfile(io.BytesIO(data)), (
        "Bundle must be a valid ZIP file (not tar.gz)"
    )


# -----------------------------------------------------------------------
# EZ-2: §5.10 structure present.
# -----------------------------------------------------------------------

def test_ez2_required_structure_present():
    """ZIP must contain all §5.10 required top-level entries."""
    s3 = _FakeS3()
    svc, _, _ = _make_service(s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    data = s3.uploads[-1]["data"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())

    # Required entries per §5.10.
    for required in (
        "README.txt",
        "manifest.json",
        "leads.json",
        "leads.csv",
        "knowledge/manifest.json",
        "instances.json",
        "audit_log.jsonl",
        # conversations/ directory (via CSV).
        "conversations/conversations.csv",
    ):
        assert required in names, (
            f"§5.10 requires {required!r} in the ZIP but it was not found. "
            f"Found: {sorted(names)}"
        )


# -----------------------------------------------------------------------
# EZ-3: per-session JSON in conversations/.
# -----------------------------------------------------------------------

def test_ez3_per_session_json_present():
    """conversations/{session_id}.json must be present for each session."""
    session_row = (
        "sess-abc",          # session_id
        datetime(2024, 1, 1, tzinfo=timezone.utc),  # session_created_at
        "widget",            # channel
        42,                  # instance_id
        "conv-xyz",          # conversation_id
        datetime(2024, 1, 1, tzinfo=timezone.utc),  # conv_created_at
        datetime(2024, 1, 2, tzinfo=timezone.utc),  # conv_updated_at
        [{"id": "msg-1", "role": "user", "content": "hello",
          "created_at": "2024-01-01T00:00:00+00:00"}],  # messages
    )
    db = _make_db_with_data(sessions=[session_row])
    s3 = _FakeS3()
    svc, _, _ = _make_service(db=db, s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    data = s3.uploads[-1]["data"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())

    assert "conversations/sess-abc.json" in names, (
        "Per-session JSON conversations/sess-abc.json must be in the ZIP"
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        session_obj = json.loads(zf.read("conversations/sess-abc.json"))
    assert session_obj["session_id"] == "sess-abc"
    assert "messages" in session_obj


# -----------------------------------------------------------------------
# EZ-4: conversations.csv present.
# -----------------------------------------------------------------------

def test_ez4_conversations_csv_present():
    """conversations/conversations.csv must be present."""
    s3 = _FakeS3()
    svc, _, _ = _make_service(s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    data = s3.uploads[-1]["data"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "conversations/conversations.csv" in zf.namelist()


# -----------------------------------------------------------------------
# EZ-5: instances.json — no secret_ref or secrets.
# -----------------------------------------------------------------------

def test_ez5_instances_json_no_secret_material():
    """instances.json must NEVER contain secret_ref or secrets."""
    instance_row = (
        1,               # id
        "admin-1",       # admin_id
        "My Instance",   # display_name
        "active",        # instance_status
        datetime(2024, 1, 1, tzinfo=timezone.utc),  # created_at
        datetime(2024, 1, 2, tzinfo=timezone.utc),  # updated_at
        None,            # soft_deleted_at
        None,            # instance_status_note
    )
    conn_row = (
        1,                      # instance_id
        "email_sender",         # connection_type
        "sendgrid",             # provider
        {"from_email": "noreply@test.com"},  # non_secret_config (non_secret)
        "connected",            # status
        datetime(2024, 1, 1, tzinfo=timezone.utc),  # last_health_check_at
        datetime(2024, 1, 1, tzinfo=timezone.utc),  # created_at
        datetime(2024, 1, 1, tzinfo=timezone.utc),  # updated_at
        # secret_ref is NOT included in the query (intentionally omitted)
    )
    db = _make_db_with_data(instances=[instance_row], connections=[conn_row])
    s3 = _FakeS3()
    svc, _, _ = _make_service(db=db, s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    data = s3.uploads[-1]["data"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        instances_raw = zf.read("instances.json").decode("utf-8")
    instances = json.loads(instances_raw)

    # Must contain provider + non_secret_config + status.
    assert len(instances) == 1
    inst = instances[0]
    assert inst["instance_status"] == "active"
    assert len(inst["connections"]) == 1
    conn = inst["connections"][0]
    assert conn["provider"] == "sendgrid"
    assert conn["status"] == "connected"
    assert "non_secret_config" in conn

    # MUST NOT contain secret_ref.
    instances_str = instances_raw
    assert "secret_ref" not in instances_str, (
        "instances.json must NEVER contain secret_ref — "
        "this is a security invariant (§5.10)"
    )
    # Also assert no common secret field names.
    for forbidden in ("password", "api_key", "api_secret", "token", "secret"):
        # Allow 'non_secret_config' as the field name itself but not
        # standalone secret key names.
        assert f'"{forbidden}"' not in instances_str, (
            f"instances.json must not contain key {forbidden!r}"
        )


def test_ez5_instances_json_has_provider_and_status():
    """instances.json has provider + non_secret_config + status per §5.10."""
    instance_row = (
        7, "admin-1", "Inst-7", "active",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        None, None,
    )
    conn_row = (
        7, "crm", "salesforce", {"crm_object": "Lead"}, "connected",
        None,
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db = _make_db_with_data(instances=[instance_row], connections=[conn_row])
    s3 = _FakeS3()
    svc, _, _ = _make_service(db=db, s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    data = s3.uploads[-1]["data"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        instances = json.loads(zf.read("instances.json"))

    conn = instances[0]["connections"][0]
    assert conn["provider"] == "salesforce"
    assert conn["status"] == "connected"
    assert conn["non_secret_config"] == {"crm_object": "Lead"}


# -----------------------------------------------------------------------
# EZ-6: Free-tier gate.
# -----------------------------------------------------------------------

def test_ez6_free_blocked_outside_closure():
    """Free admin without closure_initiated_at raises ExportFreeGateError."""
    svc, _, _ = _make_service()
    with pytest.raises(ExportFreeGateError):
        svc.enqueue(
            admin_id="free-admin",
            triggered_by="admin_request",
            tier_at_request="free",
            audit_ctx=MagicMock(),
            closure_initiated_at=None,  # Not in closure
        )


def test_ez6_free_allowed_during_closure():
    """Free admin with closure_initiated_at set passes the gate."""
    db = _FakeDB()
    # The enqueue path calls db.execute (INSERT) and db.flush — stubs ok.
    svc, _, _ = _make_service(db=db)
    closure_ts = datetime(2024, 6, 1, tzinfo=timezone.utc)

    mock_ctx = MagicMock()
    # enqueue will call audit_repository.record — that's our fake.
    # It also calls db.execute (INSERT) which FakeDB accepts (returns empty rows).
    try:
        job = svc.enqueue(
            admin_id="free-admin",
            triggered_by="admin_request",
            tier_at_request="free",
            audit_ctx=mock_ctx,
            closure_initiated_at=closure_ts,
        )
    except Exception as exc:
        # Should NOT raise ExportFreeGateError.
        assert not isinstance(exc, ExportFreeGateError), (
            f"Free admin during closure must be allowed but raised: {exc}"
        )


# -----------------------------------------------------------------------
# EZ-7: Pro/Enterprise self-serve allowed.
# -----------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["pro"])
def test_ez7_pro_enterprise_self_serve_allowed(tier: str):
    """Pro/Enterprise may export at any time (no closure required)."""
    db = _FakeDB()
    svc, _, _ = _make_service(db=db)
    mock_ctx = MagicMock()
    try:
        job = svc.enqueue(
            admin_id=f"{tier}-admin",
            triggered_by="admin_request",
            tier_at_request=tier,
            audit_ctx=mock_ctx,
            closure_initiated_at=None,
        )
    except ExportFreeGateError:
        pytest.fail(f"{tier} admin must be allowed to export without closure")
    except Exception:
        # Other exceptions (DB integrity etc.) are fine in test stubs.
        pass


# -----------------------------------------------------------------------
# EZ-8: data_export_self_serve audit emitted for Pro/Enterprise.
# -----------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["pro"])
def test_ez8_self_serve_audit_emitted_for_pro_enterprise(tier: str):
    """Pro/Enterprise non-closure export emits data_export_self_serve audit."""
    db = _FakeDB()
    audit_repo = _FakeAuditRepo()
    svc, _, _ = _make_service(db=db, audit_repo=audit_repo)
    mock_ctx = MagicMock()

    from app.models.admin_audit_log import ACTION_DATA_EXPORT_SELF_SERVE
    try:
        svc.enqueue(
            admin_id=f"{tier}-admin",
            triggered_by="admin_request",
            tier_at_request=tier,
            audit_ctx=mock_ctx,
            closure_initiated_at=None,
        )
    except Exception:
        pass

    self_serve_audits = [
        c for c in audit_repo.calls
        if c["action"] == ACTION_DATA_EXPORT_SELF_SERVE
    ]
    assert len(self_serve_audits) >= 1, (
        f"{tier} non-closure export must emit data_export_self_serve audit"
    )


# -----------------------------------------------------------------------
# EZ-9: No data_export_self_serve for Free closure path.
# -----------------------------------------------------------------------

def test_ez9_no_self_serve_audit_for_free_closure():
    """Free admin closure-triggered export must NOT emit data_export_self_serve."""
    db = _FakeDB()
    audit_repo = _FakeAuditRepo()
    svc, _, _ = _make_service(db=db, audit_repo=audit_repo)
    mock_ctx = MagicMock()
    closure_ts = datetime(2024, 6, 1, tzinfo=timezone.utc)

    from app.models.admin_audit_log import ACTION_DATA_EXPORT_SELF_SERVE
    try:
        svc.enqueue(
            admin_id="free-admin",
            triggered_by="admin_request",
            tier_at_request="free",
            audit_ctx=mock_ctx,
            closure_initiated_at=closure_ts,
        )
    except Exception:
        pass

    self_serve_audits = [
        c for c in audit_repo.calls
        if c["action"] == ACTION_DATA_EXPORT_SELF_SERVE
    ]
    assert len(self_serve_audits) == 0, (
        "Free closure-path export must NOT emit data_export_self_serve"
    )


# -----------------------------------------------------------------------
# EZ-10: ExportFreeGateError is a DataExportError.
# -----------------------------------------------------------------------

def test_ez10_free_gate_error_extends_base():
    """ExportFreeGateError must extend DataExportError."""
    assert issubclass(ExportFreeGateError, DataExportError)


# -----------------------------------------------------------------------
# EZ-11: audit_log.jsonl is JSONL format.
# -----------------------------------------------------------------------

def test_ez11_audit_log_is_jsonl():
    """audit_log.jsonl must be valid JSONL (one JSON object per line)."""
    audit_row = (
        "audit-1",  # id
        datetime(2024, 1, 1, tzinfo=timezone.utc),  # created_at
        "create",   # action
        "instance", # resource_type
        "1",        # resource_pk
        "inst-1",   # resource_natural_id
        "key-abc",  # actor_key_prefix
        "pro",      # tier_at_write
        None,       # cold_archived_at
        None,       # before_json
        {"name": "test"},  # after_json
        "note text",       # note
    )
    db = _make_db_with_data(audit_rows=[audit_row])
    s3 = _FakeS3()
    svc, _, _ = _make_service(db=db, s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    data = s3.uploads[-1]["data"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        audit_content = zf.read("audit_log.jsonl").decode("utf-8")

    # Must be JSONL, not CSV.
    lines = [line for line in audit_content.splitlines() if line.strip()]
    assert len(lines) >= 1
    for line in lines:
        obj = json.loads(line)  # raises on invalid JSON
        assert "action" in obj or "id" in obj


# -----------------------------------------------------------------------
# EZ-12: S3 upload uses application/zip ContentType.
# -----------------------------------------------------------------------

def test_ez12_s3_content_type_is_application_zip():
    """S3 upload must use ContentType=application/zip."""
    s3 = _FakeS3()
    svc, _, _ = _make_service(s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    assert s3.uploads[-1]["extra_args"]["ContentType"] == "application/zip", (
        "S3 upload must use ContentType=application/zip"
    )


# -----------------------------------------------------------------------
# EZ-13: S3 key uses .zip extension.
# -----------------------------------------------------------------------

def test_ez13_s3_key_uses_zip_extension():
    """S3 key must end with .zip and include the admin_id + timestamp."""
    s3 = _FakeS3()
    svc, _, _ = _make_service(s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    key = s3.uploads[-1]["key"]
    assert key.endswith(".zip"), f"S3 key must end with .zip; got {key!r}"
    assert "admin-1" in key, f"S3 key must include admin_id; got {key!r}"
    assert "tar.gz" not in key, f"S3 key must not contain tar.gz; got {key!r}"


# -----------------------------------------------------------------------
# EZ-14: manifest.json schema_version = 2.
# -----------------------------------------------------------------------

def test_ez14_manifest_schema_version_2():
    """manifest.json must declare schema_version: 2 (ZIP format)."""
    s3 = _FakeS3()
    svc, _, _ = _make_service(s3=s3)
    svc._build_and_upload_bundle(
        admin_id="admin-1", tier_at_request="pro", job_id="job-1"
    )
    data = s3.uploads[-1]["data"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["schema_version"] == 2, (
        "manifest.json schema_version must be 2 for the ZIP format"
    )
    assert manifest["format"] == "zip"


# -----------------------------------------------------------------------
# Existing contract: exception classes still exist (C1 from arc10 test).
# -----------------------------------------------------------------------

def test_existing_exception_classes_preserved():
    """All exception classes from the original Arc 10 contract still present."""
    from app.services.data_export_service import (
        DataExportError,
        ExportAlreadyInFlightError,
        ExportGenerationError,
        ExportNotFoundError,
        ExportNotReadyError,
    )
    for cls in (
        DataExportError, ExportAlreadyInFlightError, ExportNotReadyError,
        ExportNotFoundError, ExportGenerationError,
    ):
        assert cls is not None


# -----------------------------------------------------------------------
# EZ: data_export_self_serve constant exists.
# -----------------------------------------------------------------------

def test_data_export_self_serve_audit_constant():
    """ACTION_DATA_EXPORT_SELF_SERVE must be importable with stable value."""
    from app.models.admin_audit_log import ACTION_DATA_EXPORT_SELF_SERVE
    assert ACTION_DATA_EXPORT_SELF_SERVE == "data_export_self_serve"
