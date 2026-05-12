"""Backend-free contract tests for Step 31 sub-branch 1.

Sub-branch 1 lands two changes on the public widget chat surface
(`POST /api/v1/chat/widget`):

  1. Application-level audit log. Three structured `logger.info`
     emissions land on the `/ecs/luciel-backend` CloudWatch stream
     for every widget turn (`widget_chat_turn_received`,
     `widget_chat_session_resolved`, `widget_chat_turn_completed`).
     Closes DRIFTS token
     `D-widget-chat-no-application-level-audit-log-2026-05-10` and
     flips the widget-surface 📋 marker on ARCHITECTURE §3.2.7 ✅.

  2. `create_session_with_identity` route wiring. When the request
     payload carries a `client_claim` field, lazy session creation
     swaps from the legacy anonymous `session_service.create_session(
     user_id=None, ...)` to
     `session_service.create_session_with_identity(claim_type=...,
     claim_value=..., issuing_adapter='widget', ...)` so subsequent
     widget turns from the same visitor join the same
     `conversation_id` per the §3.2.11 design.

Coverage (all AST + import only -- no Postgres, no FastAPI runtime):

    * `ClientClaim` Pydantic schema exists with the documented shape
      (`claim_type` Literal of email|phone|sso_subject; `claim_value`
      str with min_length=1, max_length=512).
    * `ChatWidgetRequest.client_claim` field exists, is nullable,
      defaults None -- preserving the legacy anonymous path.
    * `chat_widget.py` module-level constant
      `WIDGET_ISSUING_ADAPTER = "widget"` exists -- hardcoded so the
      client cannot spoof the issuing adapter.
    * `chat_widget.py` imports `time` at module load (for monotonic
      latency capture).
    * The route emits exactly THREE distinct `logger.info` event
      names with the documented field shapes.
    * Each of the three emissions carries the required scope-bearing
      fields (`tenant_id`, `domain_id`, `session_id` where
      applicable).
    * Critical PII defense: NO `logger.info` call references
      `payload.message` directly (only `message_length` is logged).
    * The `if payload.client_claim is not None` branch exists and
      calls `session_service.create_session_with_identity(...)` with
      `issuing_adapter=WIDGET_ISSUING_ADAPTER`.

End-to-end correctness of the create_session_with_identity wiring
(actual session-row persistence, identity resolution) is covered by
sub-branch 4's e2e harness and the Step 24.5c sub-branch 4 contract
tests this sub-branch builds on (resolver mints + session.user_id
binding). This file is the surface-shape pin.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_SCHEMA_PATH = REPO_ROOT / "app" / "schemas" / "chat.py"
WIDGET_ROUTE_PATH = REPO_ROOT / "app" / "api" / "v1" / "chat_widget.py"


# ---------------------------------------------------------------------
# 1. ClientClaim schema shape
# ---------------------------------------------------------------------

class TestClientClaimSchema:
    def test_class_exists(self):
        from app.schemas.chat import ClientClaim
        # Pydantic BaseModel subclass.
        from pydantic import BaseModel
        assert issubclass(ClientClaim, BaseModel)

    def test_claim_type_is_literal_of_three_values(self):
        from app.schemas.chat import ClientClaim
        fields = ClientClaim.model_fields
        assert "claim_type" in fields
        # Pydantic v2 stores the type on .annotation; pull the
        # Literal args off it. Sorted so the assertion is
        # order-independent.
        import typing
        annotation = fields["claim_type"].annotation
        args = typing.get_args(annotation)
        assert sorted(args) == sorted(["email", "phone", "sso_subject"])

    def test_claim_value_has_bounded_length(self):
        # claim_value must be str with min_length=1 / max_length=512
        # to defend against zero-length spoofing AND DoS via huge
        # payloads going into the resolver's hash bucket. We sniff
        # the JSON schema rather than pydantic internals so the test
        # survives a Pydantic minor-version bump.
        from app.schemas.chat import ClientClaim
        schema = ClientClaim.model_json_schema()
        props = schema["properties"]["claim_value"]
        assert props.get("minLength") == 1
        assert props.get("maxLength") == 512
        # And the underlying type is string.
        assert props.get("type") == "string"


# ---------------------------------------------------------------------
# 2. ChatWidgetRequest.client_claim field shape
# ---------------------------------------------------------------------

class TestChatWidgetRequestClientClaim:
    def test_client_claim_field_exists(self):
        from app.schemas.chat import ChatWidgetRequest
        assert "client_claim" in ChatWidgetRequest.model_fields

    def test_client_claim_defaults_to_none(self):
        # The whole point of the field being optional is to preserve
        # the legacy anonymous widget bundle path -- a customer who
        # has not bumped their <script src> tag yet must still see
        # the route work. Default None == backward compat.
        from app.schemas.chat import ChatWidgetRequest
        f = ChatWidgetRequest.model_fields["client_claim"]
        assert f.default is None

    def test_client_claim_accepts_none_at_runtime(self):
        # Construct without client_claim to lock the legacy path.
        from app.schemas.chat import ChatWidgetRequest
        req = ChatWidgetRequest(message="hello")
        assert req.client_claim is None

    def test_client_claim_accepts_populated_dict(self):
        # The wire shape: nested object the FastAPI router will
        # validate via Pydantic.
        from app.schemas.chat import ChatWidgetRequest, ClientClaim
        req = ChatWidgetRequest(
            message="hello",
            client_claim={
                "claim_type": "email",
                "claim_value": "person@example.com",
            },
        )
        assert isinstance(req.client_claim, ClientClaim)
        assert req.client_claim.claim_type == "email"
        assert req.client_claim.claim_value == "person@example.com"


# ---------------------------------------------------------------------
# 3. chat_widget.py module-level constants & imports
# ---------------------------------------------------------------------

class TestWidgetModuleSurface:
    def test_issuing_adapter_constant_is_widget(self):
        # The constant MUST be a literal string "widget" defined at
        # module level so it is impossible to overwrite via request
        # payload or env. We assert this via AST rather than runtime
        # import because importing chat_widget pulls in the full
        # backend dependency chain (sqlalchemy engine bind, settings
        # validation) and a contract test must not need a live
        # Postgres URL -- same backend-free discipline as Step 24.5c.
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        found = False
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == "WIDGET_ISSUING_ADAPTER"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "widget"
                ):
                    found = True
                    break
            if found:
                break
        assert found, (
            "WIDGET_ISSUING_ADAPTER must be a module-level assignment "
            "with literal value 'widget' in chat_widget.py"
        )

    def test_time_is_imported_at_top_level(self):
        # `time.monotonic()` is the right clock for latency capture
        # (never goes backwards across NTP sync). Pinning the
        # top-level import prevents a future edit from accidentally
        # swapping it for `time.time()` via a lazy-import refactor.
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        top_level_imports: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_imports.append(alias.name)
        assert "time" in top_level_imports


# ---------------------------------------------------------------------
# 4. Three logger.info emissions with the documented event names
# ---------------------------------------------------------------------

def _collect_logger_info_calls(tree: ast.AST) -> list[ast.Call]:
    """Walk an AST and return every `logger.info(...)` Call node."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "logger"
            and func.attr == "info"
        ):
            out.append(node)
    return out


