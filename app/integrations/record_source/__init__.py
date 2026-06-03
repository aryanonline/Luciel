"""Record-source integration package — Arc 17.

``lookup_record`` reads LIVE, EXACT records from its configured
``record_source`` connection on every call (Architecture §3.2
correctness boundary) — it is NOT the knowledge store and is never
blended with vector / graph retrieval. The connection's non-secret
``config_json.store_ref`` names WHERE the records live; this package
resolves that location to a ``RecordSource`` and runs a deterministic,
domain-agnostic query over the rows.

Pieces:
  * ``RecordSource``           — the ABC + the shared ``query_rows`` matcher.
  * ``LocalFileRecordSource``  — local-path / in-memory CSV. Used in tests.
  * ``S3RecordSource``         — real boto3 ``get_object``. DEPLOY-GATED:
                                 needs AWS creds + s3:GetObject; never
                                 exercised by the suite.
  * ``resolve_record_source``  — scheme dispatch (s3:// → S3, file://|path
                                 → local) + the s3 deploy gate.

The s3 deploy gate mirrors ``connections_live_secrets_enabled``: with
``record_source_live_enabled`` False (the boot-safe default) an s3://
store_ref raises ``RecordSourceUnavailableError`` and NO boto3 client is
ever constructed — the tool turns that into an HONEST deploy-gated
failure, never a fake success.
"""
from __future__ import annotations

from app.integrations.record_source.base import (
    DEFAULT_RESULT_CAP,
    RecordSource,
    RecordSourceError,
    parse_csv_bytes,
    query_rows,
)
from app.integrations.record_source.local_source import LocalFileRecordSource
from app.integrations.record_source.resolver import (
    RecordSourceUnavailableError,
    resolve_record_source,
)
from app.integrations.record_source.s3_source import S3RecordSource

__all__ = [
    "DEFAULT_RESULT_CAP",
    "RecordSource",
    "RecordSourceError",
    "RecordSourceUnavailableError",
    "LocalFileRecordSource",
    "S3RecordSource",
    "parse_csv_bytes",
    "query_rows",
    "resolve_record_source",
]
