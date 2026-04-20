"""Plain-text parser (Step 25b, File 5)."""
from __future__ import annotations

from app.knowledge.parsers.base import ParsedDocument, Parser, ParserError


class TextParser(Parser):
    source_type = "txt"

    def parse(self, file_bytes: bytes, filename: str | None = None) -> ParsedDocument:
        # Try UTF-8 first, fall back to latin-1 (never fails) with a
        # metadata note so the ingest audit captures the fallback.
        try:
            text = file_bytes.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1")
            encoding = "latin-1"
        if not text.strip():
            raise ParserError("Text file is empty or whitespace-only")
        return ParsedDocument(
            text=text,
            metadata={"encoding": encoding, "bytes": len(file_bytes)},
        )