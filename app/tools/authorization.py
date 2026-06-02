"""Tool authorisation — Arc 12 WU2.

The broker default-deny gate, factored into its own module so:

  * the broker stays focused on dispatch / classification / schema
    validation;
  * Arc 14's agentic loop has a single, documented seam to construct
    or extend the authoriser (cycle accounting, fan-out budget, etc.
    bolt on here);
  * tests can inject a synthetic authoriser without monkey-patching
    the broker.

Stable interface (KEEP THIS SIGNATURE)
--------------------------------------

``ToolAuthorizer.authorize(tool, context) -> AuthorizationDecision``

* ``tool``     — the ``LucielTool`` the broker is about to dispatch.
* ``context``  — the immutable ``ToolContext`` the broker constructed
  for this invocation. Carries ``admin_id`` + ``instance_id`` (and,
  per WU1, optional ``session`` + ``inbound_message_id``).

Returns an ``AuthorizationDecision``:

* ``allowed: bool``           — True ⇒ proceed; False ⇒ refuse.
* ``reason: str``             — short machine-readable code; the broker
                                surfaces this in the structured
                                tool-error metadata.
* ``message: str``            — admin-facing description.
* ``failure_kind: str``       — one of:
    - ``"unauthorized"``              (no live row / disabled / revoked)
    - ``"tier_not_permitted"``        (tool.requires_tier rejects tier)
    - ``"channel_not_enabled"``       (tool.requires_channels unmet)
    - ``"connection_not_configured"`` (Arc 15 WU5 — tool.requires_connection
                                       has no live ``connected`` row)
    - ``""``                          (when ``allowed=True``)

Arc 14 will compose additional checks (cycle detection for
``call_sibling_luciel``, fan-out budget across the composition tree)
on top of this interface — the dispatch contract MUST remain stable.

Tier + channel structural checks (WU2 founder ruling)
-----------------------------------------------------

The WU2 brief asks the broker to enforce ``requires_tier`` and
``requires_channels`` at this gate if the data is available, and to
leave a clearly-marked marker if it is not. WU2-as-shipped did not yet
carry the admin's tier or the enabled channel set on ``ToolContext``.
We therefore:

  * Implement the *structural* check points here so the wiring is in
    place — methods ``_check_tier`` and ``_check_channels`` are
    plumbed through ``authorize`` and gated on the context fields.
  * When the data is absent, tier and channel enforcement *skip* and
    emit a debug log message so a maintainer knows the gate is
    structurally live but data-blind. The authorisation-row check
    (the load-bearing part of WU2) is enforced unconditionally.

Arc 14 U5 — dispatch-time re-check threaded (§3.3.3)
----------------------------------------------------
The §3.3.3 "Arc 14 hardening OPTION" is now taken: the orchestrator's
ACT step threads ``admin_tier`` + ``enabled_channels`` onto the
``ToolContext`` (reusing the tier/channel values the loop already
resolves for the grounding floor + arbiter). So on the agentic-loop
dispatch path BOTH structural checks now run with real data — the
belt-and-suspenders gate-1 re-check is live. The skip branches remain
ONLY for call sites that still construct a context without these fields
(e.g. legacy/unit-test contexts); they are an intentional
backward-compatible default, not unfinished Arc 14 work.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

from app.tools.base import LucielTool, ToolContext

logger = logging.getLogger(__name__)


# =====================================================================
# Public decision shape
# =====================================================================


@dataclass(frozen=True)
class AuthorizationDecision:
    """Result of an authorisation check.

    The broker translates an ``allowed=False`` decision into a
    structured ``ToolResult(success=False, ...)`` whose metadata
    carries the reason + failure_kind so the runtime layer and
    audit row construction can branch.
    """

    allowed: bool
    reason: str
    message: str
    failure_kind: str = ""

    @classmethod
    def allow(cls) -> "AuthorizationDecision":
        return cls(
            allowed=True,
            reason="authorized",
            message="",
            failure_kind="",
        )

    @classmethod
    def deny(
        cls, *, reason: str, message: str, failure_kind: str = "unauthorized"
    ) -> "AuthorizationDecision":
        return cls(
            allowed=False,
            reason=reason,
            message=message,
            failure_kind=failure_kind,
        )


# =====================================================================
# Stable interface — Arc 14 will compose on top of this
# =====================================================================


class ToolAuthorizer(Protocol):
    """Stable interface the broker calls before dispatch.

    Implementations:
      * ``DefaultDenyToolAuthorizer``  — production WU2 path. Looks
        up the live row in ``instance_tool_authorizations`` via the
        repository and refuses if absent / revoked / disabled.
      * Test doubles construct ``AuthorizationDecision`` directly.
      * Arc 14 will subclass / wrap ``DefaultDenyToolAuthorizer`` to
        add cycle detection and fan-out budget — KEEP this method
        signature stable.
    """

    def authorize(
        self, tool: LucielTool, context: ToolContext
    ) -> AuthorizationDecision: ...


# =====================================================================
# Production implementation — default-deny on the live row
# =====================================================================


class DefaultDenyToolAuthorizer:
    """Default-deny authoriser backed by ``instance_tool_authorizations``.

    Construct with a session-getter callable so the broker can stay
    framework-agnostic (FastAPI dependency injection writes sessions
    to ``ctx.session``; the unit tests build a session and pass it
    in directly). If ``ctx.session`` is ``None`` and no session
    getter is configured, the authoriser refuses — there is no
    silent-allow path.
    """

    def __init__(
        self,
        *,
        session_factory: "Optional[callable]" = None,  # type: ignore[name-defined]
    ) -> None:
        """``session_factory`` is an optional zero-arg callable that
        returns a SQLAlchemy ``Session`` to use when ``ctx.session``
        is not provided. In production the API deps fill ``ctx.session``
        directly so this fallback is rarely used; tests and the
        worker path use it."""
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Stable entry point
    # ------------------------------------------------------------------

    def authorize(
        self, tool: LucielTool, context: ToolContext
    ) -> AuthorizationDecision:
        # 1. Load-bearing: the per-instance authorisation row check.
        row_decision = self._check_row(tool, context)
        if not row_decision.allowed:
            return row_decision

        # 2. Tier enforcement (§3.3.3). Arc 14 U5 threads ``admin_tier``
        #    onto ToolContext from the ACT step, so on the agentic-loop
        #    path this runs with real data. Contexts without the field
        #    (legacy/test) skip with a debug log — see module docstring.
        tier_decision = self._check_tier(tool, context)
        if not tier_decision.allowed:
            return tier_decision

        # 3. Channel enforcement (§3.3.3). Arc 14 U5 threads
        #    ``enabled_channels`` onto ToolContext from the ACT step;
        #    contexts without the field skip — see module docstring.
        channel_decision = self._check_channels(tool, context)
        if not channel_decision.allowed:
            return channel_decision

        # 4. Connection enforcement (§3.3.3 third dispatch gate, Arc 15
        #    WU5). For a tool that declares ``requires_connection`` the
        #    broker requires a live ``instance_connections`` row with
        #    ``status == 'connected'``. Tools with ``requires_connection
        #    is None`` skip this gate. NEVER a silent failure.
        connection_decision = self._check_connection(tool, context)
        if not connection_decision.allowed:
            return connection_decision

        return AuthorizationDecision.allow()

    # ------------------------------------------------------------------
    # Step 1 — the load-bearing row lookup
    # ------------------------------------------------------------------

    def _check_row(
        self, tool: LucielTool, context: ToolContext
    ) -> AuthorizationDecision:
        # Default-deny: empty admin_id / instance_id (the WU1
        # placeholder context) refuses immediately. This catches
        # call sites that forgot to thread a real ToolContext.
        if not context.admin_id or not context.instance_id:
            return AuthorizationDecision.deny(
                reason="no_tool_context",
                message=(
                    "Tool dispatch refused: no admin/instance context "
                    "supplied. Per Arc 12 WU2 every invocation must "
                    "carry an explicit ToolContext."
                ),
                failure_kind="unauthorized",
            )

        # Fetch the session — prefer the one threaded onto ctx; fall
        # back to the session_factory if configured. If neither is
        # present, refuse — there is no silent-allow path.
        session = context.session
        owns_session = False
        if session is None and self._session_factory is not None:
            session = self._session_factory()
            owns_session = True
        if session is None:
            logger.warning(
                "DefaultDenyToolAuthorizer: no DB session available "
                "for admin=%s instance=%s tool=%s — refusing.",
                context.admin_id, context.instance_id, tool.tool_id,
            )
            return AuthorizationDecision.deny(
                reason="no_db_session",
                message=(
                    "Tool dispatch refused: authorisation lookup "
                    "could not access a database session."
                ),
                failure_kind="unauthorized",
            )

        try:
            from app.repositories.instance_tool_authorization_repository import (
                InstanceToolAuthorizationRepository,
            )
            repo = InstanceToolAuthorizationRepository(session)
            row = repo.get_live(
                admin_id=context.admin_id,
                instance_id=context.instance_id,
                tool_id=tool.tool_id,
            )
        finally:
            if owns_session:
                try:
                    session.close()
                except Exception:  # pragma: no cover
                    logger.exception(
                        "DefaultDenyToolAuthorizer: session.close() raised"
                    )

        if row is None:
            return AuthorizationDecision.deny(
                reason="no_authorization_row",
                message=(
                    f"Tool '{tool.tool_id}' is not authorised on this "
                    "instance. Per Arc 12 WU2 the broker refuses any "
                    "tool that does not have a live row in "
                    "instance_tool_authorizations."
                ),
                failure_kind="unauthorized",
            )

        if not row.enabled:
            return AuthorizationDecision.deny(
                reason="authorization_disabled",
                message=(
                    f"Tool '{tool.tool_id}' is paused on this instance "
                    "(authorisation row exists but enabled=False)."
                ),
                failure_kind="unauthorized",
            )

        return AuthorizationDecision.allow()

    # ------------------------------------------------------------------
    # Step 2 — tier check (§3.3.3 dispatch-time re-check)
    # ------------------------------------------------------------------

    def _check_tier(
        self, tool: LucielTool, context: ToolContext
    ) -> AuthorizationDecision:
        # Arc 14 U5 threads ``admin_tier`` onto ToolContext from the
        # orchestrator ACT step, so this re-check runs with real data on
        # the agentic-loop path. Contexts that omit the field (legacy /
        # unit-test) skip the re-check — an intentional backward-compat
        # default, not unfinished work.
        admin_tier = getattr(context, "admin_tier", None)
        if not admin_tier:
            logger.debug(
                "Tool authorisation: tier re-check SKIPPED — admin_tier "
                "not present on ToolContext. tool=%s requires_tier=%s",
                tool.tool_id, tool.requires_tier,
            )
            return AuthorizationDecision.allow()

        if admin_tier not in tool.requires_tier:
            return AuthorizationDecision.deny(
                reason="tier_not_permitted",
                message=(
                    f"Tool '{tool.tool_id}' requires tier in "
                    f"{tool.requires_tier!r}; admin is on '{admin_tier}'."
                ),
                failure_kind="tier_not_permitted",
            )
        return AuthorizationDecision.allow()

    # ------------------------------------------------------------------
    # Step 3 — channel check (§3.3.3 dispatch-time re-check)
    # ------------------------------------------------------------------

    def _check_channels(
        self, tool: LucielTool, context: ToolContext
    ) -> AuthorizationDecision:
        # Arc 14 U5 threads ``enabled_channels`` onto ToolContext from
        # the orchestrator ACT step, so this re-check runs with real data
        # on the agentic-loop path. Contexts that omit the field skip the
        # re-check — backward-compat default, not unfinished work.
        required = tool.requires_channels
        if not required:
            return AuthorizationDecision.allow()

        enabled_channels = getattr(context, "enabled_channels", None)
        if enabled_channels is None:
            logger.debug(
                "Tool authorisation: channel re-check SKIPPED — "
                "enabled_channels not present on ToolContext. "
                "tool=%s requires_channels=%s",
                tool.tool_id, required,
            )
            return AuthorizationDecision.allow()

        missing = required - frozenset(enabled_channels)
        if missing:
            return AuthorizationDecision.deny(
                reason="channel_not_enabled",
                message=(
                    f"Tool '{tool.tool_id}' requires channel(s) "
                    f"{sorted(missing)!r} which are not enabled on "
                    "this instance."
                ),
                failure_kind="channel_not_enabled",
            )
        return AuthorizationDecision.allow()

    # ------------------------------------------------------------------
    # Step 4 — connection check (§3.3.3 third dispatch gate, Arc 15 WU5)
    # ------------------------------------------------------------------

    def _check_connection(
        self, tool: LucielTool, context: ToolContext
    ) -> AuthorizationDecision:
        """Refuse a connection-bearing tool that has no live ``connected``
        ``instance_connections`` row.

        Tools with ``requires_connection is None`` skip the gate. When a
        connection IS required the gate is load-bearing: like ``_check_row``
        it refuses if it cannot reach a DB session — there is no
        silent-allow path. A missing/non-``connected`` row yields a
        structured deny with ``failure_kind="connection_not_configured"``,
        the same shape as the default-deny refusal so the agentic loop
        reasons uniformly.
        """
        required_connection = getattr(tool, "requires_connection", None)
        if required_connection is None:
            return AuthorizationDecision.allow()

        session = context.session
        owns_session = False
        if session is None and self._session_factory is not None:
            session = self._session_factory()
            owns_session = True
        if session is None:
            logger.warning(
                "DefaultDenyToolAuthorizer: no DB session available for "
                "connection check admin=%s instance=%s tool=%s — refusing.",
                context.admin_id, context.instance_id, tool.tool_id,
            )
            return AuthorizationDecision.deny(
                reason="no_db_session",
                message=(
                    "Tool dispatch refused: connection lookup could not "
                    "access a database session."
                ),
                failure_kind="connection_not_configured",
            )

        try:
            from app.repositories.instance_connection_repository import (
                InstanceConnectionRepository,
            )
            repo = InstanceConnectionRepository(session)
            row = repo.get_live_by_type(
                admin_id=context.admin_id,
                instance_id=context.instance_id,
                connection_type=required_connection,
            )
        finally:
            if owns_session:
                try:
                    session.close()
                except Exception:  # pragma: no cover
                    logger.exception(
                        "DefaultDenyToolAuthorizer: session.close() raised"
                    )

        if row is None:
            return AuthorizationDecision.deny(
                reason="connection_not_configured",
                message=(
                    f"Tool '{tool.tool_id}' requires a live "
                    f"'{required_connection}' connection, but none is "
                    "configured on this instance. Configure it under "
                    "Connections before this tool can run."
                ),
                failure_kind="connection_not_configured",
            )

        if row.status != "connected":
            return AuthorizationDecision.deny(
                reason="connection_not_connected",
                message=(
                    f"Tool '{tool.tool_id}' requires a 'connected' "
                    f"'{required_connection}' connection; the configured "
                    f"connection is '{row.status}'. Reconnect it before "
                    "this tool can run."
                ),
                failure_kind="connection_not_configured",
            )

        return AuthorizationDecision.allow()


# =====================================================================
# Convenience: a flat-allow authoriser used by legacy / cognition paths
# =====================================================================


class _AlwaysAllowAuthorizer:
    """Test/legacy authoriser that allows every call.

    NOT used in production. Exposed so callers that explicitly want
    to bypass authorisation (a unit test exercising the classifier
    or schema validation in isolation) have a documented mechanism
    rather than ad-hoc monkey-patching.
    """

    def authorize(
        self, tool: LucielTool, context: ToolContext
    ) -> AuthorizationDecision:
        return AuthorizationDecision.allow()


__all__ = [
    "AuthorizationDecision",
    "ToolAuthorizer",
    "DefaultDenyToolAuthorizer",
    "_AlwaysAllowAuthorizer",
]
