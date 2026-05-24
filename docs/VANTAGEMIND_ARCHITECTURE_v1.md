# VantageMind — Architecture v1 (Draft for Founder Review)

**Status:** DRAFT — Founder review pending
**Authored:** 2026-05-24
**Anchors to:** `docs/VANTAGEMIND_VISION_v1_FINAL.md` (the canonical product vision)
**Purpose:** Translate the founder-approved vision into a concrete technical architecture — what we build, in what order, with what components, and with which security/isolation properties at each layer. This document is the **sole architecture source of truth** going forward. The legacy `docs/ARCHITECTURE.md` (813 lines, pre-vision) is preserved in git history but no longer the active anchor.

---

## 0. How to Read This Document

This document is the **bridge between vision and code**. It does three things:

1. **Maps each vision pillar to concrete subsystems** — channels, tools, knowledge, runtime, lifecycle, isolation
2. **Marks every subsystem with a status flag** so we know what we are building vs. extending
3. **Names the arc that owns each subsystem** so the roadmap is traceable

**Status flags used throughout:**

| Flag | Meaning |
|---|---|
| ✅ **LIVE** | Shipped in prod today, customer-visible |
| 🟨 **SCAFFOLDED** | Code exists in the repo but is not customer-visible or not wired end-to-end |
| 🔵 **DESIGNED** | This document is the design; implementation is queued in a named arc |
| ⚪ **PROPOSED** | Recommendation in this doc; founder review pending |

**Founder review action items** appear in `> 📝 REVIEW:` callouts. Mark them up however you want.

---

## 1. The Two-Plane Architecture

VantageMind is built on a **two-plane** mental model. Every component lives in exactly one plane.

### 1.1 Control Plane — "The Admin's Cockpit"

What the **business owner** sees and configures.

**Responsibilities:**
- Admin authentication + team management
- Subscription + billing
- Instance creation, configuration, deactivation
- Knowledge ingestion (upload, crawl, paste)
- Tool selection
- Channel selection
- Escalation contact setup
- Personality picklist configuration
- Dashboard analytics + audit log viewing

**User-facing interfaces:**
- Web dashboard (Luciel-Website frontend)
- Admin API (`/api/v1/admin/*`, `/api/v1/billing/*`, `/api/v1/dashboard/*`)

**Status today:** ✅ **LIVE** — authentication, billing, instance CRUD, dashboard. 🟨 **SCAFFOLDED** — most other surfaces (no UI for tools, channels, KB, escalation, deactivation).

### 1.2 Data Plane — "The Luciel's Workspace"

What the **end customer** interacts with and what the Luciel runtime executes against.

**Responsibilities:**
- Inbound message reception (across channels)
- Conversation + session management
- Knowledge retrieval (vector / graph)
- Tool execution
- LLM orchestration (the agentic loop)
- Channel arbitration + outbound delivery
- Audit trail emission

**User-facing interfaces:**
- Chat widget (embed-key authenticated)
- Email inbound webhook (Arc 13)
- SMS inbound webhook (Arc 13)
- Voice inbound webhook (Arc 14b)
- Widget API (`/api/v1/chat-widget/*`)

**Status today:** ✅ **LIVE** — widget channel only. 🔵 **DESIGNED** — all other channels.

### 1.3 Why the Two-Plane Split Matters

This split is the **architectural enforcement** of the four isolation walls in Vision §5:

- The Control Plane authenticates as an **Admin** (or team member) → reads/writes only that Admin's configuration
- The Data Plane authenticates as an **embed key** (or channel webhook signed for an Admin) → reads/writes only that Admin's customer-facing data
- **Neither plane can cross to the other tenant** because the authentication subject differs at every request

> 📝 REVIEW: Does the two-plane split feel right? Some platforms ship a single-plane API with row-level filtering — that's simpler but less defensible at audit time.

---

## 2. The Component Map (Target State)

This is the full architecture VantageMind will run on by the end of Arc 16. The system is large enough that a single combined diagram becomes unreadable, so it is split into **five layered views** — each on its own page. Read them in order; each view zooms into a slice of the same underlying system.

**The five views:**

1. **§2.1 Whole-System Overview** — the four layers and how they relate
2. **§2.2 Control Plane Detail** — what admins configure
3. **§2.3 Data Plane Detail** — the Luciel runtime
4. **§2.4 Persistence Layer Detail** — what is stored where
5. **§2.5 Observability + Audit Layer Detail** — how we see what happened

<div style="page-break-after: always;"></div>

### 2.1 Whole-System Overview

A bird's-eye view of how the four layers connect. Details for each layer follow in §2.2 – §2.5.

```
        ┌───────────────────────────────────────────────────────┐
        │                    CONTROL PLANE                       │
        │                  (Admin's Cockpit)                     │
        │      Web Dashboard │ Admin API │ Billing │ Config       │
        └────────────────────────┬──────────────────────────────┘
                                 │ writes
                                 ▼
        ┌───────────────────────────────────────────────────────┐
        │                 PERSISTENCE LAYER                      │
        │   PostgreSQL + pgvector │ Redis │ S3 │ Graph DB         │
        └────────────────────────┬──────────────────────────────┘
                                 ▲ reads
                                 │
        ┌────────────────────────┴──────────────────────────────┐
        │                     DATA PLANE                         │
        │                 (Luciel's Workspace)                   │
        │  Channels → Orchestrator → Persona / KB / Tools / LLM   │
        └────────────────────────┬──────────────────────────────┘
                                 │ emits
                                 ▼
        ┌───────────────────────────────────────────────────────┐
        │             OBSERVABILITY + AUDIT LAYER                │
        │   admin_audit_log │ trace │ CloudWatch │ smoke probe    │
        └───────────────────────────────────────────────────────┘
```

**Reading guide:**
- **Down-arrows** = writes (config flows from Control Plane into Persistence; events flow from Data Plane into Observability)
- **Up-arrow** = reads (Data Plane reads its configuration from Persistence at runtime)
- **No direct arrow** between Control Plane and Data Plane — they communicate only through Persistence. This is the architectural enforcement of the isolation walls in Vision §5.

<div style="page-break-after: always;"></div>

### 2.2 Control Plane Detail

What the **business owner** sees and configures.

