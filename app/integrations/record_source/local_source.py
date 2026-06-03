"""LocalFileRecordSource — local-path / in-memory CSV source (Arc 17).

Reads a CSV from a local filesystem path (``file://`` URI or a bare
path) or from CSV bytes/text supplied directly. No network, no AWS —
this is the source the test suite and local dev exercise end to end.

The S3 path lives in ``s3_source.py`` (DEPLOY-GATED) and is never
reached from here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.integrations.record_source.base import (
    RecordSource,
    RecordSourceError,
    parse_csv_bytes,
)


class LocalFileRecordSource(RecordSource):
    """A ``RecordSource`` backed by a local CSV file or in-memory bytes.

    Construct with EITHER ``path`` (a filesystem path or ``file://``
    URI) OR ``data`` (raw CSV bytes/str). ``data`` wins when both are
    given — the in-memory form is the test convenience.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        data: Optional[bytes | str] = None,
    ) -> None:
        if path is None and data is None:
            raise RecordSourceError(
                "LocalFileRecordSource requires a path or in-memory data."
            )
        self._path = _strip_file_scheme(path) if path is not None else None
        self._data = data

    def fetch_rows(self) -> list[dict]:
        if self._data is not None:
            return parse_csv_bytes(self._data)
        path = Path(self._path)  # type: ignore[arg-type]
        if not path.is_file():
            raise RecordSourceError(
                f"record source file not found: {self._path!r}"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RecordSourceError(
                f"could not read record source file {self._path!r}: {exc}"
            ) from exc
        return parse_csv_bytes(raw)


def _strip_file_scheme(path: str) -> str:
    if path.startswith("file://"):
        return path[len("file://"):]
    return path
