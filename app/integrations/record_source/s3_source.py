"""S3RecordSource — the REAL boto3 record source.

DEPLOY-GATED: ``fetch_rows`` makes a live AWS S3 ``get_object`` call and
therefore requires AWS credentials + an IAM ``s3:GetObject`` grant on
the record-source bucket prefix. It is NEVER selected by
``resolve_record_source`` unless ``record_source_live_enabled`` is True,
and it is NEVER exercised by the test suite (tests use
``LocalFileRecordSource``). The boto3 client is constructed lazily on
first use so merely importing this module costs nothing and needs no
creds.

The ``store_ref`` is an ``s3://bucket/key`` URI. The bucket/key carry
NO secret — they are a non-secret storage location held in the
connection's ``config_json.store_ref`` (Architecture §3.2 / §3.8.2). Any
credential needed to read the object (if ever) rides behind
``credential_ref`` via the SecretStore — never config_json. In this
build the object is a plain CSV needing only the task role's S3 grant.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.integrations.record_source.base import (
    RecordSource,
    RecordSourceError,
    parse_csv_bytes,
)


class S3RecordSource(RecordSource):
    """A ``RecordSource`` backed by an ``s3://`` object. DEPLOY-GATED —
    see module docstring."""

    def __init__(self, s3_uri: str, *, region_name: str | None = None) -> None:
        self._s3_uri = s3_uri
        self._region_name = region_name
        self._bucket, self._key = _parse_s3_uri(s3_uri)
        self._client: Any = None

    def _get_client(self) -> Any:
        # DEPLOY-GATED: constructs a live AWS client. Lazy so import +
        # construction never touch the network; the first real call does.
        if self._client is None:  # pragma: no cover - DEPLOY-GATED
            import boto3  # local import keeps boto3 off the import path

            self._client = boto3.client("s3", region_name=self._region_name)
        return self._client

    def fetch_rows(self) -> list[dict]:
        # DEPLOY-GATED: live GetObject call. Never invoked in tests.
        from botocore.exceptions import (  # pragma: no cover - DEPLOY-GATED
            BotoCoreError,
            ClientError,
        )

        client = self._get_client()  # pragma: no cover - DEPLOY-GATED
        try:  # pragma: no cover - DEPLOY-GATED
            resp = client.get_object(Bucket=self._bucket, Key=self._key)
            body = resp["Body"].read()
        except (ClientError, BotoCoreError) as exc:  # pragma: no cover
            raise RecordSourceError(
                f"could not read S3 record source {self._s3_uri!r}: {exc}"
            ) from exc
        return parse_csv_bytes(body)  # pragma: no cover - DEPLOY-GATED


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise RecordSourceError(
            f"malformed s3 store_ref (expected s3://bucket/key): {s3_uri!r}"
        )
    return parsed.netloc, parsed.path.lstrip("/")
