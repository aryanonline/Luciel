from app.models.api_key import ApiKey
from app.models.knowledge import KnowledgeChunk  # noqa: F401
from app.models.knowledge_source import KnowledgeSource  # noqa: F401  (Arc 11 Step 1)
from app.models.memory import MemoryItem
from app.models.message import MessageModel
from app.models.session import SessionModel
from app.models.trace import Trace
from app.models.retention import RetentionPolicy, DeletionLog
from app.models.user_consent import UserConsent
from app.models.admin_audit_log import AdminAuditLog  # noqa: F401  (Step 24.5 — File 6.5a)
from app.models.user import User  # noqa: F401  (Step 24.5b)
from app.models.conversation import Conversation  # noqa: F401  (Step 24.5c)
from app.models.identity_claim import IdentityClaim, ClaimType  # noqa: F401  (Step 24.5c)
from app.models.subscription import Subscription  # noqa: F401  (Step 30a)
from app.models.email_send_event import (  # noqa: F401  (Arc 8 WU-6)
    EmailSendEvent,
    SES_EVENT_TYPES,
    SES_EVENT_TYPES_TRIGGER_SUPPRESSION,
    SES_EVENT_BOUNCE,
    SES_EVENT_COMPLAINT,
)
from app.models.email_suppression import (  # noqa: F401  (Arc 8 WU-6)
    EmailSuppression,
    SUPPRESSION_REASONS,
    SUPPRESSION_REASON_HARD_BOUNCE,
    SUPPRESSION_REASON_COMPLAINT,
    SUPPRESSION_REASON_MANUAL_BLOCK,
)
# Arc 5 B1 — V2 Admin → Instance shape (aggressive-cleanup amendment).
from app.models.admin import (  # noqa: F401
    Admin,
    AdminConfig,
    ALLOWED_TIERS_V2,
    ALLOWED_TIER_SOURCES,
    TIER_FREE,
    TIER_PRO,
)
# Arc 6 A — admin_widget_domains allowlist (Free/Pro widget domain control).
from app.models.admin_widget_domain import AdminWidgetDomain  # noqa: F401
from app.models.instance import Instance  # noqa: F401
# Arc 13 — channel inbound-addressing → Instance routing map.
from app.models.channel_route import ChannelRoute  # noqa: F401
# Arc 12 WU2 — per-instance tool authorisation (default-deny broker gate).
from app.models.instance_tool_authorization import (  # noqa: F401
    InstanceToolAuthorization,
)
# Arc 15 WU4 / Arc 17 — Connections Layer (§3.8) models, relocated to
# app.connections (Unit 12 §8). Bare ``import`` (not ``from … import``)
# registers the ORM classes on Base.metadata via module execution while
# deferring attribute access, so a cold import of a connection submodule
# (which imports app.models.base, triggering this __init__) cannot
# deadlock on the app.models ⇄ app.connections cycle.
import app.connections.instance_connection  # noqa: F401
import app.connections.secret_cleanup_outbox  # noqa: F401
# Arc 14 U2 — §3.4.5 escalation judgment event store.
from app.models.escalation_event import (  # noqa: F401
    EscalationEvent,
    SIGNAL_EXPLICIT_HUMAN_REQUEST,
    SIGNAL_STRONG_NEGATIVE_SENTIMENT,
    SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
    SIGNAL_HIGH_VALUE_LEAD,
    ALLOWED_SIGNALS,
    GATE_INTAKE,
    GATE_OUTCOME,
    ALLOWED_GATES,
    SIGNAL_BUDGET_EXHAUSTED,
)
# Arc 18 — §3.4.1b conversation-overage durable billing ledger.
from app.models.conversation_overage_ledger import (  # noqa: F401
    ConversationOverageLedger,
)
# Unit 13g — §3.4.1b+§4.5 budget counter write-through (Postgres authoritative).
from app.models.conversation_budget_counter import (  # noqa: F401
    ConversationBudgetCounter,
    ConversationCountedSession,
)
# Arc 14 U4 — §3.4.4 lead capture + §3.4.7 summarization (cognition).
from app.models.lead import Lead  # noqa: F401
# Unit 13e — §3.4.10 persisted session-summary store (cross-session memory).
from app.models.session_summary import SessionSummary  # noqa: F401
# Arc 12 WU6 — BYO webhook config + general-purpose tool execution log.
from app.models.byo_webhook_endpoint import ByoWebhookEndpoint  # noqa: F401
from app.models.tool_execution_log import (  # noqa: F401
    ToolExecutionLog,
    ERROR_CLASS_TRANSPORT,
    ERROR_CLASS_TIMEOUT,
    ERROR_CLASS_SCHEMA_INPUT,
    ERROR_CLASS_SCHEMA_OUTPUT,
    ERROR_CLASS_CIRCUIT_OPEN,
    ERROR_CLASS_EGRESS_DENIED,
    ERROR_CLASS_HTTP_ERROR,
    ERROR_CLASS_OTHER,
    CB_STATE_CLOSED,
    CB_STATE_HALF_OPEN,
    CB_STATE_OPEN,
)
# Arc 5 Path A Commit C2: app/models/aliases.py was deleted along with the
# Tenant / TenantConfig / LucielInstance / DomainConfig / Agent transitional
# re-exports. Importers must reference Admin / AdminConfig / Instance
# directly (see app/models/admin.py and app/models/instance.py).

