"""JSON parser (Step 25b, File 5).

Flattens JSON into 'dotted.path: value' lines, one per leaf. Arrays
become 'parent[i].child: value'. Makes vector retrieval work over
deeply-nested config / API-response documents without losing path
context.
"""
from __future__ import annotations

import json as json_lib
from typing import Any

from app.knowledge.parsers.base import ParsedDocument, Parser, ParserError


class JsonParser(Parser):
    source_type = "json"

    def parse(self, file_bytes: bytes, filename: str | None = None) -> ParsedDocument:
        try:
            raw = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw = file_bytes.decode("latin-1")
        if not raw.strip():
            raise ParserError("JSON file is empty")
        try:
            data = json_lib.loads(raw)
        except json_lib.JSONDecodeError as exc:
            raise ParserError(f"Invalid JSON: {exc}") from exc

        lines: list[str] = []
        self._flatten(data, "", lines)
        if not lines:
            raise ParserError("JSON produced no extractable leaves")

        text = "\n".join(lines)
        return ParsedDocument(
            text=text,
            metadata={"leaf_count": len(lines), "bytes": len(file_bytes)},
        )

    def _flatten(self, node: Any, path: str, out: list[str]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                child_path = f"{path}.{k}" if path else str(k)
                self._flatten(v, child_path, out)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                child_path = f"{path}[{i}]"
                self._flatten(v, child_path, out)
        else:
            # Scalar leaf (str / int / float / bool / None).
            value = "" if node is None else str(node)
            out.append(f"{path}: {value}" if path else value)