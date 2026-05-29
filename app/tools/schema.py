"""
JSON-Schema validation helpers for the tool subsystem.

Arc 12 WU1 introduces an input/output JSON-Schema contract on every
``LucielTool``. The broker validates input before ``execute()`` and
the output after. The WU6 BYO sandbox uses the same helpers so a
custom webhook gets the identical validation discipline as an
in-tree tool.

Dependency choice
-----------------
``jsonschema`` is NOT a pinned dependency in ``pyproject.toml`` and
is not installed in the runtime environment as of Arc 12 WU1. Rather
than add the dependency for the tiny subset of JSON Schema the
tool-contract schemas actually use, this module ships a minimal
validator that covers the keywords every shipped schema (and every
schema specified in §3.3.2 / WU3) actually exercises:

  * ``type`` (object, array, string, number, integer, boolean, null)
  * ``properties`` + ``additionalProperties``
  * ``required``
  * ``items`` (single schema, not the tuple form)
  * ``enum``
  * ``minLength`` / ``maxLength``  (strings)
  * ``minimum`` / ``maximum``      (numbers / integers)
  * ``pattern``                    (strings; compiled with ``re``)

This is deliberately a minimal validator. If a future tool needs a
keyword we have not implemented, the right answer is to add
``jsonschema`` as a real dependency and replace this module with a
thin wrapper. WU1 commit message documents this choice.

The fail mode is `SchemaValidationError`, a single exception type
the broker can catch uniformly. Errors carry a JSON-pointer-ish
``path`` field so audit logs can pinpoint the offending field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class SchemaValidationError(Exception):
    """Raised when a payload fails JSON-Schema validation.

    Attributes
    ----------
    path : str
        JSON-pointer-ish location of the offending field, e.g.
        ``"properties.recipient"`` or ``"items[2]"``.
    """

    def __init__(self, message: str, path: str = "") -> None:
        self.path = path
        super().__init__(f"{message} (at {path!r})" if path else message)


@dataclass
class _Ctx:
    """Internal walker state."""

    path: str


def _fail(msg: str, ctx: _Ctx) -> None:
    raise SchemaValidationError(msg, path=ctx.path)


_TYPE_PY = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _check_type(value: Any, expected: str, ctx: _Ctx) -> None:
    py_t = _TYPE_PY.get(expected)
    if py_t is None:
        _fail(f"unsupported schema type {expected!r}", ctx)
    # JSON Schema treats integers as not-booleans (bool is a subclass
    # of int in Python). Special-case so True/False does not satisfy
    # `type: integer`.
    if expected == "integer" and isinstance(value, bool):
        _fail("expected integer, got boolean", ctx)
    if expected == "number" and isinstance(value, bool):
        _fail("expected number, got boolean", ctx)
    if not isinstance(value, py_t):
        _fail(
            f"expected {expected!r}, got {type(value).__name__}",
            ctx,
        )


def _walk(value: Any, schema: dict[str, Any], ctx: _Ctx) -> None:
    if not isinstance(schema, dict):
        _fail("schema node is not an object", ctx)

    # type
    expected_type = schema.get("type")
    if expected_type is not None:
        if isinstance(expected_type, list):
            # Any of the listed types is acceptable.
            matched = False
            for t in expected_type:
                try:
                    _check_type(value, t, ctx)
                    matched = True
                    break
                except SchemaValidationError:
                    continue
            if not matched:
                _fail(
                    f"expected one of {expected_type!r}, "
                    f"got {type(value).__name__}",
                    ctx,
                )
        else:
            _check_type(value, expected_type, ctx)

    # enum
    if "enum" in schema:
        if value not in schema["enum"]:
            _fail(f"value not in enum {schema['enum']!r}", ctx)

    # object
    if isinstance(value, dict):
        props = schema.get("properties", {})
        required = schema.get("required", [])
        for r in required:
            if r not in value:
                _fail(f"missing required property {r!r}", ctx)
        additional_ok = schema.get("additionalProperties", True)
        for k, v in value.items():
            sub_ctx = _Ctx(path=f"{ctx.path}.{k}" if ctx.path else k)
            if k in props:
                _walk(v, props[k], sub_ctx)
            else:
                if additional_ok is False:
                    _fail(f"unexpected property {k!r}", sub_ctx)
                elif isinstance(additional_ok, dict):
                    _walk(v, additional_ok, sub_ctx)

    # array
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                sub_ctx = _Ctx(path=f"{ctx.path}[{i}]")
                _walk(item, item_schema, sub_ctx)

    # string
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            _fail(
                f"string shorter than minLength={schema['minLength']}",
                ctx,
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _fail(
                f"string longer than maxLength={schema['maxLength']}",
                ctx,
            )
        if "pattern" in schema:
            if not re.search(schema["pattern"], value):
                _fail(
                    f"string does not match pattern {schema['pattern']!r}",
                    ctx,
                )

    # numeric
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _fail(
                f"value < minimum={schema['minimum']}",
                ctx,
            )
        if "maximum" in schema and value > schema["maximum"]:
            _fail(
                f"value > maximum={schema['maximum']}",
                ctx,
            )


def validate_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str = "",
) -> None:
    """Validate ``value`` against ``schema`` or raise
    :class:`SchemaValidationError`.

    The validator supports the subset of JSON Schema documented in
    the module docstring. A schema that uses a keyword not in the
    supported subset is validated leniently (the unknown keyword is
    ignored) so adding a documentation-only ``description`` or
    ``examples`` key does not trip validation.
    """

    _walk(value, schema, _Ctx(path=path))


__all__ = ["validate_schema", "SchemaValidationError"]
