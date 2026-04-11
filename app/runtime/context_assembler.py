from app.persona.luciel_core import LUCIEL_IDENTITY
from app.runtime.contracts import RuntimeRequest

class ContextAssembler:
    def build_prompt(self, req: RuntimeRequest) -> str:
        return (
            f"{LUCIEL_IDENTITY}\n\n"
            f"Tenant: {req.tenant_id}\nDomain: {req.domain_id}\nChannel: {req.channel}\n"
            f"User message: {req.message}\n"
            "Respond as Luciel with clarity and restraint."
        )
