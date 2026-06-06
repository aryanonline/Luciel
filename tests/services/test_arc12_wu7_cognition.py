"""Arc 12 WU7 — cognition relocation + chat_service alignment sweep.

Three groups:

  1. **Behaviour-equivalence** for cognition. The same inputs that
     pre-WU7 fired escalate / save_memory / get_session_summary
     through the broker must still fire the equivalent cognition
     behaviour after WU7. Founder ruling 4b — behaviour-PRESERVING,
     not behaviour-expanding.

  2. **Removal** asserts (grep-style + AST + behavioural):
       - chat_service.py no longer threads ``domain_id`` /
         ``agent_id`` through ``respond()`` / ``respond_stream()`` /
         ``_resolve_luciel_context`` / prompt composition.
       - chat_service.py no longer substring-matches tool intents
         on the raw LLM reply.
       - The registry holds exactly the 8 §3.3.2 catalog tools and
         nothing cognition-shaped.
       - The 3 cognition tool classes no longer exist in
         ``app/tools/implementations/``.
       - The legacy ``instances.allowed_tools`` getattr fallback is
         gone.

  3. **chat_service shape**: the constructor accepts a
     ``cognition_service`` and no longer composes a Domain/Agent
     prompt chain.
"""
from __future__ import annotations

import ast
import os
import pathlib

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


_HERE = pathlib.Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[2]
_CHAT_SERVICE = _PROJECT_ROOT / "app" / "services" / "chat_service.py"
_CHAT_SERVICE_SRC = _CHAT_SERVICE.read_text()
_CHAT_SERVICE_TREE = ast.parse(_CHAT_SERVICE_SRC)


# ---------------------------------------------------------------------
# Group 1 — Cognition behaviour-equivalence
# ---------------------------------------------------------------------


def _make_cognition():
    from app.cognition import CognitionService

    return CognitionService()


def test_cognition_escalate_via_tool_call_envelope() -> None:
    """Pre-WU7 a raw_reply containing a TOOL_CALL escalation envelope
    fired the EscalateTool via the broker; chat_service then ran
    EscalationService.handle_escalation. Post-WU7 the cognition
    module owns intent detection AND fires the escalation
    side-effect itself."""

    captured: list[dict] = []

    class _StubEscalation:
        def handle_escalation(self, **kwargs):
            captured.append(kwargs)

    from app.cognition import CognitionService

    svc = CognitionService(escalation_service=_StubEscalation())
    raw = (
        'TOOL_CALL: {"tool": "escalate_to_human", '
        '"parameters": {"reason": "user demanded human"}}'
    )
    outcome = svc.process_turn(
        raw_reply=raw,
        messages=[],
        session_id="sess_1",
        user_id="user_1",
        admin_id="adm_1",
    )

    assert outcome.intent == "escalate_to_human"
    assert outcome.handled is True
    assert outcome.escalated is True
    assert outcome.escalation_reason == "user demanded human"
    assert captured == [{
        "session_id": "sess_1",
        "user_id": "user_1",
        "admin_id": "adm_1",
        "reason": "user demanded human",
    }], "EscalationService side effect must fire with the same kwargs"


def test_cognition_escalate_via_substring_fallback() -> None:
    """The pre-WU7 chat_service used a substring match
    (``'escalate_to_human' in raw_reply``). The cognition module
    preserves that fallback so any raw_reply that pre-WU7
    chat_service would have caught still fires escalation."""

    svc = _make_cognition()
    outcome = svc.process_turn(
        raw_reply="I'll escalate_to_human about this.",
        messages=[],
        session_id="s",
        user_id="u",
        admin_id="a",
    )
    assert outcome.intent == "escalate_to_human"
    assert outcome.escalated is True


def test_cognition_save_memory_returns_payload_not_persisted() -> None:
    """Pre-WU7 SaveMemoryTool returned ``{category, content}`` without
    writing the DB itself — chat_service ran the PolicyEngine
    memory-write gate and persisted via the repository. Cognition
    preserves the same split: it surfaces the payload and lets the
    chat path persist."""

    svc = _make_cognition()
    raw = (
        'TOOL_CALL: {"tool": "save_memory", "parameters": '
        '{"category": "preference", "content": "uses two monitors"}}'
    )
    outcome = svc.process_turn(
        raw_reply=raw,
        messages=[],
        session_id="s",
        user_id="u",
        admin_id="a",
    )
    assert outcome.intent == "save_memory"
    assert outcome.handled is True
    assert outcome.memory_payload == {
        "category": "preference",
        "content": "uses two monitors",
    }
    assert "Memory saved: [preference] uses two monitors" == outcome.output