```
┌──────────────────────────────────────────────────────────────────────┐
│                          CONTROL PLANE                                │
│                                                                       │
│   ┌────────────────────┐                                             │
│   │   Web Dashboard    │ ◄── Admin user (browser)                    │
│   │   (Luciel-Website) │                                             │
│   │   ✅ LIVE (partial)│                                             │
│   └─────────┬──────────┘                                             │
│             │ HTTPS                                                   │
│             ▼                                                         │
│   ┌────────────────────┐         ┌────────────────────┐              │
│   │     Admin API      │◄───────►│  Billing Service    │             │
│   │  /api/v1/admin/*   │         │  (Stripe Live)      │             │
│   │  ✅ LIVE (partial) │         │  ✅ LIVE            │             │
│   └─────────┬──────────┘         └─────────┬──────────┘             │
│             │                              │                          │
│             └──────────────┬───────────────┘                          │
│                            ▼                                          │
│   ┌──────────────────────────────────────────────────────────┐       │
│   │              Configuration Service Layer                  │       │
│   │                                                            │       │
│   │   • Instance config (the 5 pillars)                       │       │
│   │   • Knowledge ingestion API          🔵 Arc 11             │       │
│   │   • Tool selection API               🔵 Arc 12             │       │
│   │   • Channel selection API            🔵 Arc 13             │       │
│   │   • Escalation contact API           🔵 Arc 10             │       │
│   │   • Personality picklist API         🔵 Arc 11             │       │
│   │   • Instance deactivation API        🔵 Arc 10             │       │
│   │   • Account closure API              🔵 Arc 10             │       │
│   └──────────────────────────┬───────────────────────────────┘       │
│                              │ writes config rows                     │
└──────────────────────────────┼───────────────────────────────────────┘
                               ▼  (to Persistence — §2.4)
```

**Key principle:** Every API in this plane authenticates as an **Admin** (or a team member acting under that Admin). It can never read or write data scoped to a different Admin — that is enforced both at query-time (tenant_id filtering today) and will be enforced at storage-time (PostgreSQL Row-Level Security, Arc 9).

<div style="page-break-after: always;"></div>

### 2.3 Data Plane Detail

What the **end customer** interacts with and what the Luciel runtime executes against.

```
┌────────────────────────────────────────────────────────────────────────┐
│                              DATA PLANE                                 │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │                   Channel Adapter Layer                       │    │
│   │                                                                │    │
│   │   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐     │    │
│   │   │  Chat    │  │  Email   │  │   SMS    │  │  Voice   │     │    │
│   │   │  Widget  │  │  Inbound │  │  Inbound │  │  Inbound │     │    │
│   │   │ ✅ LIVE  │  │🔵 Arc 13 │  │🔵 Arc 13 │  │🔵 Arc 14b│     │    │
│   │   └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘     │    │
│   │        └─────────────┴─────────────┴─────────────┘            │    │
│   │                          │                                     │    │
│   │              (unified message envelope)                        │    │
│   └──────────────────────────┼─────────────────────────────────────┘    │
│                              ▼                                          │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │               Luciel Runtime Orchestrator                     │    │
│   │            🟨 SCAFFOLDED — app/runtime/orchestrator.py        │    │
│   │            → 🔵 Arc 14 (full agentic loop)                    │    │
│   │                                                                │    │
│   │     ┌────────┐    ┌──────────┐    ┌──────┐    ┌─────────┐    │    │
│   │     │  Plan  │ →  │ Retrieve │ →  │ Act  │ →  │ Reflect │    │    │
│   │     └────────┘    └──────────┘    └──────┘    └─────────┘    │    │
│   └─────┬─────────────┬────────────────┬──────────────┬──────────┘    │
│         │             │                │              │                │
│         ▼             ▼                ▼              ▼                │
│   ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌──────────────┐       │
│   │ Persona  │  │ Knowledge  │  │  Tool    │  │   Channel    │       │
│   │ Engine   │  │ Retriever  │  │  Broker  │  │   Arbiter    │       │
│   │ 🟨 SCAFF │  │ 🟨 SCAFF   │  │ 🟨 SCAFF │  │ 🔵 Arc 14    │       │
│   │ → Arc 15 │  │ → Arc 11   │  │ → Arc 12 │  │ (in/outbound)│       │
│   └──────────┘  └────────────┘  └──────────┘  └──────────────┘       │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │                        LLM Router                             │    │
│   │           🟨 SCAFFOLDED — app/integrations/llm/router.py       │    │
│   │                                                                │    │
│   │      OpenAI client ✅  │  Anthropic client ✅  │  Stub ✅      │    │
│   │      Provider arbitration: pending Arc 14                     │    │
│   └──────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

**Key principle:** Every API in this plane authenticates as an **embed key** (or a channel webhook signed for that key's owner). The runtime can never read knowledge, tool configuration, or persona for an instance that does not match the authenticated key.

<div style="page-break-after: always;"></div>

### 2.4 Persistence Layer Detail

What is stored where, and the isolation guarantees on each store.

```
┌────────────────────────────────────────────────────────────────────────┐
│                       SHARED PERSISTENCE LAYER                          │
│                                                                         │
│   ┌────────────────────────────────────────────────────────────┐      │
│   │                  PostgreSQL (RDS)                           │      │
│   │                  ✅ LIVE   + RLS  🔵 Arc 9                  │      │
│   │                                                              │      │
│   │   Tables (selected):                                         │      │
│   │   • admins, admin_team_members, instances                    │      │
│   │   • agent_config (5-pillar instance config)                  │      │
│   │   • conversations, conversation_messages                     │      │
│   │   • leads, scope_assignment                                  │      │
│   │   • knowledge_documents, knowledge_chunks                    │      │
│   │   • knowledge_embeddings (pgvector column)                   │      │
│   │   • admin_audit_log, trace                                   │      │
│   │   • subscription, invoice_history                            │      │
│   │                                                              │      │
│   │   Isolation: tenant_id filter on every query today.          │      │
│   │              PostgreSQL RLS to be enforced at the row level  │      │
│   │              in Arc 9 (the Tenant Isolation Audit arc).      │      │
│   └────────────────────────────────────────────────────────────┘      │
│                                                                         │
│   ┌──────────────────────────────┐   ┌─────────────────────────┐      │
│   │   Redis (ElastiCache)        │   │   S3 (Object Storage)   │      │
│   │   ✅ LIVE                    │   │   🔵 Arc 11             │      │
│   │                              │   │                          │      │
│   │   • Session cache            │   │   • Raw KB uploads       │      │
│   │   • Celery broker / backend  │   │     (PDF/DOCX/etc.)      │      │
│   │   • Rate-limit buckets       │   │   • Pre-chunking source  │      │
│   │   • Idempotency keys         │   │   • Lifecycle: keep 30d  │      │
│   └──────────────────────────────┘   │     after deactivation   │      │
│                                       └──────────────────────────┘     │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │                   Graph DB (deferred)                         │    │
│   │                   🔵 Arc 16                                   │    │
│   │                                                                │    │
│   │   For multi-hop reasoning over relationships extracted from   │    │
│   │   the KB. Vendor choice deferred — Neo4j vs. Neptune vs.      │    │
│   │   in-Postgres `apache_age`. See Architecture §10 open         │    │
│   │   decisions.                                                   │    │
│   └──────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