__all__ = [
    "ApiKey",
    "DeletionLog",
    "SessionModel",
    "UserConsent",
    "MessageModel",
    "MemoryItem",
    "RetentionPolicy",
    "Trace",
    "KnowledgeChunk",
    "KnowledgeSource",
    "AdminAuditLog",
    "User",
    "Conversation",
    "IdentityClaim",
    "ClaimType",
    "Subscription",
    # Arc 8 WU-6 -- SES feedback / suppression cohort
    "EmailSendEvent",
    "SES_EVENT_TYPES",
    "SES_EVENT_TYPES_TRIGGER_SUPPRESSION",
    "SES_EVENT_BOUNCE",
    "SES_EVENT_COMPLAINT",
    "EmailSuppression",
    "SUPPRESSION_REASONS",
    "SUPPRESSION_REASON_HARD_BOUNCE",
    "SUPPRESSION_REASON_COMPLAINT",
    "SUPPRESSION_REASON_MANUAL_BLOCK",
    # Arc 5 B1 -- V2 Admin / Instance.
    "Admin",
    "AdminConfig",
    "Instance",
    "ALLOWED_TIERS_V2",
    "ALLOWED_TIER_SOURCES",
    "TIER_FREE",
    "TIER_PRO",
    # Arc 6 A -- widget domain allowlist.
    "AdminWidgetDomain",
    # Arc 13 -- channel routing map.
    "ChannelRoute",
    # Arc 12 WU2 -- per-instance tool authorisation.
    "InstanceToolAuthorization",
    # Arc 15 WU4 -- per-instance external-system connections.
    "InstanceConnection",
    "CONNECTION_TYPES",
    "CONNECTION_STATUSES",
    # Arc 17 -- lifecycle secret-cleanup outbox.
    "SecretCleanupOutbox",
    "OUTBOX_STATUSES",
    # Arc 14 U2 -- escalation judgment event store.
    "EscalationEvent",
    "SIGNAL_EXPLICIT_HUMAN_REQUEST",
    "SIGNAL_STRONG_NEGATIVE_SENTIMENT",
    "SIGNAL_CANNOT_CONFIDENTLY_ANSWER",
    "SIGNAL_HIGH_VALUE_LEAD",
    "ALLOWED_SIGNALS",
    "GATE_INTAKE",
    "GATE_OUTCOME",
    "ALLOWED_GATES",
    "SIGNAL_BUDGET_EXHAUSTED",
    # Arc 18 -- §3.4.1b conversation-overage durable billing ledger.
    "ConversationOverageLedger",
    # Unit 13g -- §3.4.1b+§4.5 budget counter write-through.
    "ConversationBudgetCounter",
    "ConversationCountedSession",
    # Arc 14 U4 -- §3.4.4 lead capture + §3.4.7 summarization.
    "Lead",
    # Unit 13e -- §3.4.10 persisted session-summary store.
    "SessionSummary",
    # Arc 12 WU6 -- BYO webhook config + tool execution log.
    "ByoWebhookEndpoint",
    "ToolExecutionLog",
    "ERROR_CLASS_TRANSPORT",
    "ERROR_CLASS_TIMEOUT",
    "ERROR_CLASS_SCHEMA_INPUT",
    "ERROR_CLASS_SCHEMA_OUTPUT",
    "ERROR_CLASS_CIRCUIT_OPEN",
    "ERROR_CLASS_EGRESS_DENIED",
    "ERROR_CLASS_HTTP_ERROR",
    "ERROR_CLASS_OTHER",
    "CB_STATE_CLOSED",
    "CB_STATE_HALF_OPEN",
    "CB_STATE_OPEN",
    # Arc 12b -- permission-based custom roles.
]
