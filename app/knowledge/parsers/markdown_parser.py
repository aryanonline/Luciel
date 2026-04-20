"""Markdown parser (Step 25b, File 5).

Renders Markdown -> HTML -> plain text via bs4, so chunk boundaries are
paragraph-aware for free. Preserves code fences by leaving their
rendered de>/<pre> text in place — the chunker sees them as normal
paragraphs.
"""
from __future__ import annotations

import markdown as md_lib
from bs4 import BeautifulSoup

from app.knowledge.parsers.base import ParsedDocument, Parser, ParserError


class MarkdownParser(Parser):
    source_type = "md"

    def parse(self, file_bytes: bytes, filename: str | None = None) -> ParsedDocument:
        try:
            raw = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw = file_bytes.decode("latin-1")
        if not raw.strip():
            raise ParserError("Markdown file is empty")
        html = md_lib.markdown(
            raw,
            extensions=["fenced_code", "tables"],
        )
        # Keep block structure as double-newlines so paragraph chunking works.
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n\n").strip()
        if not text:
            raise ParserError("Markdown rendered to empty text")
        return ParsedDocument(
            text=text,
            metadata={"raw_length": len(raw), "html_length": len(html)},
        )