**Key principle:** **No data store is shared across Admins.** Every row, every cache key, every S3 object path is prefixed or filtered by the owning Admin's `tenant_id`. RLS in Arc 9 makes this a database-enforced guarantee, not just an application-enforced one.

<div style="page-break-after: always;"></div>

### 2.5 Observability + Audit Layer Detail

How we see what happened — for debugging, for the customer, and for compliance.

```
┌────────────────────────────────────────────────────────────────────────┐
│                       OBSERVABILITY + AUDIT                             │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │   admin_audit_log    ✅ LIVE                                  │    │
│   │   ────────────────                                            │    │
│   │   Append-only record of every Control-Plane action:           │    │
│   │   instance create, config change, KB upload, channel toggle,  │    │
│   │   tool toggle, deactivation, account closure.                 │    │
│   │   Surfaced in the dashboard. Retained: tier-dependent.        │    │
│   └──────────────────────────────────────────────────────────────┘    │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │   trace table        ✅ LIVE                                  │    │
│   │   ─────────                                                   │    │
│   │   Per-request execution trace inside the Data Plane:          │    │
│   │   tokens used, model called, tool invoked, KB chunks read,    │    │
│   │   latency per step. The substrate for the future "show your   │    │
│   │   reasoning" customer-visible debug view (Vision §6).         │    │
│   └──────────────────────────────────────────────────────────────┘    │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │   CloudWatch        ✅ LIVE                                   │    │
│   │   ──────────                                                  │    │
│   │   Structured app logs, metric filters, alarms.                │    │
│   │   Operator-facing — never customer-facing.                    │    │
│   └──────────────────────────────────────────────────────────────┘    │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │   Smoke Probe       ✅ LIVE (Arc 8 C4)                        │    │
│   │   ───────────                                                 │    │
│   │   ECS Fargate scheduled task hitting `/ready` against the     │    │
│   │   internal ALB. Confirms DB + Redis reachability from inside  │    │
│   │   the VPC every N minutes.                                    │    │
│   └──────────────────────────────────────────────────────────────┘    │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │   Retention Worker  ✅ LIVE                                   │    │
│   │   ─────────────────                                           │    │
│   │   Celery beat task. Deletes audit_log + trace rows past their │    │
│   │   tier-specific retention window (Free 30d, Pro 1yr, Ent 7yr).│    │
│   └──────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

**Key principle:** Audit data is **append-only** during its retention window, and the retention window is itself a function of the Admin's tier. This is the architectural surface that makes Enterprise's compliance promise (Vision §3.6) defensible.

> 📝 REVIEW: Is the two-plane separation explicit enough? Should we draw a hard network boundary (separate ECS services, separate ALB target groups) between control and data planes, or is logical separation in the same service acceptable at our scale?

> 📝 REVIEW: Is splitting the diagram into 5 views the right call, or do you want a combined "everything on one wall-poster" view in an appendix too?

---

## 3. Subsystem-by-Subsystem Architecture

This section walks each subsystem from the diagram, names the components, gives the data model anchor, and identifies the arc that owns the work.

### 3.1 Channel Adapter Layer

**Vision pillar:** §3.1 Communication Channels
**Owner arcs:** Arc 13 (email + SMS), Arc 14b (voice — deferred)

#### 3.1.1 Adapter Contract

Every channel adapter implements a uniform contract:

```python
class ChannelAdapter(Protocol):
    channel_id: str               # "widget" | "email" | "sms" | "voice"
    
    async def receive(request) -> InboundMessage:
        """Normalize a channel-specific payload into a unified envelope."""
    
    async def send(message: OutboundMessage) -> DeliveryReceipt:
        """Send via the channel; return delivery confirmation."""
    
    async def verify_inbound(request) -> InstanceContext:
        """Authenticate the inbound request → resolve to (admin_id, instance_id, session_id)."""
