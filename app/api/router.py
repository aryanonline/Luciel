from fastapi import APIRouter

from app.api.v1 import admin, chat, health, sessions
from app.api.v1 import retention
from app.api.v1 import consent  # ADD THIS
from app.api.v1 import verification  # Step 26b
from app.api.v1 import users  # Step 24.5b
from app.api.v1 import audit_log  # Step 28 Phase 2 - Commit 2
from app.api.v1 import admin_forensics  # Step 29 Commit C.1
from app.api.v1 import chat_widget  # Step 30b commit (c)
from app.api.v1 import dashboard  # Step 31 sub-branch 3
from app.api.v1 import billing  # Step 30a
from app.api.v1 import auth  # Step 30a.3 -- password auth, mandatory at signup
from app.api.v1 import ses_events  # Arc 8 WU-6 Phase C -- SES feedback / suppression sink
from app.api.v1 import admin_knowledge  # Arc 11 Step 7
from app.api.v1 import admin_tools  # Arc 12 WU2b
from app.api.v1 import twilio_webhook  # Arc 13 D4 -- inbound SMS webhook
from app.api.v1 import admin_channels  # Arc 13 D5 -- channel-config admin API
from app.api.v1 import admin_personality  # Arc 15 WU3 -- personality config API
from app.api.v1 import admin_escalation  # Arc 15 WU3 -- escalation-contact API
from app.api.v1 import admin_connections  # Arc 15 WU4 -- connection-config API
from app.api.v1 import admin_usage  # Arc 18 -- conversation-budget usage API
from app.api.v1 import admin_handoff  # Rescan Tier-C -- human-controlled session handoff
from app.api.v1 import admin_escalation_ack  # Unit 9 -- escalation ack endpoint

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(chat.router)
api_router.include_router(sessions.router, prefix="/sessions")
api_router.include_router(admin.router)
api_router.include_router(retention.router)
api_router.include_router(consent.router)  # ADD THIS
api_router.include_router(verification.router)  # Step 26b.2
api_router.include_router(users.router)  # Step 24.5b
api_router.include_router(audit_log.router)  # Step 28 Phase 2 - Commit 2
api_router.include_router(admin_forensics.router)  # Step 29 Commit C.1
api_router.include_router(chat_widget.router)  # Step 30b commit (c)
api_router.include_router(dashboard.router)  # Step 31 sub-branch 3
api_router.include_router(billing.router)  # Step 30a
api_router.include_router(auth.router)  # Step 30a.3 -- password auth
api_router.include_router(ses_events.router)  # Arc 8 WU-6 Phase C
api_router.include_router(admin_knowledge.router)  # Arc 11 Step 7
api_router.include_router(admin_knowledge.internal_router)  # Arc 11 Step 7
api_router.include_router(admin_tools.router)  # Arc 12 WU2b
api_router.include_router(twilio_webhook.router)  # Arc 13 D4 -- inbound SMS
api_router.include_router(admin_channels.router)  # Arc 13 D5 -- channel config
api_router.include_router(admin_personality.router)  # Arc 15 WU3 -- personality
api_router.include_router(admin_escalation.router)  # Arc 15 WU3 -- escalation
api_router.include_router(admin_connections.router)  # Arc 15 WU4 -- connections
api_router.include_router(admin_usage.router)  # Arc 18 -- budget usage API
api_router.include_router(admin_handoff.router)  # Rescan Tier-C -- human handoff
api_router.include_router(admin_escalation_ack.router)  # Unit 9 -- escalation ack