def _call_first_arg_str(call: ast.Call) -> str | None:
    """Return the first positional argument as a string literal or None."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _call_extra_keys(call: ast.Call) -> set[str]:
    """Return the keys of the `extra={...}` kwarg as a set."""
    for kw in call.keywords:
        if kw.arg != "extra":
            continue
        if not isinstance(kw.value, ast.Dict):
            return set()
        keys: set[str] = set()
        for k in kw.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
        return keys
    return set()


class TestAuditLogEmissions:
    def test_three_distinct_event_names_present(self):
        # The three documented events MUST all be emitted at least
        # once via `logger.info("<event>", ...)`. We allow the same
        # event name to be emitted at multiple call sites (e.g. the
        # completion event has three: success, moderation-blocked,
        # error-interrupt) -- the dashboards rely on counting events,
        # not call sites.
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        calls = _collect_logger_info_calls(tree)
        first_args = {_call_first_arg_str(c) for c in calls}
        required = {
            "widget_chat_turn_received",
            "widget_chat_session_resolved",
            "widget_chat_turn_completed",
        }
        missing = required - first_args
        assert not missing, f"missing logger.info event(s): {missing}"

    def test_turn_received_field_shape(self):
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        calls = _collect_logger_info_calls(tree)
        candidates = [
            c for c in calls
            if _call_first_arg_str(c) == "widget_chat_turn_received"
        ]
        assert len(candidates) == 1, (
            "widget_chat_turn_received must emit exactly once per turn "
            "(at request entry, after scope checks)"
        )
        keys = _call_extra_keys(candidates[0])
        required = {
            "event",
            "tenant_id",
            "domain_id",
            "agent_id",
            "luciel_instance_id",
            "embed_key_prefix",
            "message_length",
            "has_session_id",
            "has_client_claim",
        }
        missing = required - keys
        assert not missing, (
            f"widget_chat_turn_received extra={{}} missing keys: {missing}"
        )

    def test_session_resolved_field_shape(self):
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        calls = _collect_logger_info_calls(tree)
        candidates = [
            c for c in calls
            if _call_first_arg_str(c) == "widget_chat_session_resolved"
        ]
        assert len(candidates) == 1, (
            "widget_chat_session_resolved must emit exactly once per turn"
        )
        keys = _call_extra_keys(candidates[0])
        required = {
            "event",
            "tenant_id",
            "domain_id",
            "session_id",
            "user_id",
            "conversation_id",
            "is_new_session",
            "is_new_user",
            "is_new_conversation",
        }
        missing = required - keys
        assert not missing, (
            f"widget_chat_session_resolved extra={{}} missing keys: "
            f"{missing}"
        )

    def test_turn_completed_field_shape_at_every_site(self):
        # The completion event has multiple emission sites (success,
        # moderation-blocked, error-interrupt). EVERY site must carry
        # the same field shape so the dashboard's GROUP BY does not
        # produce ragged rows.
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        calls = _collect_logger_info_calls(tree)
        candidates = [
            c for c in calls
            if _call_first_arg_str(c) == "widget_chat_turn_completed"
        ]
        assert len(candidates) >= 2, (
            "widget_chat_turn_completed must emit at success AND at "
            "moderation-block sites (and may emit on error-interrupt)"
        )
        required = {
            "event",
            "tenant_id",
            "domain_id",
            "session_id",
            "latency_ms",
            "tokens_emitted",
            "blocked_by_moderation",
        }
        for i, call in enumerate(candidates):
            keys = _call_extra_keys(call)
            missing = required - keys
            assert not missing, (
                f"widget_chat_turn_completed call site #{i} extra={{}} "
                f"missing keys: {missing}"
            )


# ---------------------------------------------------------------------
# 5. PII defense -- raw payload.message body never crosses log boundary
# ---------------------------------------------------------------------

class TestNoMessageBodyInLogs:
    """A misconfigured CloudWatch log group is a PII exfil hazard.

    The audit log carries scope-bearing ids and lengths only -- the
    raw user-typed message body must NEVER appear in any
    `logger.info` or `logger.warning` `extra={...}` dict.
    """

    def test_no_logger_call_references_payload_message_body(self):
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Only check logger.* calls.
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
            ):
                continue
            call_src = ast.unparse(node)
            # Allow `len(payload.message)` -> `message_length`.
            # Forbid bare `payload.message` in any logger call.
            #
            # Strip out the `len(payload.message)` substring first
            # so the bare-attribute check after it is a clean grep.
            sanitized = call_src.replace("len(payload.message)", "")
            assert "payload.message" not in sanitized, (
                f"logger.{func.attr} site references payload.message "
                f"directly -- PII must not cross log boundary:\n{call_src}"
            )


# ---------------------------------------------------------------------
# 6. create_session_with_identity branch wired to client_claim
# ---------------------------------------------------------------------

class TestCreateSessionWithIdentityWiring:
    def test_branch_keyed_on_payload_client_claim(self):
        # The route MUST contain a branch that switches on
        # `payload.client_claim is not None` (or an equivalent test
        # that reaches the identity-bound path). We grep the AST for
        # the literal `if`-or-`elif` test substring because the
        # source-level idiom is the contract surface customers' SREs
        # will read when triaging.
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test_src = ast.unparse(node.test)
                if "payload.client_claim" in test_src and "None" in test_src:
                    found = True
                    break
        assert found, (
            "expected `if payload.client_claim is not None` branch in "
            "chat_widget.py to route into create_session_with_identity"
        )

    def test_create_session_with_identity_called_with_widget_constant(self):
        # The identity-bound branch MUST call
        # `session_service.create_session_with_identity(...)` with
        # `issuing_adapter=WIDGET_ISSUING_ADAPTER` -- never with a
        # value pulled from the request. We sniff the AST for that
        # exact keyword argument shape.
        src = WIDGET_ROUTE_PATH.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "create_session_with_identity"
            ):
                continue
            # Found the call. Now confirm the issuing_adapter kwarg
            # uses the module constant, not a payload-derived value.
            for kw in node.keywords:
                if kw.arg != "issuing_adapter":
                    continue
                if (
                    isinstance(kw.value, ast.Name)
                    and kw.value.id == "WIDGET_ISSUING_ADAPTER"
                ):
                    return  # success
                pytest.fail(
                    "issuing_adapter must be the module-level "
                    "WIDGET_ISSUING_ADAPTER constant, not "
                    f"{ast.unparse(kw.value)!r}"
                )
        pytest.fail(
            "create_session_with_identity(...) is never called from "
            "chat_widget.py -- the client_claim wiring is missing"
        )

    def test_legacy_anonymous_branch_still_present(self):
        # Backward compat: the route MUST still call the legacy
        # session_service.create_session(user_id=None, channel='widget')
        # when client_claim is absent. The Step 30b widget bundles
        # shipped before this PR rely on it.
        src = WIDGET_ROUTE_PATH.read_text()
        # AST grep for a create_session call with user_id=None.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "create_session"
            ):
                continue
            # Confirm user_id=None kwarg present.
            for kw in node.keywords:
                if (
                    kw.arg == "user_id"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is None
                ):
                    return  # success
        pytest.fail(
            "Legacy anonymous create_session(user_id=None, ...) call is "
            "missing from chat_widget.py -- legacy widget bundles "
            "would break"
        )


# ---------------------------------------------------------------------
# 7. SessionService.create_session_with_identity signature unchanged
# ---------------------------------------------------------------------

class TestSessionServiceSignaturePreserved:
    """Step 24.5c's contract test pins this method already; we
    re-pin the keyword names the widget route depends on so a future
    rename in session_service.py trips this file too, not just the
    Step 24.5c file.
    """

    def test_required_kwargs_for_widget_call_site(self):
        from app.services.session_service import SessionService
        sig = inspect.signature(
            SessionService.create_session_with_identity
        )
        for required in [
            "tenant_id",
            "domain_id",
            "agent_id",
            "channel",
            "claim_type",
            "claim_value",
            "issuing_adapter",
        ]:
            assert required in sig.parameters, (
                f"widget call site relies on `{required}` kwarg of "
                f"create_session_with_identity; missing from current "
                f"signature: {sig}"
            )
