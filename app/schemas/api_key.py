"""
API key schemas.

Permission vocabulary (Step 24):
- chat           : may call /api/v1/chat and /chat/stream
- sessions       : may manage sessions under its scope
- admin          : may manage domains/agents/knowledge/keys WITHIN its scope
                   (tenant-, domain-, or agent-scoped based on key fields)
- platform_admin : may act across all tenants (VantageMind operators only)

Scope is determined by the key's tenant_id / domain_id / agent_id columns,
not by the permissions list. Permissions gate WHICH actions;
scope gates WHICH rows those actions may touch.

Step 27a: ALLOWED_PERMISSIONS enum validator added to catch typos like
`platformadmin` or `platform-admin` at mint time, before they reach the
DB and silently bypass `"platform_admin" in permissions` checks.

Step 30b commit (a) of step-30b-embed-key-issuance: embed-key issuance
schemas added below the admin-key schemas. The single source of truth
for the embed permission contract lives here as EMBED_REQUIRED_PERMISSIONS
so the runtime gate (app/api/widget_deps.require_embed_key) and the
issuance path (app/api/v1/admin.create_embed_key) cannot drift.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Step 27a: single source of truth for valid permission strings.
# Any addition must update this set AND the permission-matrix docs in Section 2.3
# of the canonical recap.
ALLOWED_PERMISSIONS: frozenset[str] = frozenset({
    "chat",
    "sessions",
    "admin",
    "platform_admin",
})


# Step 30b commit (a) of step-30b-embed-key-issuance: embed keys at v1
# carry exactly this permission set. The lockstep contract with Step 30c
# (action classifier) requires that any tool path require either
# 'tool:routine' / 'tool:notify' / 'tool:approval' explicitly, which the
# v1 widget cannot mint. Until 30c lands, embed keys are conversational
# only. This frozenset is imported by app/api/widget_deps.py for the
# runtime gate and by the issuance validator for mint-time enforcement;
# they MUST be the same object.
EMBED_REQUIRED_PERMISSIONS: frozenset[str] = frozenset({"chat"})


# Step 30b commit (a) of step-30b-embed-key-issuance: branding knob
# length caps. These match the design comments in the alembic migration
# (a7c1f4e92b85) verbatim. Any change here must also update the migration
# docstring -- the column is JSONB and does not enforce these at the DB
# layer, so the schema is the only enforcement point.
_DISPLAY_NAME_MAX = 50
_GREETING_MESSAGE_MAX = 240
_ACCENT_COLOR_REGEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_HTML_FRAGMENT_REGEX = re.compile(r"[<>]")


# Step 30b commit (a) of step-30b-embed-key-issuance: origin guardrails.
# An origin per RFC 6454 is exactly scheme + host (+ optional default-
# stripped port) with no path, query, fragment, or wildcard. The widget
# runtime gate (require_embed_key) lowercases scheme/host before
# comparison, so we accept any case here and normalize. Reject anything
# that smells like a path, a wildcard, or a missing scheme to avoid
# writing malformed entries to the DB -- the widget runtime cannot un-
# break a bad row at request time.
_ORIGIN_REGEX = re.compile(
    r"^https?://[a-zA-Z0-9.-]+(?::\d{1,5})?$"
)


class ApiKeyCreate(BaseModel):
    tenant_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        description=(
            "NULL for platform-admin keys (cross-tenant bypass via "
            "platform_admin permission per Invariant 5)."
        ),
    )
    domain_id: str | None = None
    agent_id: str | None = None
    luciel_instance_id: int | None = Field(  # Step 24.5
        default=None,
        description=(
            "Pin this key to a specific LucielInstance. When set, the key "
            "can only chat with that one Luciel. Admin keys leave this null."
        ),
    )
    display_name: str
    permissions: list[str] = Field(
        default_factory=lambda: ["chat", "sessions"]
    )
    rate_limit: int = Field(default=1000, ge=0)
    created_by: str | None = None

    @field_validator("permissions")
    @classmethod
    def _validate_permissions(cls, v: list[str]) -> list[str]:
        """
        Step 27a: reject unknown permission strings at mint time.

        Motivation: pre-27a, `ApiKeyCreate(permissions=["platformadmin"])`
        (missing underscore) would land in the DB and silently fail every
        `"platform_admin" in permissions` check downstream, producing a key
        that looks privileged but has zero effective permissions. This
        validator raises ValueError at mint time so the typo surfaces loudly.
        """
        if not isinstance(v, list):
            raise ValueError("permissions must be a list of strings")
        if not v:
            raise ValueError("permissions must not be empty")
        unknown = [p for p in v if p not in ALLOWED_PERMISSIONS]
        if unknown:
            raise ValueError(
                f"unknown permission(s): {unknown!r}. "
                f"allowed: {sorted(ALLOWED_PERMISSIONS)}"
            )
        # normalize: dedupe while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for p in v:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key_prefix: str
    tenant_id: str | None
    domain_id: str | None
    agent_id: str | None
    display_name: str
    permissions: list[str]
    rate_limit: int
    active: bool
    created_by: str | None
    created_at: datetime
    luciel_instance_id: int | None = None


class ApiKeyCreateResponse(BaseModel):
    """Returned only at creation time. The raw_key is shown once and never again."""

    api_key: ApiKeyRead
    raw_key: str


# ---------------------------------------------------------------------
# Step 30b commit (a) of step-30b-embed-key-issuance
# ---------------------------------------------------------------------
#
# Embed-key issuance schemas. Embed keys are a separate credential class
# that gates the public chat widget. They live on the same api_keys
# table as admin keys (see migration a7c1f4e92b85) but carry stricter
# invariants enforced at issuance time AND at request time:
#
#   - key_kind == 'embed'                    (set server-side, not client)
#   - permissions == ['chat']                (set server-side, not client)
#   - allowed_origins is non-empty           (validated below)
#   - rate_limit_per_minute is positive      (validated below)
#   - widget_config is the three-knob shape  (WidgetConfig below)
#
# The endpoint and the CLI both validate against these schemas, so the
# admin REST surface and the operator script cannot drift.
# ---------------------------------------------------------------------


class WidgetConfig(BaseModel):
    """Three-knob branding payload for the embeddable widget.

    The migration docstring (a7c1f4e92b85) is the design contract:
    accent_color is a 7-character hex string (with leading #),
    greeting_message and display_name are length-capped plaintext, and
    NO other knobs exist at v1. ``extra='forbid'`` makes Pydantic
    raise on any unknown field so a customer-supplied logo URL or
    free-form CSS attempt fails loudly at issuance instead of
    silently landing on the api_keys row.

    HTML fragments (any '<' or '>') are rejected because the widget
    bundle (commit (d) of step-30b-chat-widget) renders these fields
    as text content. The defense-in-depth here means even a future
    bundle bug that accidentally renders one of these as innerHTML
    cannot ship XSS into a customer site -- the row never had angle
    brackets to begin with.
    """

    model_config = ConfigDict(extra="forbid")

    accent_color: str | None = Field(
        default=None,
        description=(
            "7-character hex with leading '#', e.g. '#1A2B3C'. Validated "
            "server-side; case is preserved as-supplied."
        ),
    )
    display_name: str | None = Field(
        default=None,
        max_length=_DISPLAY_NAME_MAX,
        description=(
            f"Plaintext label for the widget header. Up to "
            f"{_DISPLAY_NAME_MAX} characters. HTML rejected."
        ),
    )
    greeting_message: str | None = Field(
        default=None,
        max_length=_GREETING_MESSAGE_MAX,
        description=(
            f"Plaintext greeting shown when the widget panel opens. Up "
            f"to {_GREETING_MESSAGE_MAX} characters. HTML rejected."
        ),
    )

    @field_validator("accent_color")
    @classmethod
    def _validate_accent_color(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _ACCENT_COLOR_REGEX.match(v):
            raise ValueError(
                "accent_color must be a 7-character hex string with "
                "leading '#', e.g. '#1A2B3C'."
            )
        return v

    @field_validator("display_name", "greeting_message")
    @classmethod
    def _reject_html(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if _HTML_FRAGMENT_REGEX.search(v):
            raise ValueError(
                "HTML fragments are not allowed in widget_config text "
                "fields. The widget renders these as plaintext; angle "
                "brackets indicate either a misuse or an injection "
                "attempt and are rejected at issuance."
            )
        return v

    def to_jsonb(self) -> dict[str, Any]:
        """Serialize to a plain dict for the JSONB column.

        ``model_dump(exclude_none=True)`` keeps the row compact -- a
        widget_config with only accent_color set lands as
        {'accent_color': '#1A2B3C'} rather than {'accent_color': ...,
        'display_name': null, 'greeting_message': null}. The runtime
        gate already treats absent keys and null keys identically.
        """
        return self.model_dump(exclude_none=True)


class EmbedKeyCreate(BaseModel):
    """Request body for POST /admin/embed-keys.

    Notice what is NOT in this schema: ``key_kind`` (server forces
    'embed'), ``permissions`` (server forces ['chat']), ``rate_limit``
    (admin-key per-day cap, irrelevant to embed keys), ``agent_id`` and
    ``luciel_instance_id`` (embed keys are tenant- or domain-scoped at
    v1; per-Luciel pinning lands in a later step). The narrowness of
    the request surface is the point: the operator chooses where the
    key applies and how it brands, never what credential class it is.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(
        min_length=2,
        max_length=100,
        description=(
            "Required. Embed keys MUST be tenant-scoped -- a NULL "
            "tenant_id would mean a customer-shipped key that crosses "
            "tenants, which is the exact failure mode embed keys exist "
            "to prevent."
        ),
    )
    domain_id: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Optional. When set, the key only resolves chat for that "
            "domain within the tenant."
        ),
    )
    display_name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Operator-facing label for this key in the admin UI "
            "(distinct from widget_config.display_name, which is the "
            "customer-facing widget header)."
        ),
    )
    allowed_origins: list[str] = Field(
        min_length=1,
        description=(
            "Required, non-empty. Each entry is exactly scheme + host "
            "(+ optional port). No paths, queries, fragments, or "
            "wildcards. Origin matching at request time is case-"
            "insensitive on scheme and host."
        ),
    )
    rate_limit_per_minute: int = Field(
        gt=0,
        le=10000,
        description=(
            "Per-minute burst cap. The per-day rate_limit column "
            "(admin-key concept) does not apply to embed keys; the "
            "per-minute cap is the only quota the runtime enforces."
        ),
    )
    widget_config: WidgetConfig = Field(
        default_factory=WidgetConfig,
        description=(
            "Three-knob branding. Defaults to all-null (widget renders "
            "with built-in defaults)."
        ),
    )
    created_by: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Audit-trail field. Operator email or service identifier "
            "that initiated the issuance."
        ),
    )

    @field_validator("allowed_origins")
    @classmethod
    def _validate_origins(cls, v: list[str]) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("allowed_origins must be a list of strings")
        if not v:
            raise ValueError("allowed_origins must be non-empty")
        cleaned: list[str] = []
        seen: set[str] = set()
        for entry in v:
            if not isinstance(entry, str):
                raise ValueError(
                    f"allowed_origins entry must be a string, got: {type(entry).__name__}"
                )
            stripped = entry.strip()
            if not stripped:
                raise ValueError(
                    "allowed_origins entries must not be empty strings"
                )
            if "*" in stripped:
                raise ValueError(
                    f"wildcard origins are not allowed: {entry!r}. "
                    "Each entry must be an exact scheme+host(+port)."
                )
            # Normalize first (lowercase scheme and host), THEN validate.
            # Operators frequently paste origins copy-pasted from a browser
            # address bar where the scheme can be mixed-case. The runtime
            # gate (require_embed_key) lowercases the incoming Origin
            # header before comparison, so we must store the lowercased
            # form to match. Validating the lowercased form also lets the
            # regex stay case-sensitive (faster, simpler, and a single
            # source of truth for what 'shape' an origin has).
            normalized = stripped.lower()
            if not _ORIGIN_REGEX.match(normalized):
                raise ValueError(
                    f"invalid origin: {entry!r}. Origins must be exactly "
                    f"scheme + host (+ optional port), e.g. "
                    f"'https://customer.com' or 'https://customer.com:8443'. "
                    f"No paths, queries, fragments, trailing slashes, or wildcards."
                )
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)
        return cleaned

    @model_validator(mode="after")
    def _ensure_widget_config_present(self) -> EmbedKeyCreate:
        # WidgetConfig has a default_factory so this is always set, but
        # guard against an explicit None being passed in (which Pydantic
        # would otherwise accept).
        if self.widget_config is None:
            raise ValueError(
                "widget_config is required (it may be empty {}, but it "
                "may not be null)."
            )
        return self


class EmbedKeyRead(BaseModel):
    """Read-side projection of an embed key.

    Distinct from ApiKeyRead because the embed-key shape exposes the
    four widget columns and omits agent_id / luciel_instance_id which
    are always NULL on embed keys at v1.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    key_prefix: str
    tenant_id: str
    domain_id: str | None
    display_name: str
    key_kind: str
    allowed_origins: list[str]
    rate_limit_per_minute: int
    widget_config: dict[str, Any]
    active: bool
    created_by: str | None
    created_at: datetime


class EmbedKeyCreateResponse(BaseModel):
    """Returned only at issuance. The raw_key is shown once and never again.

    The customer pastes raw_key into their site's HTML; the operator
    cannot retrieve it later (only the SHA-256 hash is stored). If the
    customer loses the key, the operator deactivates the row and mints
    a new one (Pattern E: deactivate, never delete).

    `warnings` carries non-fatal operator-facing notes from issuance
    (e.g. tenant-wide mints that skip the scope-prompt preflight; see
    ARCHITECTURE §3.2.2 'Issuance'). Defaults to an empty list to keep
    the response backward-compatible with pre-Step-30d clients.
    """

    embed_key: EmbedKeyRead
    raw_key: str
    warnings: list[str] = []