def test_cognition_save_memory_rejects_missing_fields() -> None:
    """Pre-WU7 SaveMemoryTool returned success=False when category
    or content was missing. Cognition preserves that semantics so
    the chat path doesn't try to persist an empty memory row."""

    svc = _make_cognition()
    raw = 'TOOL_CALL: {"tool": "save_memory", "parameters": {}}'
    outcome = svc.process_turn(
        raw_reply=raw, messages=[], session_id="s",
        user_id="u", admin_id="a",
    )
    assert outcome.intent == "save_memory"
    assert outcome.memory_payload is None
    assert outcome.metadata.get("success") is False


def test_cognition_session_summary_formats_messages() -> None:
    """Pre-WU7 SessionSummaryTool formatted ``ROLE: preview`` per
    message with a 150-char preview cap. Cognition preserves that
    shape verbatim."""

    svc = _make_cognition()
    raw = 'TOOL_CALL: {"tool": "get_session_summary", "parameters": {}}'
    outcome = svc.process_turn(
        raw_reply=raw,
        messages=[
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ],
        session_id="s", user_id="u", admin_id="a",
    )
    assert outcome.intent == "get_session_summary"
    assert "Session summary (2 messages):" in outcome.output
    assert "USER: Hi" in outcome.output
    assert "ASSISTANT: Hello!" in outcome.output


def test_cognition_session_summary_empty_history() -> None:
    svc = _make_cognition()
    outcome = svc.process_turn(
        raw_reply='TOOL_CALL: {"tool": "get_session_summary"}',
        messages=[],
        session_id="s", user_id="u", admin_id="a",
    )
    assert outcome.output == "No messages in this session yet."


def test_cognition_no_intent_when_raw_reply_is_plain() -> None:
    """A plain LLM reply with no cognition intent must produce no
    outcome — the chat path treats this as a normal answer turn."""

    svc = _make_cognition()
    outcome = svc.process_turn(
        raw_reply="Sure, the property at 123 Main St is available.",
        messages=[],
        session_id="s", user_id="u", admin_id="a",
    )
    assert outcome.intent is None
    assert outcome.handled is False
    assert outcome.escalated is False


def test_cognition_is_not_tier_gated() -> None:
    """§3.4: cognition is always-on, every tier. The CognitionService
    constructor must not accept any tier / authorisation parameter;
    process_turn must not consult any registry / tier table."""

    import inspect

    from app.cognition import CognitionService

    init_sig = inspect.signature(CognitionService.__init__)
    init_params = set(init_sig.parameters)
    forbidden = {"tier", "tiers", "registry", "broker", "authorizer"}
    assert not (init_params & forbidden), (
        f"CognitionService.__init__ must be tier-/registry-/broker-"
        f"free. Got params {init_params!r}."
    )

    process_sig = inspect.signature(CognitionService.process_turn)
    process_params = set(process_sig.parameters)
    assert not (process_params & forbidden), (
        f"CognitionService.process_turn must be tier-/registry-/broker-"
        f"free. Got params {process_params!r}."
    )


# ---------------------------------------------------------------------
# Group 2 — Removal asserts
# ---------------------------------------------------------------------


def test_chat_service_does_not_read_session_domain_or_agent() -> None:
    """Founder ruling 5: the Domain/Agent threading is superseded v1
    scaffold. The chat path must not READ ``session.domain_id`` or
    ``session.agent_id`` — those attributes survive on the session
    model out of scope here, but the chat service must not consume
    them. (Downstream services that still accept a ``domain_id`` /
    ``agent_id`` kwarg get ``None`` — collapsing the threading at
    this layer is what §3.7.2 mandates.)"""

    forbidden_reads = (
        "session.domain_id",
        "session.agent_id",
        "getattr(session, \"agent_id\"",
        "getattr(session, 'agent_id'",
        "getattr(session, \"domain_id\"",
        "getattr(session, 'domain_id'",
    )
    for token in forbidden_reads:
        assert token not in _CHAT_SERVICE_SRC, (
            f"chat_service.py must not read {token!r} after WU7 "
            f"(founder ruling 5 — collapse to Admin→Instance, §3.7.2)."
        )


def test_chat_service_does_not_take_domain_or_agent_locals() -> None:
    """No local variable in any method named ``domain_id`` or
    ``agent_id`` after WU7. Pinned via AST so we catch both
    re-assignment and parameter-style introductions."""

    for node in ast.walk(_CHAT_SERVICE_TREE):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id in ("domain_id", "agent_id")
                ):
                    raise AssertionError(
                        f"chat_service.py defines a local "
                        f"{target.id!r} — must be removed in WU7."
                    )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.arg in ("domain_id", "agent_id"):
                    raise AssertionError(
                        f"chat_service.{node.name} takes {arg.arg!r} "
                        f"as a parameter — must be removed in WU7."
                    )