```

**The unified `InboundMessage` envelope:**

| Field | Source |
|---|---|
| `admin_id` | From channel-authenticated context |
| `instance_id` | From channel routing (embed key, email-to address, phone number) |
| `session_id` | New (if first contact) or matched (if existing thread) |
| `customer_identifier` | Channel-specific (browser fingerprint, email, phone, etc.) |
| `body` | Normalized text + structured attachments |
| `channel_metadata` | Channel-specific raw (headers, MIME, audio URL, etc.) |
| `received_at` | Ingestion timestamp |

#### 3.1.2 Per-Channel Components

| Channel | Adapter location | Status | Arc | Vendor |
|---|---|---|---|---|
| **Widget** | `app/api/v1/chat_widget.py` | ✅ LIVE | Shipped (Arc 1–8) | n/a (browser direct) |
| **Email** | `app/channels/email_adapter.py` | 🔵 DESIGNED | Arc 13 | SES inbound + outbound |
| **SMS** | `app/channels/sms_adapter.py` | 🔵 DESIGNED | Arc 13 | Twilio |
| **Voice** | `app/channels/voice_adapter.py` | 🔵 DESIGNED | Arc 14b | Twilio Voice + Whisper STT + ElevenLabs TTS |
| **WhatsApp** | `app/channels/whatsapp_adapter.py` | ⚪ PROPOSED | post-v1 | Meta Business API |
| **Slack** | `app/channels/slack_adapter.py` | ⚪ PROPOSED | post-v1 | Slack Bot API |

#### 3.1.3 Inbound Routing

When a message arrives, the adapter must resolve it to **exactly one `(admin_id, instance_id)`** pair before handing to the runtime.

- **Widget:** Embed key → DB lookup → `(admin_id, instance_id)`
- **Email:** Recipient address (`instance-slug@admin-slug.luciel-mail.com` or custom domain) → DB lookup
- **SMS:** Recipient phone number (provisioned per-instance from Twilio number pool) → DB lookup
- **Voice:** Recipient phone number → same as SMS lookup

**Failure mode:** unresolvable inbound is dropped with audit log (never silently consumed).

> 📝 REVIEW:
> - Twilio for SMS + Voice: agreed, or evaluate alternatives (Vonage, Plivo, MessageBird)?
> - Per-instance phone number provisioning: do we let each Pro instance grab its own number, or share a single number with routing via short codes / keywords?
> - Email inbound: SES inbound (in our existing AWS footprint) vs. dedicated vendor like Postmark / SendGrid Inbound Parse?

---

### 3.2 Knowledge Subsystem

**Vision pillar:** §3.3 Knowledge
**Owner arcs:** Arc 11 (vector v1), Arc 16 (graph v2)

#### 3.2.1 Existing Scaffolding (Good News)

The code already has substantial scaffolding:

| Component | Location | Status |
|---|---|---|
| Knowledge model | `app/models/knowledge.py` (`KnowledgeEmbedding`) | 🟨 SCAFFOLDED (table exists, vector column wired) |
| Chunker | `app/knowledge/chunker.py` | 🟨 SCAFFOLDED |
| Embedder | `app/knowledge/embedder.py` | 🟨 SCAFFOLDED |
| Ingestion service | `app/knowledge/ingestion.py` | 🟨 SCAFFOLDED |
| Retriever | `app/knowledge/retriever.py` | 🟨 SCAFFOLDED |
| Parsers | `app/knowledge/parsers/` (PDF, DOCX, HTML, JSON, MD, TXT, CSV) | 🟨 SCAFFOLDED — 7 formats |

**What's missing for Arc 11:**
- Admin-facing ingestion API (`/api/v1/admin/instances/{id}/knowledge`)
- Upload UI in the dashboard
- Website crawler (the URL-paste flow)
- Embed-worker integration into the Celery queue
- Per-Admin quota enforcement (100 MB / 5 GB / unlimited)
- Retrieval integration into the runtime orchestrator
- RLS policies on `knowledge_embeddings` (pending Arc 9)

#### 3.2.2 Data Model (Target — Arc 11 close)

```
knowledge_sources
  id, admin_id, instance_id, source_type ("file"|"url"|"paste"|"csv"),
  source_uri (S3 key or URL), source_filename, ingested_by (user_id),
  ingestion_status ("pending"|"chunking"|"embedding"|"ready"|"failed"|"soft_deleted"),
  soft_deleted_at, bytes_size, created_at, updated_at

knowledge_embeddings  (extends existing table)
  id, source_id (FK), admin_id, instance_id, chunk_ordinal,
  content, embedding (vector(1536)), token_count, created_at
  
  indexes:
    - (admin_id, instance_id) → tenant + instance scope
    - HNSW on embedding → fast vector search
    - source_id → for source-level deletion cascade
```

#### 3.2.3 Retrieval Flow

```
runtime asks → retriever.retrieve(instance_id, query_text, top_k=5)
              ↓
           filter WHERE admin_id = $authenticated_admin
                    AND instance_id = $current_instance
                    AND ingestion_status = 'ready'
              ↓
           HNSW ANN search on embedding column
              ↓
           re-rank by recency + chunk_ordinal proximity
              ↓
           return top 5 chunks → runtime injects into LLM context
```

**Hybrid retrieval (Arc 16):** Add a graph filter pass *before* the vector ANN — if the user query has structured intent ("3-bedroom under $1M"), graph filters the candidate set first, then vector ranks the survivors.

#### 3.2.4 Graph Store (Arc 16)

**Vendor candidates:** Neo4j AuraDB (managed) vs. Memgraph (open-source, in-memory) vs. PostgreSQL recursive CTEs (cheapest, performant up to ~1M edges).

**Recommendation:** Start with **PostgreSQL recursive CTEs** at Arc 16 — no new vendor, no new cost. Migrate to Neo4j only if we hit perf ceiling (likely after ~1000 Enterprise tenants with rich graph data).

> 📝 REVIEW:
> - pgvector for v1: agreed (Vision §3.3 already locked this)?
> - Graph at v2: PostgreSQL CTE first, Neo4j later — agree, or jump straight to Neo4j?
> - Should we offer a "raw knowledge view" in the dashboard (so Admins can see what got ingested), or keep it opaque?

---

### 3.3 Tool Subsystem

**Vision pillar:** §3.2 Tools
**Owner arc:** Arc 12

#### 3.3.1 Existing Scaffolding (Good News)

| Component | Location | Status |
|---|---|---|
| Tool registry | `app/tools/registry.py` | 🟨 SCAFFOLDED |
| Tool broker (dispatch) | `app/tools/broker.py` | 🟨 SCAFFOLDED |
| Tool base class | `app/tools/base.py` | 🟨 SCAFFOLDED |
| `escalate_tool` | `app/tools/implementations/escalate_tool.py` | 🟨 SCAFFOLDED |
| `save_memory_tool` | `app/tools/implementations/save_memory_tool.py` | 🟨 SCAFFOLDED |
| `session_summary_tool` | `app/tools/implementations/session_summary_tool.py` | 🟨 SCAFFOLDED |

**What's missing for Arc 12:**
- Most v1 catalog tools (book_appointment, send_email, send_sms, lookup_property, capture_lead, transfer_to_human, schedule_callback, call_sibling_luciel)
- Per-instance tool selection UI
- Per-instance tool authorization at runtime
- Tool input/output JSON schema validation
- Tool execution audit trail
- Tool execution sandbox (timeouts, retry policy, circuit breaker)

#### 3.3.2 Tool Contract

```python
class Tool(Protocol):
    tool_id: str                      # stable identifier
    display_name: str                 # admin-facing
    description: str                  # one sentence
    input_schema: JsonSchema          # validated before execution
    output_schema: JsonSchema         # validated after execution
    requires_tier: tuple[str, ...]    # ("free", "pro", "enterprise") or subset
    requires_channels: set[str]       # e.g. send_sms requires SMS channel enabled
    
    async def execute(input: dict, context: ToolContext) -> dict:
        """Execute with admin_id + instance_id in context for scoping."""
