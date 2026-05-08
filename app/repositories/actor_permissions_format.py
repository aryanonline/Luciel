"""Actor-permissions on-disk format helpers for admin_audit_logs.actor_permissions.

Step 29.y gap-fix Commit 1 (D-actor-permissions-comma-fragility-2026-05-07).

# Problem

Pre-29.y, AuditContext.permissions_str joined permissions with ',' and the
defensive parser in AuditContext.from_request split on ','. That assumes no
permission token ever contains a comma. The contract was never enforced --
a future permission like 'role:admin,read' would silently fragment into
['role:admin', 'read'] on round-trip, and the audit row's actor_permissions
column would be unparseable in any structured way.

# Why we cannot just rewrite history to JSON

actor_permissions is a chained field in the admin_audit_logs hash chain
(see app/repositories/audit_chain.py::_CHAIN_FIELDS, Pillar 23, migration
8ddf0be96f44). Every historical row was hashed with the comma-string form.
Rewriting historical column values to JSON would either break the chain
(if we leave row_hash alone) or require recomputing row_hash for every
historical row (which mutates audit history -- a Pattern E forensics red
line).

# Solution: dual-format on read, JSON on write, no history rewrite

  - serialize_actor_permissions(perms) -> str:
        Produces the canonical NEW on-disk form, which is JSON of a
        sorted list of strings, e.g. '["admin","worker"]'. New audit
        rows written from this commit forward use this form.

  - parse_actor_permissions(value) -> tuple[str, ...]:
        Accepts either the legacy comma form ('admin,worker') OR the
        new JSON form ('["admin","worker"]'). Returns a tuple of perm
        tokens in stable sorted order. Used by AuditContext.from_request
        for the defensive string-input branch, by the audit-log API
        serializer, and by any read-side consumer.

# Hash-chain invariants preserved

Old rows still contain their original comma-string column values.
canonical_row_hash() reads actor_permissions verbatim from each row, so
old rows recompute to the same hash they already store. New rows store
the JSON string and hash that JSON string. Pillar 23's row-by-row
recompute remains stable. No row_hash is ever rewritten.

# Forward-only invariant: NO COMMAS allowed in individual perm tokens

Even though the JSON form is robust to commas, we still REJECT any
permission token containing ',' or '"' or '\\' at serialize time. This
keeps the legacy comma-form parser sound for the entire deploy window
where old rows still exist on disk, and prevents future drift if anyone
ever reverts the on-disk format.
"""
from __future__ import annotations

import json
from typing import Iterable


# Maximum stored length must respect admin_audit_logs.actor_permissions
# column = String(500). JSON encoding of a list of short tokens is well
# under that. We assert at serialize time as a defensive guard.
_MAX_SERIALIZED_LEN = 500


class ActorPermissionsFormatError(ValueError):
    """Raised when a permission token violates the storage contract."""


def _validate_token(token: str) -> str:
    if not isinstance(token, str):
        raise ActorPermissionsFormatError(
            f"permission tokens must be str, got {type(token).__name__}"
        )
    stripped = token.strip()
    if not stripped:
        raise ActorPermissionsFormatError("empty permission token")
    # Disallow chars that would corrupt either the legacy comma form or
    # the JSON form. This is the forward-only invariant: any future
    # permission vocabulary must use plain identifiers, not comma- or
    # quote-laden strings. ScopePolicy permissions today are simple
    # tokens like 'admin', 'chat', 'sessions', 'worker', 'system',
    # 'platform_admin' -- this validation makes the implicit contract
    # explicit.
    for bad in (",", '"', "\\", "\n", "\r", "\t"):
        if bad in stripped:
            raise ActorPermissionsFormatError(
                f"permission token {stripped!r} contains forbidden "
                f"character {bad!r}; tokens must be plain identifiers"
            )
    return stripped


def serialize_actor_permissions(perms: Iterable[str] | None) -> str | None:
    """Serialize an iterable of permission tokens to the canonical on-disk
    form (JSON of a sorted list of strings).

    Returns None for empty/None input so the column stores SQL NULL,
    matching the prior comma-form behaviour for empty input.
    """
    if perms is None:
        return None
    tokens = [_validate_token(p) for p in perms]
    if not tokens:
        return None
    # Sort for deterministic round-trip and stable hashing on the
    # caller's side. Duplicates collapse.
    canonical = sorted(set(tokens))
    serialized = json.dumps(canonical, separators=(",", ":"))
    if len(serialized) > _MAX_SERIALIZED_LEN:
        raise ActorPermissionsFormatError(
            f"serialized actor_permissions length {len(serialized)} "
            f"exceeds column cap {_MAX_SERIALIZED_LEN}"
        )
    return serialized


def parse_actor_permissions(value: str | None) -> tuple[str, ...]:
    """Parse the on-disk actor_permissions string into a tuple of tokens.

    Accepts BOTH formats so we can read pre-29.y rows (comma form) and
    post-29.y rows (JSON form) uniformly. Returns () for None / empty.

    The discriminator is the first non-whitespace char: '[' means JSON,
    anything else is treated as legacy comma form.
    """
    if value is None:
        return ()
    s = value.strip()
    if not s:
        return ()
    if s.startswith("["):
        try:
            decoded = json.loads(s)
        except json.JSONDecodeError as exc:
            raise ActorPermissionsFormatError(
                f"actor_permissions value starts with '[' but is not "
                f"valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, list):
            raise ActorPermissionsFormatError(
                f"actor_permissions JSON must be a list, got "
                f"{type(decoded).__name__}"
            )
        tokens = tuple(_validate_token(p) for p in decoded)
        return tokens
    # Legacy comma form. Pre-29.y rows. Validate per-token here too --
    # if a historical row somehow contains a forbidden char it surfaces
    # as a forensic alert rather than silently corrupting downstream.
    parts = tuple(p.strip() for p in s.split(",") if p.strip())
    return tuple(_validate_token(p) for p in parts)
