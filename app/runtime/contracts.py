from dataclasses import dataclass

@dataclass
class RuntimeRequest:
    message: str
    session_id: str
    user_id: str | None
    tenant_id: str
    domain_id: str
    channel: str

@dataclass
class RuntimeResponse:
    message: str
    trace_id: str
    confidence: float
    session_id: str
    intent_summary: str | None = None
    escalation_flag: bool = False