```

#### 3.3.3 Per-Instance Tool Authorization

```
instance_tools  (new table — Arc 12)
  instance_id, tool_id, enabled (bool), config (JSON for per-tool params),
  created_at, updated_at
  PK: (instance_id, tool_id)
```

**At runtime:** Tool broker checks `instance_tools.enabled = true` before dispatch. Default-deny: if no row, tool is disabled.

#### 3.3.4 Sibling-Luciel Composition

The `call_sibling_luciel` tool is special:

- Authorization: caller's `instance_tools.enabled` must include this tool
- Depth limit: caller's tier `max_composition_depth` enforced via a depth counter in `ToolContext`
- Loop prevention: `(caller_instance_id, callee_instance_id)` pairs tracked per request to detect cycles
- Audit: every sibling call written to `tool_execution_log` with full trace

> 📝 REVIEW:
> - Is the v1 catalog right (7 tools + composition)? Anything to add or drop?
> - Should we ship `bring_your_own_webhook` at v1 or defer to v2? (BYO adds significant security surface — input/output validation, retry policy, the customer's endpoint failing, etc.)
> - Tool execution sandbox: do tools run in-process (simple, but a buggy tool can crash the worker) or out-of-process (isolated, slower, more complex)?

---

### 3.4 Runtime Orchestrator (The Intelligence Layer)

**Vision pillar:** §4 Runtime Intelligence Layer
**Owner arc:** Arc 14

#### 3.4.1 Existing Scaffolding

| Component | Location | Status |
|---|---|---|
| Orchestrator entry | `app/runtime/orchestrator.py` | 🟨 SCAFFOLDED |
| Context assembler | `app/runtime/context_assembler.py` | 🟨 SCAFFOLDED |
| Runtime contracts | `app/runtime/contracts.py` | 🟨 SCAFFOLDED |
| LLM router | `app/integrations/llm/router.py` | 🟨 SCAFFOLDED |

#### 3.4.2 The Agentic Loop (Arc 14 target)

```
┌─────────────────────────────────────────────────────────────┐
│  RECEIVE                                                     │
│  - InboundMessage arrives via channel adapter               │
│  - Resolve (admin_id, instance_id, session_id)              │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  CONTEXT ASSEMBLY (context_assembler.py)                     │
│  - Load instance config (persona picklists, tools, channels)│
│  - Load recent conversation history (this session only)     │
│  - Retrieve knowledge (top-k from vector store)             │
│  - Compose system prompt from persona picklists             │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  PLAN                                                        │
│  - LLM call: "what should I do?"                            │
│  - Output: { reply, tool_calls[], should_escalate, channel }│
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  ACT                                                         │
│  - Dispatch any tool_calls via tool broker (parallel ok)    │
│  - Tool results merged back into context                    │
│  - If should_escalate → invoke escalation flow              │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  REFLECT                                                     │
│  - Did tools succeed? Was the answer satisfactory?          │
│  - If failed: retry (bounded N=2) or escalate               │
│  - Bounded loop: max 5 iterations per inbound               │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  RESPOND                                                     │
│  - Channel arbiter picks outbound channel                   │
│  - Adapter.send(outbound_message)                           │
│  - Delivery receipt logged                                  │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  LOG                                                         │
│  - Full trace → `trace` table                               │
│  - Lead/conversation update → `conversation` + `message`    │
│  - Tool executions → `tool_execution_log`                   │
└─────────────────────────────────────────────────────────────┘
```

#### 3.4.3 Channel Arbiter

Component: `app/runtime/channel_arbiter.py` (🔵 new at Arc 14)

**Decision tree:**
1. Did the customer explicitly request a channel? → Use that.
2. Is the response > 500 chars and inbound was SMS? → Switch to email (if enabled), prompt customer for permission.
3. Is this an urgent escalation? → Use highest-priority enabled channel (voice > SMS > email).
4. Default: same channel as inbound.

**Constraint:** Arbiter can only select from channels the **Admin enabled for this instance**. If preferred channel is disabled, fall back to inbound channel.

#### 3.4.4 LLM Router

Component: `app/integrations/llm/router.py` (🟨 already scaffolded)

**Per-tier provider routing (target — Arc 14):**

| Tier | `model_tier_default` | Provider chain |
|---|---|---|
| Free | `base` | GPT-4o-mini → Claude Haiku (fallback) |
| Pro | `mid` | GPT-4o → Claude Sonnet (fallback) |
| Enterprise | `top` | Claude Opus → GPT-4 (fallback) |

**Override path:** Admin can pin `agent_config.preferred_provider` (column already exists). Enterprise contracts can override via `admin_tier_overrides`.

> 📝 REVIEW:
> - Bounded loop at 5 iterations: right number? Too high (cost), too low (capability)?
> - Provider chain per tier: agreed mappings, or different defaults?
> - Should we expose model selection to Pro Admins (e.g. "choose your default model" dropdown), or keep it tier-locked?

---

### 3.5 Persona & Memory

**Vision pillar:** §3.5 Personality & Business Rules
**Owner arc:** Arc 15 (config UX) + Arc 14 (runtime composition)

#### 3.5.1 Existing Scaffolding

| Component | Location | Status |
|---|---|---|
| Luciel core persona | `app/persona/luciel_core.py` | 🟨 SCAFFOLDED |
| Memory service | `app/memory/service.py` | 🟨 SCAFFOLDED |
| Memory extractor | `app/memory/extractor.py` | 🟨 SCAFFOLDED |
| Cross-session retriever | `app/memory/cross_session_retriever.py` | 🟨 SCAFFOLDED — **but vision §5.4 says strict per-session at v1** |
| `agent_config` fields | `escalation_contact`, `system_prompt_additions`, `policy_overrides`, `preferred_provider` | ✅ LIVE (columns exist) |

#### 3.5.2 Persona Composition (Arc 14)

The system prompt is **never written by the customer**. It is composed at runtime from:

```
SYSTEM_PROMPT = 
  LUCIEL_CORE_PROMPT                           # static, app/persona/luciel_core.py
  + INSTANCE_NAME stanza                       # from instance.display_name
  + PICKLIST stanza (tone/verbosity/...)       # from agent_config picklists (Arc 15)
  + SPECIAL_INSTRUCTIONS stanza (≤280 chars)   # from agent_config.system_prompt_additions
  + KNOWLEDGE_CONTEXT stanza                   # retrieved chunks (truncated to budget)
  + CONVERSATION_HISTORY stanza                # last N turns this session only
  + TOOLS_AVAILABLE stanza                     # from instance_tools (Arc 12)
  + CHANNELS_AVAILABLE stanza                  # from instance_channels (Arc 13)
  + ESCALATION_RULES stanza                    # from agent_config.escalation_contact + triggers
