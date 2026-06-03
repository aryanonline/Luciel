"""resolve_record_source — scheme dispatch + the s3 deploy-gate.

Picks a ``RecordSource`` impl from a connection's non-secret
``store_ref``:

  * ``s3://...``           → ``S3RecordSource`` (DEPLOY-GATED). Selected
                             ONLY when ``settings.record_source_live_enabled``
                             is True. When the flag is False the resolver
                             raises ``RecordSourceUnavailableError`` — an
                             HONEST "not reachable in this environment"
                             signal the tool turns into a clean
                             ``success=False`` result. NO boto3 client is
                             constructed and NO AWS call is made.
  * ``file://...`` / path  → ``LocalFileRecordSource``.

The resolver NEVER fabricates a success and NEVER blends data sources —
it only chooses where to read the live records from.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.integrations.record_source.base import RecordSource
from app.integrations.record_source.local_source import LocalFileRecordSource

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings


class RecordSourceUnavailableError(RuntimeError):
    """The store_ref names a source not reachable in this environment.

    Raised for an ``s3://`` store_ref while ``record_source_live_enabled``
    is False (the deploy gate). The tool catches this and returns an
    HONEST deploy-gated failure — never a fake success, never a crash.
    """


def resolve_record_source(
    store_ref: str, settings: "Settings"
) -> RecordSource:
    """Return the ``RecordSource`` impl for ``store_ref``.

    Raises ``RecordSourceUnavailableError`` when the scheme is ``s3://``
    and the live-read flag is off (the deploy gate), or when the scheme
    is unrecognised.
    """
    if not store_ref or not str(store_ref).strip():
        raise RecordSourceUnavailableError(
            "record source has no store_ref configured."
        )

    scheme = urlparse(store_ref).scheme.lower()

    if scheme == "s3":
        if not settings.record_source_live_enabled:
            # DEPLOY GATE: live S3 read is withheld in this environment.
            # Honest, not fake — the S3 impl is NOT constructed.
            raise RecordSourceUnavailableError(
                "record source is configured on remote object storage "
                "(s3://) which is not reachable in this environment. "
                "Live S3 record reads are deploy-gated pending the AWS "
                "s3:GetObject grant."
            )
        # DEPLOY-GATED branch: real boto3 S3 source.
        from app.integrations.record_source.s3_source import S3RecordSource

        return S3RecordSource(store_ref, region_name=settings.aws_region)

    if scheme in ("file", ""):
        # ``file://`` URI or a bare local path (no scheme).
        return LocalFileRecordSource(store_ref)

    raise RecordSourceUnavailableError(
        f"unsupported record source store_ref scheme: {scheme!r}."
    )
