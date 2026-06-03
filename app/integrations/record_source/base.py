"""RecordSource ABC + the domain-agnostic query helper — Arc 17.

A ``RecordSource`` resolves a connection's non-secret ``store_ref`` (a
storage location) to a list of dict rows parsed from a CSV — the CSV
header row supplies the dict keys. ``lookup_record`` reads the source
LIVE on every call and returns the matched rows as live records; the
source is NOT the knowledge store and is never blended with vector /
graph retrieval (Architecture §3.2 correctness boundary).

Domain-agnostic (Locked Decision #5): the query vocabulary is generic
``record_id`` / ``query`` / ``filters`` only. There is NO vertical-
specific column knowledge anywhere in this module — the only column
names it reasons about are the structural ``id`` / ``record_id``
identity columns common to any tabular record set.
"""
from __future__ import annotations

import csv
import io
from abc import ABC, abstractmethod
from typing import Optional


class RecordSourceError(RuntimeError):
    """Raised when a record source cannot be read.

    The caller (``lookup_record``) translates this into an HONEST
    failure result (``success=False``) — never a fake success and never
    a crash.
    """


# Default cap on returned rows. A lookup that matches more than this is
# truncated and the truncation is surfaced in the tool output so the
# caller knows the result set was bounded rather than complete.
DEFAULT_RESULT_CAP = 50

# Column names treated as the row identity for a ``record_id`` lookup,
# matched case-insensitively. Generic / structural — NOT vertical.
_ID_COLUMNS = ("id", "record_id")


def parse_csv_bytes(data: bytes | str) -> list[dict]:
    """Parse CSV ``data`` into a list of dict rows (header row = keys).

    Accepts ``bytes`` (decoded as UTF-8, BOM-tolerant) or ``str``. An
    empty / header-only document yields ``[]``.
    """
    if isinstance(data, bytes):
        text = data.decode("utf-8-sig")
    else:
        text = data
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _norm(value: object) -> str:
    """Normalise a cell / criterion to a trimmed, case-folded string."""
    return str(value).strip().casefold()


def query_rows(
    rows: list[dict],
    *,
    record_id: Optional[str] = None,
    query: Optional[str] = None,
    filters: Optional[dict] = None,
    cap: int = DEFAULT_RESULT_CAP,
) -> tuple[list[dict], bool]:
    """Deterministic, domain-agnostic row matcher.

    Criteria are combined with AND. Returns ``(matched_rows, truncated)``
    where ``truncated`` is True when the match set exceeded ``cap`` and
    was clipped.

    Semantics:
      * ``record_id`` — match rows whose id-like column (``id`` /
        ``record_id``, case-insensitive) equals ``record_id`` (trimmed,
        case-insensitive). If the row set has no id-like column, match
        against the FIRST column instead.
      * ``filters`` — a ``{column: value}`` map; a row matches only when
        EVERY named column equals the given value (trimmed, case-
        insensitive). An unknown column matches nothing → ``[]`` (not an
        error).
      * ``query`` — free-text substring match (case-insensitive) across
        ALL cell values in a row.
      * Empty input (none of the three) → a bounded sample of the first
        ``cap`` rows, flagged truncated when the source has more.
    """
    id_col = _resolve_id_column(rows)

    matched: list[dict] = []
    for row in rows:
        if record_id is not None and not _match_record_id(
            row, id_col, record_id
        ):
            continue
        if filters and not _match_filters(row, filters):
            continue
        if query is not None and not _match_query(row, query):
            continue
        matched.append(row)

    no_criteria = record_id is None and query is None and not filters
    if no_criteria:
        truncated = len(rows) > cap
        return rows[:cap], truncated

    truncated = len(matched) > cap
    return matched[:cap], truncated


def _resolve_id_column(rows: list[dict]) -> Optional[str]:
    """Return the id-like column name for ``rows`` (case-insensitive),
    falling back to the first column, or ``None`` for an empty set."""
    if not rows:
        return None
    columns = list(rows[0].keys())
    lowered = {c.casefold(): c for c in columns}
    for candidate in _ID_COLUMNS:
        if candidate in lowered:
            return lowered[candidate]
    return columns[0] if columns else None


def _match_record_id(row: dict, id_col: Optional[str], record_id: str) -> bool:
    if id_col is None:
        return False
    return _norm(row.get(id_col)) == _norm(record_id)


def _match_filters(row: dict, filters: dict) -> bool:
    lowered = {str(k).casefold(): v for k, v in row.items()}
    for col, want in filters.items():
        key = str(col).casefold()
        if key not in lowered:
            return False
        if _norm(lowered[key]) != _norm(want):
            return False
    return True


def _match_query(row: dict, query: str) -> bool:
    needle = _norm(query)
    if not needle:
        return True
    return any(needle in _norm(v) for v in row.values())


class RecordSource(ABC):
    """Abstract live record source. ``fetch_rows`` reads the backing
    store and returns dict rows; ``query`` applies the shared matcher."""

    @abstractmethod
    def fetch_rows(self) -> list[dict]:
        """Read the backing store and return its rows as dicts. Raises
        ``RecordSourceError`` when the store is unreachable / malformed."""

    def query(
        self,
        *,
        record_id: Optional[str] = None,
        query: Optional[str] = None,
        filters: Optional[dict] = None,
        cap: int = DEFAULT_RESULT_CAP,
    ) -> tuple[list[dict], bool]:
        """Fetch then match. Returns ``(rows, truncated)``."""
        rows = self.fetch_rows()
        return query_rows(
            rows,
            record_id=record_id,
            query=query,
            filters=filters,
            cap=cap,
        )
