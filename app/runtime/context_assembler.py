from app.persona.luciel_core import build_system_prompt
from app.runtime.contracts import RuntimeRequest


class ContextAssembler:
    """Arc 5 Path A: assembles the runtime prompt from the canonical
    ``build_system_prompt`` layer-builder. The legacy ``LUCIEL_IDENTITY``
    string constant was retired during the persona refactor; the
    replacement is ``build_system_prompt(...)`` which formats the
    canonical ``LUCIEL_SYSTEM_PROMPT`` template with ``assistant_name``
    and the optional tenant/domain/agent layers.
    """

    def build_prompt(self, req: RuntimeRequest) -> str:
        identity = build_system_prompt()
        return (
            f"{identity}\n\n"
            f"Tenant: {req.admin_id}\nDomain: {req.domain_id}\nChannel: {req.channel}\n"
            f"User message: {req.message}\n"
            "Respond as Luciel with clarity and restraint."
        )