```

#### 3.5.3 Memory — Strict Per-Session at v1

Per Vision §5.4: cross-session learning is **out of scope at v1**.

**Action item:** the existing `cross_session_retriever.py` must be **gated off** at runtime (feature flag = false) until we formally design the anonymization pipeline that Vision §5.4 requires.

> 📝 REVIEW: Agree to gate cross-session retrieval off at v1? Or keep it on but only for the Admin's own internal-facing instances (where there's no customer-data leakage concern)?

---

### 3.6 Lifecycle Subsystem

**Vision pillar:** §6 Lifecycle
**Owner arc:** Arc 10

#### 3.6.1 Existing Scaffolding

- `instances.active` column ✅ LIVE
- `instances.pending_downgrade_archived_at` ✅ LIVE — already designed for the soft-delete window
- `admin_audit_log` table ✅ LIVE — every lifecycle event written here
- Retention worker ✅ LIVE — runs daily, purges per tier window

#### 3.6.2 New Components at Arc 10

| Component | Type | Purpose |
|---|---|---|
| `deactivation_service.py` | Service | Orchestrates deactivation across all dependent resources |
| `cap_reclamation_service.py` | Service | Recomputes available cap after deactivation |
| `embed_key_revoker.py` | Service | Immediately invalidates all keys for a deactivated instance |
| `sibling_access_revoker.py` | Service | Revokes `call_sibling_luciel` paths into deactivated instances |
| `soft_delete_worker` | Celery task | After 30-day grace, hard-deletes knowledge embeddings |
| Deactivation UI | Frontend | Buttons + confirmation modals per Vision §6.1–6.3 |

#### 3.6.3 Deactivation Cascade (Arc 10 target)

```
Admin clicks "Deactivate Instance"
  ↓
POST /api/v1/admin/instances/{id}/deactivate
  ↓
deactivation_service.deactivate_instance(instance_id):
  1. instances.active = false
  2. embed_keys: status = 'revoked', revoked_at = now()
  3. instance_tools (sibling access): revoke any rows where this is the callee
  4. knowledge_sources: status = 'soft_deleted', soft_deleted_at = now()
  5. cap_reclamation: decrement admins.consumed_instance_count
  6. audit_log: INSERT 'instance_deactivated' event
  7. emit event for downstream listeners (analytics, billing)
  ↓
30 days later, soft_delete_worker:
  - DELETE knowledge_embeddings WHERE source.soft_deleted_at < now() - INTERVAL '30 days'
  - DELETE knowledge_sources WHERE soft_deleted_at < now() - INTERVAL '30 days'
```

Same cascade pattern for **team member deactivation** and **account closure**.

> 📝 REVIEW:
> - The 30-day soft-delete window: is it from `soft_deleted_at` (cleaner) or from `last_active_at` (more generous)?
> - On account closure: should we offer a one-click "Download all my data" button before hard-delete (GDPR best practice), or assume the Admin used CSV export already?

---

### 3.7 Isolation Architecture (The Four Walls)

**Vision pillar:** §5 Security & Isolation Boundaries
**Owner arc:** Arc 9 (audit + RLS hardening)

This is the **load-bearing architectural property** of the platform. Every other subsystem depends on these walls holding.

#### 3.7.1 Wall 1 — Cross-Admin (Tenant Isolation)

**Mechanism (target):** Three-layer defense.

| Layer | Mechanism | Status |
|---|---|---|
| **L1 — App layer** | Service-layer queries always filter by `admin_id` from authenticated context | ✅ LIVE (partial — Arc 9 audits coverage) |
| **L2 — DB layer (RLS)** | PostgreSQL Row-Level Security policies fail-closed if `admin_id` filter missing | 🔵 Arc 9 |
| **L3 — Network layer** | Per-Admin connection pool with `SET app.admin_id = '$x'` per request | 🔵 Arc 9 (deferred) |

#### 3.7.2 Wall 2 — Cross-Team (Within an Admin)

**Mechanism:** `scope_assignment` table + role catalog.

```
scope_assignment  (already exists)
  user_id, admin_id, scope_type ("instance" | "all_instances"),
  scope_id (instance_id if scoped), role ("owner"|"manager"|"operator"|"viewer"),
  granted_by, granted_at, revoked_at
```

**Service-layer enforcement:** every read/write checks `admin_id` AND `scope_assignment.scope_id includes target instance_id` AND `role permits operation`.

**Status:** 🟨 SCAFFOLDED (table exists). Role catalog + enforcement: 🔵 Arc 10–11.

#### 3.7.3 Wall 3 — Cross-Instance (Within an Admin)

**Mechanism:** `instance_id` non-null on every customer-data table.

**Tables that must carry `instance_id` (target Arc 9 audit):**
- ✅ `knowledge_embeddings` — has `luciel_instance_id`
- 🟨 `conversation` — needs verification
- 🟨 `message` — needs verification
- 🟨 `memory` — needs verification
- 🟨 `trace` — needs verification
- 🔵 `tool_execution_log` (new at Arc 12)

**Composition exception:** When `instance_tools` grants `call_sibling_luciel`, the runtime emits a sibling-access audit row + uses a derived context that explicitly names BOTH instances.

#### 3.7.4 Wall 4 — Cross-Lead (Within an Instance)

**Mechanism:** `session_id` scoping on every conversation row.

**Default retrieval scope:** `WHERE admin_id = $x AND instance_id = $y AND session_id = $z`.

**Cross-session learning:** disabled at v1 (Vision §5.4). When enabled at v2, goes through explicit anonymization (hash customer identifiers, strip PII, aggregate-only retrieval).

#### 3.7.5 RLS Policy Pattern (Arc 9)

Every customer-data table gets a policy of the form:

```sql
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;

