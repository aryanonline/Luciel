from app.models.agent_config import AgentConfig
from app.models.api_key import ApiKey
from app.models.knowledge import KnowledgeEmbedding
from app.models.memory import MemoryItem
from app.models.message import MessageModel
from app.models.session import SessionModel
from app.models.trace import Trace
from app.models.retention import RetentionPolicy, DeletionLog
from app.models.user_consent import UserConsent
from app.models.admin_audit_log import AdminAuditLog  # noqa: F401  (Step 24.5 — File 6.5a)
from app.models.user import User  # noqa: F401  (Step 24.5b)
from app.models.scope_assignment import ScopeAssignment, EndReason  # noqa: F401  (Step 24.5b)
from app.models.conversation import Conversation  # noqa: F401  (Step 24.5c)
from app.models.identity_claim import IdentityClaim, ClaimType  # noqa: F401  (Step 24.5c)
from app.models.subscription import Subscription  # noqa: F401  (Step 30a)
from app.models.user_invite import UserInvite, InviteStatus  # noqa: F401  (Step 30a.4)
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
    TIER_ENTERPRISE,
)
from app.models.instance import Instance  # noqa: F401
# Transitional shim — re-exports legacy names that resolve to Admin / Instance.
# Deleted at B6 once all call-sites finish their rename.
from app.models.aliases import (  # noqa: F401
    Tenant,
    TenantConfig,
    LucielInstance,
    DomainConfig,
    Agent,
)

__all__ = [
    "AgentConfig",
    "ApiKey",
    "DeletionLog",
    "SessionModel",
    "UserConsent",
    "MessageModel",
    "MemoryItem",
    "RetentionPolicy",
    "Trace",
    "KnowledgeEmbedding",
    "AdminAuditLog",
    "User",
    "ScopeAssignment",
    "EndReason",
    "Conversation",
    "IdentityClaim",
    "ClaimType",
    "Subscription",
    "UserInvite",
    "InviteStatus",
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
    "TIER_ENTERPRISE",
    # Transitional aliases (deleted at B6).
    "Tenant",
    "TenantConfig",
    "LucielInstance",
    "DomainConfig",
    "Agent",
]
