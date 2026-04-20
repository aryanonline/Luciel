"""CSV parser (Step 25b, File 5).

Each row is serialized as 'header1: value1 | header2: value2 | ...' so
semantic chunking works naturally: one row per line, multiple rows per
paragraph-sized chunk. Keeps column context with each value — critical
for retrieval precision on tabular data.
"""
from __future__ import annotations

import csv
from io import StringIO

from app.knowledge.parsers.base import ParsedDocument, Parser, ParserError


class CsvParser(Parser):
    source_type = "csv"

    def parse(self, file_bytes: bytes, filename: str | None = None) -> ParsedDocument:
        try:
            raw = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw = file_bytes.decode("latin-1")
        if not raw.strip():
            raise ParserError("CSV file is empty")

        reader = csv.reader(StringIO(raw))
        try:
            header = next(reader)
        except StopIteration:
            raise ParserError("CSV has no rows") from None

        header = [h.strip() for h in header]
        lines: list[str] = []
        row_count = 0
        for row in reader:
            row_count += 1
            pairs = [
                f"{h}: {v.strip()}"
                for h, v in zip(header, row)
                if v and v.strip()
            ]
            if pairs:
                lines.append(" | ".join(pairs))

        if not lines:
            raise ParserError("CSV produced no non-empty rows after header")

        text = "\n".join(lines)
        return ParsedDocument(
            text=text,
            metadata={
                "header": header,
                "column_count": len(header),
                "row_count": row_count,
            },
        )