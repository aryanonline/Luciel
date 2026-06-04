"""Arc 15 WU2 — chat_service composer wiring (AST shape test).

Confirms the composed PRESET + BUSINESS_CONTEXT stanzas reach the prompt
and the deprecated free-text system_prompt_additions (agent_prompt) layer
is gone.

RESCAN CORE(serving-path) update: ChatService is now a THIN adapter over
the LucielOrchestrator — it no longer calls ``build_system_prompt``
directly. It still COMPOSES the stanzas in ``_resolve_luciel_context``
and now threads them onto the ``RuntimeRequest`` (persona_preset_stanza /
persona_business_context_stanza), where the orchestrator's
ContextAssembler feeds them into ``build_system_prompt``. The composer
wiring + stanza composition is unchanged; only the hand-off point moved.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_SERVICE_PATH = REPO_ROOT / "app" / "services" / "chat_service.py"
CORE_PATH = REPO_ROOT / "app" / "persona" / "luciel_core.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_chat_service_imports_composer() -> None:
    src = _read(CHAT_SERVICE_PATH)
    assert "from app.persona.composer import" in src
    assert "compose_preset_stanza" in src
    assert "compose_business_context_stanza" in src


def test_both_call_sites_pass_composed_stanzas() -> None:
    # HYBRID rewiring: the composed stanzas are threaded onto the
    # RuntimeRequest (one shared ``_run_turn`` hand-off used by both
    # ``respond`` and ``respond_stream``), where the orchestrator's
    # ContextAssembler feeds them into build_system_prompt.
    src = _read(CHAT_SERVICE_PATH)
    assert "persona_preset_stanza=ctx.preset_stanza" in src
    assert (
        "persona_business_context_stanza=ctx.business_context_stanza" in src
    )


def test_call_sites_no_longer_thread_instance_prompt_as_agent_prompt() -> None:
    src = _read(CHAT_SERVICE_PATH)
    # The deprecated free-text layer must not be threaded any more.
    assert "agent_prompt=ctx.instance_prompt" not in src


def test_context_resolution_composes_stanzas() -> None:
    src = _read(CHAT_SERVICE_PATH)
    assert "ctx.preset_stanza = compose_preset_stanza(" in src
    assert "compose_business_context_stanza(" in src


def test_build_system_prompt_accepts_stanza_params() -> None:
    tree = ast.parse(_read(CORE_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_system_prompt":
            kwarg_names = {a.arg for a in node.args.kwonlyargs}
            assert "preset_stanza" in kwarg_names
            assert "business_context_stanza" in kwarg_names
            return
    raise AssertionError("build_system_prompt not found")
