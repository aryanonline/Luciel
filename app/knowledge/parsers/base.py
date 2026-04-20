"""
Parser abstract base class + shared types (Step 25b, File 5).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ParserError(Exception):
    """Raised when a parser cannot extract text from the given bytes."""


class UnsupportedSourceType(ParserError):
    """Raised when no parser is registered for a given source_type / suffix."""


@dataclass(frozen=True)
class ParsedDocument:
    """Normalized output from any Parser.

    - text: extracted, UTF-8 decodable string ready to be chunked.
    - metadata: parser-specific structured bits (page count, sheet names,
      JSON key paths, etc.) recorded for audit / future re-chunking.
      Never used for authorization — that lives on the scope triple.
    """
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Parser(ABC):
    """One Parser per supported source type.

    Stateless: subclasses should not hold per-document state. A single
    module-level instance (see parsers/__init__._REGISTRY) serves all
    ingests concurrently.
    """

    #: Canonical slug stored in knowledge_embeddings.source_type.
    source_type: str = ""

    @abstractmethod
    def parse(self, file_bytes: bytes, filename: str | None = None) -> ParsedDocument:
        """Turn raw bytes into a ParsedDocument.

        Must raise ParserError (or a subclass) on any failure — never
        return empty text silently, never raise a format-library-specific
        exception that leaks outside the parsers package.
        """
        ...