def test_chat_service_does_not_substring_match_tool_intents() -> None:
    """Founder ruling: substring tool-detection (``'<intent>' in
    raw_reply``) is removed as the dispatch mechanism. Cognition
    intent recognition lives in ``app.cognition``; the chat path
    no longer branches on the raw reply text directly.

    Pinned via AST: any ``Compare`` node of shape ``<str> in
    <Name 'raw_reply'>`` where the string equals a cognition
    intent name is a regression.
    """

    cognition_intents = {
        "escalate_to_human", "save_memory", "get_session_summary",
    }
    for node in ast.walk(_CHAT_SERVICE_TREE):
        if not isinstance(node, ast.Compare):
            continue
        # ``X in raw_reply`` produces Compare(left=<str>, ops=[In()],
        # comparators=[Name('raw_reply')]).
        if not node.ops or not isinstance(node.ops[0], ast.In):
            continue
        comparators = node.comparators
        if not comparators or not isinstance(comparators[0], ast.Name):
            continue
        if comparators[0].id != "raw_reply":
            continue
        left = node.left
        if isinstance(left, ast.Constant) and left.value in cognition_intents:
            raise AssertionError(
                f"chat_service.py substring-matches {left.value!r} "
                f"against raw_reply — that dispatch path moved to "
                f"app.cognition in WU7."
            )


def test_chat_service_does_not_call_broker_parse_and_execute() -> None:
    """Pre-WU7 the chat path called ``tool_broker.parse_and_execute``
    on every turn to dispatch the cognition tools. With cognition
    relocated, the chat path no longer dispatches tools through
    the broker — Arc 12 default-deny means no instance has a tool
    authorised yet, and Arc 14 owns the agentic loop that would
    drive multi-step tool execution.

    Pinned via AST so docstring mentions of the removed call are
    allowed.
    """

    for node in ast.walk(_CHAT_SERVICE_TREE):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "parse_and_execute"
        ):
            raise AssertionError(
                "chat_service calls .parse_and_execute — that "
                "dispatch path was retired in WU7."
            )


def test_chat_service_does_not_use_legacy_allowed_tools_getattr() -> None:
    """Pre-WU7 the resolver did ``getattr(instance, 'allowed_tools',
    None)`` as a legacy bypass; this was superseded by the WU2
    ``instance_tool_authorizations`` table. The fallback must be
    gone.

    Pinned via AST: any string literal ``'allowed_tools'`` in a
    Call / Attribute / Subscript inside chat_service.py is a
    regression. (Mentions in module docstrings are allowed — they
    document the removal.)
    """

    # Walk every executable code node — Call args, Attribute names,
    # Subscript keys, comparisons. We allow string occurrences inside
    # Module/Function/Class docstrings (the first stmt of a
    # body, if it's an Expr(Constant(str))).

    # Build the set of docstring node ids to exclude.
    docstring_ids: set[int] = set()
    for owner in ast.walk(_CHAT_SERVICE_TREE):
        if isinstance(owner, (
            ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
        )):
            body = getattr(owner, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant,
            ) and isinstance(body[0].value.value, str):
                docstring_ids.add(id(body[0].value))

    for node in ast.walk(_CHAT_SERVICE_TREE):
        # Attribute access: instance.allowed_tools
        if isinstance(node, ast.Attribute) and node.attr == "allowed_tools":
            raise AssertionError(
                "chat_service.py reads .allowed_tools — must be "
                "removed in WU7."
            )
        # Constant string 'allowed_tools' outside docstrings.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == "allowed_tools"
            and id(node) not in docstring_ids
        ):
            raise AssertionError(
                "chat_service.py contains the literal string "
                "'allowed_tools' outside a docstring — likely a "
                "leftover getattr fallback. Remove in WU7."
            )


