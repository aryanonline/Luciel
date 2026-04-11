from uuid import uuid4
from app.runtime.contracts import RuntimeRequest, RuntimeResponse
from app.runtime.context_assembler import ContextAssembler

class LucielOrchestrator:
    def __init__(self) -> None:
        self.context = ContextAssembler()

    def run(self, req: RuntimeRequest) -> RuntimeResponse:
        _prompt = self.context.build_prompt(req)
        message = (
            "I understand. I will help clarify what matters most and guide the next step with precision. "
            f"For now, I have received your request: {req.message}"
        )
        return RuntimeResponse(
            message=message,
            trace_id=str(uuid4()),
            confidence=0.72,
            session_id=req.session_id,
            intent_summary="Initial user intent captured",
            escalation_flag=False,
        )
