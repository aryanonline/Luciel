"""PDF parser (Step 25b, File 5).

Uses pypdf for pure-Python text extraction. For table-aware parsing
(e.g., structured contracts, financial statements) a future step can
swap in pdfplumber behind the same Parser interface without changing
anything downstream.
"""
from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.knowledge.parsers.base import ParsedDocument, Parser, ParserError


class PdfParser(Parser):
    source_type = "pdf"

    def parse(self, file_bytes: bytes, filename: str | None = None) -> ParsedDocument:
        try:
            reader = PdfReader(BytesIO(file_bytes))
        except PdfReadError as exc:
            raise ParserError(f"Could not read PDF: {exc}") from exc

        if reader.is_encrypted:
            # Try empty password — many "encrypted" PDFs are only flagged as such.
            try:
                reader.decrypt("")
            except Exception as exc:  # pragma: no cover
                raise ParserError(f"PDF is encrypted: {exc}") from exc

        pages: list[str] = []
        for i, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 — pypdf raises heterogeneous exc types
                raise ParserError(f"PDF page {i} extraction failed: {exc}") from exc
            if page_text.strip():
                pages.append(page_text.strip())

        if not pages:
            raise ParserError("PDF produced no extractable text (possibly a scanned image)")

        # Double-newline between pages so the paragraph chunker treats page
        # boundaries as chunk boundaries — consistent with how it handles
        # paragraph breaks inside a page.
        text = "\n\n".join(pages)
        return ParsedDocument(
            text=text,
            metadata={"page_count": len(reader.pages), "pages_with_text": len(pages)},
        )