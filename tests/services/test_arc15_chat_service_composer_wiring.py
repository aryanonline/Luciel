"""Arc 15 WU2 — chat_service composer wiring (AST shape test).

Confirms both build_system_prompt call sites pass the composed PRESET +
BUSINESS_CONTEXT stanzas and no longer thread the deprecated free-text
system_prompt_additions (agent_prompt) layer.
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
    src = _read(CHAT_SERVICE_PATH)
    assert src.count("preset_stanza=ctx.preset_stanza") == 2
    assert src.count("business_context_stanza=ctx.business_context_stanza") == 2


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