CREATE POLICY conversations_admin_isolation
  ON conversations
  USING (admin_id = current_setting('app.admin_id', true)::text);

CREATE POLICY conversations_admin_isolation_write
  ON conversations
  FOR INSERT
  WITH CHECK (admin_id = current_setting('app.admin_id', true)::text);
```

**Service layer requirement:** every request must `SET app.admin_id = '$authenticated_admin'` on its DB connection before any query. Connection pool wrapper enforces this.

**Fail-closed posture:** if `app.admin_id` is not set, every RLS policy returns zero rows. A buggy query without the SET command returns empty — never another tenant's data.

> 📝 REVIEW:
> - Three-layer defense (L1+L2+L3): agree, or is L1+L2 sufficient at our scale?
> - RLS connection pool wrapper: build in-app or use an existing solution (PgBouncer + custom auth_query)?

---

## 4. Data Model Evolution

### 4.1 Tables That Already Exist

```
admins                         ✅ LIVE
instances                      ✅ LIVE
agent_config                   ✅ LIVE (escalation, system_prompt_additions, etc.)
knowledge_embeddings           ✅ LIVE (table exists; pipeline pending Arc 11)
scope_assignment               ✅ LIVE (table exists; enforcement pending Arc 10)
subscription                   ✅ LIVE
admin_audit_log                ✅ LIVE
admin_widget_domain            ✅ LIVE
api_key (embed keys)           ✅ LIVE
conversation                   ✅ LIVE
message                        ✅ LIVE
memory                         ✅ LIVE
session                        ✅ LIVE
trace                          ✅ LIVE
retention                      ✅ LIVE
email_send_event               ✅ LIVE
email_suppression              ✅ LIVE
identity_claim                 ✅ LIVE
user                           ✅ LIVE
user_consent                   ✅ LIVE
user_invite                    ✅ LIVE
```

### 4.2 New Tables Required by Vision

| Table | Arc | Purpose |
|---|---|---|
| `knowledge_sources` | Arc 11 | Tracks ingestion sources separately from chunks |
| `instance_tools` | Arc 12 | Per-instance tool authorization |
| `instance_channels` | Arc 13 | Per-instance channel selection |
| `tool_execution_log` | Arc 12 | Audit + debug for every tool call |
| `escalation_triggers` | Arc 14 | Multi-select trigger config per instance |
| `escalation_event` | Arc 14 | When an escalation fires + who was notified |
| `channel_outbound_log` | Arc 13 | Delivery receipts per channel send |
| `roles` (lookup) | Arc 10 | Role catalog: owner/manager/operator/viewer |

### 4.3 New Columns on Existing Tables

| Table | Column | Arc |
|---|---|---|
| `agent_config` | `personality_picklist` (JSON) | Arc 15 |
| `instances` | `soft_deleted_at` (replacing pending_downgrade_archived_at semantics?) | Arc 10 |
| `admins` | `closure_initiated_at` | Arc 10 |
| `user` | `deactivated_at`, `deactivated_by` | Arc 10 |

> 📝 REVIEW: Any tables or columns you can already see we'll need that aren't here?

---

## 5. Infrastructure (AWS)

### 5.1 Current Production Footprint

| Resource | Status | Purpose |
|---|---|---|
| ECS Fargate cluster `luciel-cluster` | ✅ LIVE | Container runtime |
| Service `luciel-backend-service` | ✅ LIVE | API backend |
| Service `luciel-worker-service` | ✅ LIVE | Celery worker |
| RDS Postgres | ✅ LIVE | Primary data store + pgvector |
| ElastiCache Redis | ✅ LIVE | Cache + Celery broker |
| ALB | ✅ LIVE | Public-facing HTTPS |
| Route53 + ACM | ✅ LIVE | DNS + TLS |
| Amplify (Luciel-Website) | ✅ LIVE | Frontend hosting |
| SES | ✅ LIVE (sandbox) | Transactional email |
| Stripe | ✅ LIVE | Billing |
| ECR | ✅ LIVE | Container registry |
| CloudWatch | ✅ LIVE | Logs + metrics |
| Smoke probe task-def | ✅ LIVE (Arc 8 C4) | Internal ALB health check |

### 5.2 New Infrastructure Required

| Resource | Arc | Purpose |
|---|---|---|
| **S3 bucket `vantagemind-kb-uploads`** | Arc 11 | Raw uploaded KB files |
| **S3 bucket `vantagemind-audit-archive`** | Arc 10 | Cold-storage audit chain post-account-closure |
| **Twilio account + phone number pool** | Arc 13 | SMS provisioning |
| **SES production sending (out of sandbox)** | Arc 13 (or sooner) | Reply-from + inbound email |
| **SES inbound receiving + S3 storage** | Arc 13 | Email-to-Luciel ingestion |
| **CloudWatch alarms on per-Admin error rates** | Arc 9 | Tenant-isolation regression detection |
| **(Eventual) Twilio Voice + S3 audio storage** | Arc 14b | Voice channel |
| **(Eventual) Neo4j AuraDB or Memgraph** | Arc 16 (if needed) | Graph KB |

### 5.3 Cost Surface

The two biggest cost adders in this roadmap:

| Item | Approx. cost driver |
|---|---|
| **OpenAI / Anthropic API calls** | Already scaling with usage; per-tier model selection caps the unit cost |
| **Twilio (SMS + Voice)** | $0.0075/SMS + ~$0.013/min voice; per-tier rate limits cap exposure |
| **pgvector storage + IO** | Negligible until ~10M chunks |
| **Neo4j (if adopted at Arc 16)** | $65/mo minimum for managed; defer until justified |

**No new fixed-cost AWS services proposed** through Arc 14. Arc 14b (voice) adds Twilio Voice + audio S3 storage.

> 📝 REVIEW: Anything on the AWS side you want to plan now (e.g. multi-region for data residency outside Canada, dedicated tenancy for Enterprise)?

---

## 6. Cross-Cutting Properties

### 6.1 Observability

Every cross-plane interaction emits to:

- **`trace` table** — structured event with `(admin_id, instance_id, session_id, request_id, event_type, payload)`
- **CloudWatch Logs** — same data, optimized for search
- **`admin_audit_log`** — Admin-visible subset (login, config change, key mint/revoke, deactivation)
- **CloudWatch Metrics** (Arc 9 enhancement) — per-Admin error rate, per-Admin request count, per-instance LLM token spend

### 6.2 Rate Limiting (Already Shipped)

Three-tier composition (Arc 7 C4 + Arc 8 C3):
- Per-Admin aggregate
- Per-Instance
- Per-Embed-Key

Continues to apply across all new channels — the rate-limit middleware is channel-agnostic.

### 6.3 Audit Chain Immutability

- `admin_audit_log` is **append-only** at the app layer (no UPDATE/DELETE routes)
- Database-level: separate role for audit writes; main app role lacks UPDATE/DELETE permission on this table (🔵 Arc 9)
- Cold storage to S3 happens via a worker that signs each row's hash → tamper-evident export at Enterprise tier

### 6.4 Foundation Model Agnosticism

The platform is **provider-agnostic** at every layer:
- `app/integrations/llm/router.py` ✅ already routes between OpenAI / Anthropic / stub
- Per-tier provider preference defined as a chain (primary → fallback)
- Embedding provider is also abstracted (currently OpenAI; pgvector-stored vectors are portable)

### 6.5 Soft-Delete by Default

Every customer-data table supports soft-delete:
- A `deleted_at` or `soft_deleted_at` column (varying by table)
- Hard-delete only after grace window (varies: 30 days for instances, 30 days for accounts post-closure)
- Audit log retained indefinitely (per tier audit retention window)

---

## 7. The Arc-by-Arc Architecture Delta

This is the **bridge** between vision §9 and concrete engineering work. Each arc lands specific architectural deltas.

| Arc | Component deltas | Schema deltas |
|---|---|---|
| **Arc 9** | RLS policies on every customer-data table; `app.admin_id` connection-pool wrapper; tenant-isolation audit + fixes | (no new tables) — adds RLS to existing |
| **Arc 10** | `deactivation_service`, `cap_reclamation_service`, `embed_key_revoker`, `sibling_access_revoker`, soft-delete worker, deactivation UI | `roles`, `admins.closure_initiated_at`, `user.deactivated_at`, `instances.soft_deleted_at` |
| **Arc 11** | KB ingestion API, upload UI, website crawler, embed-worker Celery integration, retriever-into-orchestrator wiring, S3 bucket | `knowledge_sources`; updates `knowledge_embeddings` |
| **Arc 12** | Tool registry expansion, 7 new v1 tools, sibling-Luciel composition runtime, tool UI, tool authorization at runtime | `instance_tools`, `tool_execution_log` |
| **Arc 13** | Email adapter (SES inbound), SMS adapter (Twilio), channel selection UI, Twilio number provisioning, SES production sending exit | `instance_channels`, `channel_outbound_log` |
| **Arc 14** | Full agentic loop in orchestrator, channel arbiter, escalation triggers, escalation event flow | `escalation_triggers`, `escalation_event` |
| **Arc 14b** | Voice adapter (Twilio Voice + Whisper STT + ElevenLabs TTS) | (extends `channel_outbound_log`) |
| **Arc 15** | Dropdown-driven personality config UI, system prompt composer rewrite | `agent_config.personality_picklist` |
| **Arc 16** | Graph store (PostgreSQL CTE or Neo4j), hybrid retrieval | `knowledge_graph_edges` (if CTE path) |

---

## 8. Open Architecture Decisions

Decisions the founder needs to confirm before the arc that owns them starts:

| # | Decision | Default if no decision | Must-decide-by Arc |
|---|---|---|---|
| 1 | Two-plane = logical (same ECS service) or physical (separate services + ALB target groups)? | Logical at v1; revisit if scale demands | Arc 13 |
| 2 | Twilio for SMS + Voice, or evaluate alternatives? | Twilio | Arc 13 |
| 3 | SES inbound vs. Postmark/SendGrid for email inbound? | SES (already in our footprint) | Arc 13 |
| 4 | Graph store at Arc 16: PostgreSQL CTE or Neo4j? | PostgreSQL CTE first | Arc 16 |
| 5 | Tool execution: in-process or sandboxed subprocess? | In-process at v1 (simpler) | Arc 12 |
| 6 | `bring_your_own_webhook` tool at v1 or v2? | v2 (per Vision §3.2) | Arc 12 |
| 7 | Per-instance SMS phone number (premium) or shared number + keywords? | Per-instance for Pro+, shared for Free | Arc 13 |
| 8 | Cross-session memory retrieval: gate off entirely at v1, or allow for Admin's internal-facing instances? | Gate off entirely (per Vision §5.4 strict reading) | Arc 11 |
| 9 | RLS connection pool: build in-app or PgBouncer? | In-app wrapper at v1 | Arc 9 |
| 10 | Tool execution audit log retention: same as conversation retention, or longer? | Same as conversation (per tier) | Arc 12 |

---

## 9. Doctrine Anchors

This architecture is downstream of the vision and must remain consistent with it:

- **Vision (upstream)** — `docs/VANTAGEMIND_VISION_v1_FINAL.md` (canonical product source-of-truth; if this doc and the vision diverge, vision wins)
- **Customer journey (sibling)** — `docs/VANTAGEMIND_CUSTOMER_JOURNEY_v1_FINAL.md` (the customer-facing journey downstream of the same vision)
- **Tier entitlements (live code)** — `app/policy/entitlements.py` (the runtime expression of vision §7)

**Amendment process:** Any architecture change is `ARCHITECTURE_v2` — never an in-place edit. v1 is preserved in git as the founder-approved baseline.

---

## 10. What I Need From You

Same review style as the vision doc. Read top-to-bottom once, then attack the `📝 REVIEW:` callouts. There are **15 review prompts** in this doc spread across §1, §2, §3.1, §3.2, §3.3, §3.4, §3.5, §3.6, §3.7, §4, §5.

For Section 8's 10 open architecture decisions, gut-feel answers are fine — defaults are sensible enough to ship if you have no strong opinion.

Once you're back with thoughts, I'll cut **ARCHITECTURE_v1_FINAL.md** with the open decisions locked, commit alongside the vision doc, and we begin Arc 9.

---

**Document end. Founder review pending.**
