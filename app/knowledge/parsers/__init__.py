"""
Knowledge parsers subpackage (Step 25b, File 5).

One Parser subclass per supported source type. Each parser turns raw
file bytes into a normalized (text, metadata) tuple ready for chunking.

Dispatch is via the registry in this module — `get_parser(source_type)`
and `detect_source_type(filename)` are the only two entry points the
ingestion service (File 9) needs to know about.

Domain-agnostic by construction: the same parsers serve real-estate
listing PDFs, legal contracts in DOCX, and engineering README files
in Markdown with zero vertical-specific branching. Any per-vertical
behaviour lives in chunker strategy config (tenant / domain / instance
level, File 6) and knowledge retrieval filters (File 11) — never in
the parsers themselves.
"""
from __future__ import annotations

from app.knowledge.parsers.base import (
    Parser,
    ParsedDocument,
    ParserError,
    UnsupportedSourceType,
)
from app.knowledge.parsers.csv_parser import CsvParser
from app.knowledge.parsers.docx_parser import DocxParser
from app.knowledge.parsers.html_parser import HtmlParser
from app.knowledge.parsers.json_parser import JsonParser
from app.knowledge.parsers.markdown_parser import MarkdownParser
from app.knowledge.parsers.pdf_parser import PdfParser
from app.knowledge.parsers.text_parser import TextParser

# Canonical source-type slugs. Keep short, lowercase, ASCII — these go
# into knowledge_embeddings.source_type (String(20)) verbatim.
SUPPORTED_SOURCE_TYPES: tuple[str, ...] = (
    "txt",
    "md",
    "html",
    "pdf",
    "docx",
    "csv",
    "json",
)

# Registry: source_type -> Parser instance.
# Parsers are stateless, so module-level singletons are safe and cheap.
_REGISTRY: dict[str, Parser] = {
    "txt": TextParser(),
    "md": MarkdownParser(),
    "html": HtmlParser(),
    "pdf": PdfParser(),
    "docx": DocxParser(),
    "csv": CsvParser(),
    "json": JsonParser(),
}

# Filename-suffix -> source_type. Used by detect_source_type().
# Multiple suffixes can map to the same type (e.g., .htm and .html).
_SUFFIX_TO_TYPE: dict[str, str] = {
    ".txt": "txt",
    ".md": "md",
    ".markdown": "md",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
    ".docx": "docx",
    ".csv": "csv",
    ".json": "json",
}


def get_parser(source_type: str) -> Parser:
    """Return the Parser for a canonical source_type slug.

    Raises UnsupportedSourceType if the slug isn't in the registry.
    """
    parser = _REGISTRY.get(source_type)
    if parser is None:
        raise UnsupportedSourceType(
            f"Unsupported source_type: {source_type!r}. "
            f"Supported: {sorted(_REGISTRY)}"
        )
    return parser


def detect_source_type(filename: str) -> str:
    """Detect canonical source_type from a filename's suffix.

    Raises UnsupportedSourceType if the suffix is unknown or missing.
    """
    if not filename:
        raise UnsupportedSourceType("Cannot detect source_type: empty filename")
    # Lowercase the suffix only — case-insensitive matching.
    dot = filename.rfind(".")
    if dot < 0:
        raise UnsupportedSourceType(
            f"Cannot detect source_type: filename {filename!r} has no extension"
        )
    suffix = filename[dot:].lower()
    source_type = _SUFFIX_TO_TYPE.get(suffix)
    if source_type is None:
        raise UnsupportedSourceType(
            f"Unsupported file extension: {suffix!r}. "
            f"Supported: {sorted(set(_SUFFIX_TO_TYPE.values()))}"
        )
    return source_type


__all__ = [
    "Parser",
    "ParsedDocument",
    "ParserError",
    "UnsupportedSourceType",
    "SUPPORTED_SOURCE_TYPES",
    "get_parser",
    "detect_source_type",
]