"""HTML parser (Step 25b, File 5)."""
from __future__ import annotations

from bs4 import BeautifulSoup

from app.knowledge.parsers.base import ParsedDocument, Parser, ParserError


class HtmlParser(Parser):
    source_type = "html"

    # Elements whose text content we deliberately discard — scripts, styles,
    # and boilerplate nav/chrome. Domain-agnostic: applies to every tenant.
    _STRIP_TAGS: tuple[str, ...] = ("script", "style", "noscript", "iframe")

    def parse(self, file_bytes: bytes, filename: str | None = None) -> ParsedDocument:
        try:
            raw = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw = file_bytes.decode("latin-1")
        soup = BeautifulSoup(raw, "html.parser")
        for tag_name in self._STRIP_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        text = soup.get_text(separator="\n\n").strip()
        if not text:
            raise ParserError("HTML document rendered to empty text")
        title_tag = soup.find("title")
        return ParsedDocument(
            text=text,
            metadata={
                "title": title_tag.get_text(strip=True) if title_tag else None,
                "bytes": len(file_bytes),
            },
        )