def test_chat_service_does_not_define_compose_system_prompt_additions() -> None:
    """The ``_compose_system_prompt_additions`` helper merged the
    tenant/domain/agent/instance layers. With three of those four
    layers gone, the helper has no work left and must be removed."""

    for node in ast.walk(_CHAT_SERVICE_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "_compose_system_prompt_additions", (
                "ChatService._compose_system_prompt_additions must be "
                "removed in WU7 — the three-layer prompt scaffold is "
                "collapsed to a single Admin→Instance boundary."
            )


def test_chat_service_luciel_context_is_admin_instance_only() -> None:
    """The ``LucielContext`` dataclass must no longer carry
    tenant/domain/agent prompt fields. Single Admin→Instance
    boundary per §3.7.2."""

    cls = None
    for node in ast.walk(_CHAT_SERVICE_TREE):
        if isinstance(node, ast.ClassDef) and node.name == "LucielContext":
            cls = node
            break
    assert cls is not None, "LucielContext must still exist in chat_service"

    field_names: set[str] = set()
    for body_node in cls.body:
        if isinstance(body_node, ast.AnnAssign) and isinstance(
            body_node.target, ast.Name,
        ):
            field_names.add(body_node.target.id)

    # Arc 15 doctrine cleanup — the free-text prompt fields are GONE.
    # ``instance_prompt`` was the LucielContext carrier for the dead
    # ``system_prompt_additions`` layer (Vision §3.5 / Architecture
    # §3.5.1 "never raw prompt authoring"); it is removed alongside the
    # superseded tenant/domain/agent layers.
    forbidden = {
        "tenant_prompt",
        "domain_prompt",
        "agent_prompt",
        "instance_prompt",
    }
    assert not (field_names & forbidden), (
        f"LucielContext must not carry any free-text prompt field. "
        f"Got {field_names & forbidden!r}."
    )
    # The Admin→Instance boundary contributes via the platform-composed
    # stanzas (§3.5.1), not a raw prompt string.
    assert "preset_stanza" in field_names
    assert "business_context_stanza" in field_names


def test_registry_has_exactly_the_seven_catalog_tools() -> None:
    """After WU7 + Unit 1 excision, the tool registry holds exactly the 7
    configurable tools (call_sibling_luciel removed — multi-Luciel deferred)."""

    from app.tools.registry import ToolRegistry

    ids = {t.tool_id for t in ToolRegistry().list_tools()}
    expected = {
        "book_appointment",
        "send_email",
        "send_sms",
        "lookup_record",
        "schedule_callback",
        "push_to_crm",
        # call_sibling_luciel removed (Unit 1 excision — multi-Luciel deferred).
        "bring_your_own_webhook",
    }
    assert ids == expected, (
        f"Registry mismatch — expected exactly the 7 catalog tools. "
        f"Got {ids!r}; expected {expected!r}"
    )
    assert "escalate_to_human" not in ids
    assert "save_memory" not in ids
    assert "get_session_summary" not in ids


def test_cognition_tool_classes_no_longer_exist() -> None:
    """The three cognition tool implementation files were deleted
    in WU7. Importing them must fail; the files must not be on
    disk."""

    for module_name in (
        "app.tools.implementations.escalate_tool",
        "app.tools.implementations.save_memory_tool",
        "app.tools.implementations.session_summary_tool",
    ):
        with pytest.raises(ImportError):
            __import__(module_name)

    for rel in (
        "app/tools/implementations/escalate_tool.py",
        "app/tools/implementations/save_memory_tool.py",
        "app/tools/implementations/session_summary_tool.py",
    ):
        assert not (_PROJECT_ROOT / rel).exists(), (
            f"{rel} must not exist after WU7."
        )


# ---------------------------------------------------------------------
# Group 3 — ChatService shape
# ---------------------------------------------------------------------


def test_chat_service_constructor_accepts_cognition_service() -> None:
    """ChatService gains a ``cognition_service`` parameter so the
    always-on cognition module is injected by the dependency
    graph. Default is constructed lazily so existing tests that
    skip the parameter still work."""

    import inspect

    from app.services.chat_service import ChatService

    sig = inspect.signature(ChatService.__init__)
    params = sig.parameters
    assert "cognition_service" in params, (
        "ChatService.__init__ must accept ``cognition_service``."
    )
    # Default is None (lazy default) so existing tests/wiring keep
    # working without code changes outside deps.py.
    assert params["cognition_service"].default is None


def test_chat_service_respond_signature_does_not_thread_domain_or_agent() -> None:
    """``respond`` / ``respond_stream`` must no longer accept
    ``domain_id`` / ``agent_id`` parameters."""

    import inspect

    from app.services.chat_service import ChatService

    for fn_name in ("respond", "respond_stream"):
        fn = getattr(ChatService, fn_name)
        params = set(inspect.signature(fn).parameters)
        assert "domain_id" not in params, (
            f"ChatService.{fn_name} must not accept domain_id "
            f"post-WU7."
        )
        assert "agent_id" not in params, (
            f"ChatService.{fn_name} must not accept agent_id "
            f"post-WU7."
        )


def test_chat_service_resolve_context_is_admin_instance_only() -> None:
    """``_resolve_luciel_context`` must take only
    ``luciel_instance_id`` and ``admin_id`` after WU7."""

    import inspect

    from app.services.chat_service import ChatService

    sig = inspect.signature(ChatService._resolve_luciel_context)
    params = set(sig.parameters) - {"self"}
    assert params == {"luciel_instance_id", "admin_id"}, (
        f"_resolve_luciel_context signature drifted; got {params!r}, "
        f"expected exactly luciel_instance_id + admin_id."
    )
