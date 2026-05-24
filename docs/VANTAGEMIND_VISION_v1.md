# VantageMind — Product Vision v1 (Draft for Founder Review)

**Status:** DRAFT — Founder review pending
**Author:** Aryan + Partner (this session)
**Date:** 2026-05-24
**Purpose:** Capture the founder's full product vision in one document so every subsequent arc anchors to it. This supersedes the chat-widget-only mental model of Arcs 1–8.

---

## 0. How to Read This Document

This is the **vision target** — what we are building toward, not what is shipped today. Sections 1–6 describe the destination. Sections 7–8 describe where we are now. Section 9 proposes how we sequence the gap-close.

**Founder review action items at the bottom of each section** in `> 📝 REVIEW:` callouts. Mark them up however you want — strikethrough, comment in-line, or just talk through them with me.

---

## 1. The Product in One Sentence

VantageMind is a platform where any business owner can assemble an **AI employee ("Luciel")** in under 10 minutes — picking communication channels, tools, knowledge, and an escalation human from a clean dropdown UI — and that Luciel then **autonomously serves the business's customers** across whichever channels make sense, without hallucinating, without leaking data, and without the owner having to write a single line of code.

> 📝 REVIEW: Does this one-liner capture it? Adjust freely.

---

## 2. The Core Mental Model — "Luciel as Employee"

A Luciel instance is not a chatbot. It is an **employee** the business owner hires.

Like hiring a human:
- **You decide what channels they answer** (some employees do phone, some do email, some do both).
- **You decide what tools they have access to** (some can book appointments, some can look up the MLS, some can charge a credit card).
- **You give them the knowledge they need** (the listing brochure, the FAQ, the brand voice guide).
- **You tell them who to escalate to** (manager's phone, supervisor's email, the founder's Slack).
- **You decide their personality** (warm vs. professional, brief vs. thorough) — but you do not write a manual; you pick from menus.

The Luciel then **figures out the rest on its own at runtime** — which channel to use, which tool to call, when to escalate, when to delegate to a sibling Luciel.

> 📝 REVIEW: Does the "employee" frame capture how you want customers to think about this? Any analogy that fits better?

---

## 3. The Five Configuration Pillars (Admin-Facing)

When a business owner creates a Luciel, they configure **five pillars**. All five are dropdown-driven; none require the customer to write prose longer than a tweet.

### 3.1 Communication Channels

What the Luciel can listen on and speak through.

**v1 channels (planned):**
- Chat widget (already shipped — Arc 1–8)
- Email (inbound + outbound)
- SMS (inbound + outbound, via Twilio or equivalent)

**v2 channels (later):**
- Voice (phone, via Twilio Voice + speech-to-text + text-to-speech)
- WhatsApp Business
- Slack (for internal-facing Luciels)
- Instagram DM / Facebook Messenger

**Per-instance selection.** An Admin running 10 instances might wire:
- Listing A's Luciel → chat widget only
- Listing B's Luciel → chat widget + email + SMS
- Internal HR Luciel → Slack only

**The UI:** A multi-select dropdown labeled "Channels this Luciel uses." That's it.

> 📝 REVIEW: Is the v1/v2 split right? Should voice be v1? Should we ship email + SMS together or one first?

---

### 3.2 Tools

What the Luciel can *do* (not just *say*).

**v1 tool catalog (pre-built, no code needed):**
- `book_appointment` — wire to Calendly / Google Calendar
- `send_email` — outbound email on behalf of the business
- `send_sms` — outbound SMS
- `lookup_property` — query MLS or a CSV the Admin uploads
- `capture_lead` — write to the Admin's CRM (HubSpot, Salesforce, custom webhook)
- `transfer_to_human` — handoff to live agent
- `schedule_callback` — queue a future outbound

**v1.5 (advanced):**
- `call_sibling_luciel` — instance composition (Listing A's Luciel asks Brand Guide Luciel a question)

**v2 (eventually):**
- Bring-your-own-webhook — Admin registers a custom HTTP endpoint as a tool

**Per-instance selection.** Each Luciel only has the tools the Admin checks. Default-deny.

**The UI:** A checklist labeled "Tools this Luciel can use." Each tool has a one-sentence description.

> 📝 REVIEW:
> - Is the v1 catalog right? Are any of these wrong / missing?
> - Should we ship BYO webhooks at v1 (more flexible, more dangerous) or wait until v2?
> - For real estate specifically: is MLS lookup table-stakes for v1? Which MLS providers?

---

### 3.3 Knowledge

What the Luciel knows, beyond the base model.

**v1 ingestion sources (pre-built):**
- File upload (PDF, DOCX, TXT, CSV)
- Website crawl (paste a URL, we crawl the public site)
- Manual paste-in ("paste your FAQ here")
- CSV/table import (for property lists, product catalogs)

**v2 ingestion sources:**
- Google Drive folder sync
- Notion workspace sync
- Direct CRM sync (HubSpot, Salesforce knowledge base)

**Storage architecture (proposed):**
- **Vector store** (semantic search) — for fuzzy questions ("does this listing have a pool?")
- **Graph store** (structured relationships) — for relational questions ("which of my listings have 3 bedrooms AND are under $1M?")
- **Hybrid retrieval at runtime** — the Luciel's runtime first hits graph for structured filters, then hits vector for semantic match, then hands the merged context to the LLM.

**Scoping (security-critical):**
- Knowledge is **scoped to an instance by default**.
- Across instances within the same Admin: knowledge can be **shared via composition** (Pro: depth 2; Enterprise: unlimited; Free: no composition).
- Across Admins: **never**. Hard tenant isolation.

**The UI:** "Knowledge Base" section with three buttons: Upload Files, Crawl Website, Paste Text. A list of ingested sources, each with a "Remove" button.

> 📝 REVIEW:
> - Vector + graph hybrid: ship at v1, or vector-only at v1 and graph at v2? My recommendation: vector-only at v1 (Pinecone / pgvector / Qdrant), graph at v2 (Neo4j or Memgraph). Graph is the bigger lift.
> - Which vector store? My recommendation: **pgvector** (we already have Postgres, no new vendor, fits our footprint). Alternative: Pinecone (managed, scales easier, costs money).
> - For website crawl: do we respect robots.txt and ToS, or warn the user it's their responsibility?

---

### 3.4 Escalation Contact

The human the Luciel pings when it's stuck or when the situation requires a person.

**Per-instance config:**
- **Primary escalation:** phone number, email, or Slack handle
- **Secondary escalation:** (optional fallback)
- **Escalation triggers (dropdown):**
  - "When customer explicitly asks for a human"
  - "When the customer is frustrated" (sentiment-based)
  - "When the customer asks something I don't know"
  - "When the conversation exceeds N minutes / N turns"
  - "When a high-value lead is detected" (e.g. budget > $X)
  - "Never escalate, always try to answer"

**The UI:** Two text fields (primary contact, secondary contact) + a multi-select for triggers.

**Field already exists:** `agent_config.escalation_contact` is a column in the live DB — we just need to wire it through the UI and the runtime.

> 📝 REVIEW:
> - Are these triggers the right ones? Missing any?
> - Should escalation default to a sensible setting (e.g. "always escalate when stuck") if the Admin leaves it blank?

---

### 3.5 Personality & Business Rules

Who the Luciel *is*, in dropdowns.

**v1 picklist axes (proposed):**
- **Tone:** Friendly / Professional / Direct / Warm
- **Verbosity:** Concise / Balanced / Thorough
- **Formality:** Casual / Business-casual / Formal
- **Pace:** Quick replies / Deliberate, considered replies
- **Persona name:** Free text, 1 field (e.g. "Sarah's Listing Helper")

**One optional free-text field** (capped at 280 chars): "Anything specific about your business this Luciel needs to know?" — for the edge case where dropdowns aren't enough. Hard character cap keeps it tweet-sized.

**Behind the scenes**, these picklist selections compose into the system prompt — the customer never sees the prompt. The free-text field gets appended as a "special instructions" stanza.

**Field already exists:** `agent_config.system_prompt_additions` is a column — currently free-text, we'd repurpose it as the optional 280-char field, and the picklist-derived prompt is built at runtime.

> 📝 REVIEW:
> - Are these the right picklist axes? Too many? Too few?
> - 280 chars on the free-text field — too restrictive? Should we allow 500? 1000?
> - Any business-domain specific picklists for real estate (e.g. "specialty: residential / commercial / luxury / rental")?

---

## 4. The Runtime Intelligence Layer

Once the five pillars are configured, the Luciel runs an **agentic loop** — Perplexity-Computer-style — every time a customer talks to it.

### 4.1 The Loop (per inbound message)

1. **Receive** — message arrives on a channel (widget / email / SMS / voice).
2. **Identify** — match to existing conversation or create new lead.
3. **Retrieve** — pull relevant knowledge (graph filter + vector match).
4. **Plan** — LLM reasons: "what does this customer need? what tool/channel/escalation fits?"
5. **Act** — call a tool, draft a reply, schedule a callback, or escalate.
6. **Reflect** — did the action succeed? If not, retry or escalate.
7. **Respond** — deliver the answer on the channel the customer used (or a different channel if the Luciel decides outbound differently — see 4.2).
8. **Log** — full trace into the audit chain.

### 4.2 Channel Arbitration

The Luciel decides — within the channels the Admin enabled — when to use which.

**Rules (proposed defaults):**
- **Inbound channel = default outbound channel.** A customer who texts gets a text back.
- **Override conditions:**
  - If a long answer is needed and inbound was SMS → switch to email (with permission) for the long version.
  - If urgent (lead detected) and inbound was email → switch to SMS / call for immediate follow-up.
  - If the customer asks for a callback → use voice channel.
- **Customer-initiated channel switch always wins.** If the customer says "email me," the Luciel emails.

### 4.3 Sibling-Luciel Delegation

If `composition_enabled` and the Luciel's tools include `call_sibling_luciel`:
- The Luciel can ask another Luciel a question (e.g. listing Luciel asks brand-guide Luciel "what's our company's stance on lowball offers?")
- Bounded by `max_composition_depth` (Pro: 2; Enterprise: unlimited).
- Audited end-to-end.

### 4.4 Escalation Logic

If any of the configured escalation triggers fire:
- Pause the conversation (or continue in degraded mode).
- Notify the escalation contact via their preferred channel.
- Include a one-paragraph summary + a deep-link to the full conversation in the dashboard.
- Optionally wait for human response before continuing (or hand off entirely).

> 📝 REVIEW:
> - The plan-act-reflect loop: any step missing or wrong?
> - Channel arbitration defaults: agree with the rules in 4.2?
> - Should the Luciel ever proactively reach out (outbound without prior inbound) — e.g. nurture follow-ups — or only respond to inbound? My recommendation: v1 = reactive only; v2 = proactive nurture.

---

## 5. Security & Isolation Boundaries (Non-Negotiable)

These are the **four leakage walls** the platform must enforce, always, on every read and write.

### 5.1 Cross-Admin Isolation (Tenant Isolation)

**Definition:** Admin A's data never reaches Admin B under any code path, including bugs, including admin-level mistakes.

**Mechanism:**
- Every customer-data table has `admin_id` (or `tenant_id`) as a non-null indexed column.
- Every query in the service layer filters by the authenticated admin's ID.
- Recommended hardening: **PostgreSQL Row-Level Security (RLS)** policies that fail-closed if the `admin_id` filter is missing from a query. Defense-in-depth so even a buggy query can't return another admin's row.

**Status today:** Tenant-id columns exist on most tables (verified `knowledge`, `agent_config`). RLS posture: **needs audit** (Arc 10 work).

### 5.2 Cross-Team Isolation (Within an Admin, Pro + Enterprise Only)

**Definition:** If an Admin has 25 team members (Pro) or unlimited (Enterprise), some seats may have access to only a subset of instances/data.

**Mechanism:**
- Role + scope assignment table (`scope_assignment` exists in the live schema — good).
- Every query checks both `admin_id` AND `scope_assignment` for the authenticated user.
- Roles (proposed): `admin_owner`, `admin_manager`, `instance_operator`, `read_only_viewer`.

**Status today:** `scope_assignment` table exists. Role catalog + UI surface: **needs design** (Arc 10–11 work).

### 5.3 Cross-Instance Isolation (Within an Admin)

**Definition:** Listing A's Luciel cannot see Listing B's conversations, leads, or knowledge — unless composition explicitly grants it and the depth is within tier limits.

**Mechanism:**
- Every conversation, lead, knowledge embedding has `instance_id` as a non-null column.
- Runtime retrieval filters by `instance_id` by default.
- Composition grants (when enabled) are explicit, audited, and bounded by `max_composition_depth`.

**Status today:** `luciel_instance_id` exists on `knowledge` (good). Need to verify on `conversation`, `message`, `memory`, `trace`. **Arc 10 audit work.**

### 5.4 Cross-Lead Isolation (Within an Instance)

**Definition:** Lead #1's conversation history does not bleed into Lead #2's session, even though both talked to the same Luciel.

**Mechanism:**
- `session_id` scoping on every message + memory entry.
- Conversational memory is per-session by default.
- Cross-session pattern learning (if enabled) goes through an anonymization pipeline.

**Status today:** `session` + `conversation` + `message` tables exist. **Pipeline review needed at Arc 10.**

> 📝 REVIEW:
> - Are these four walls the right four, or am I missing one?
> - PostgreSQL RLS — agree this is the right defense-in-depth posture, or do you prefer a different approach (app-layer guards only)?
> - For cross-lead: should we ever allow cross-session learning (e.g. "this Luciel learned that customers in this zip code respond better to friendly tone") with anonymization, or is per-session strict?

---

## 6. The Lifecycle (Activation, Deactivation, Cap Reclamation)

Every entity (Admin account, team member, instance) has a clean activation → active → deactivation → (optional) reactivation → (eventual) hard-delete lifecycle.

### 6.1 Instance Deactivation

**Trigger:** Admin clicks "Deactivate" in the UI.

**Effects (proposed):**
- `instances.active = false` (already exists).
- All embed keys for the instance are **immediately revoked** (any widget request returns 401).
- All sibling Luciels lose `call_sibling_luciel` access to this instance.
- All knowledge embeddings for this instance enter a **30-day soft-delete** window (recoverable).
- Conversations + leads: **retained** per the admin's audit retention window (Free 30d / Pro 365d / Enterprise unlimited).
- The instance **frees a slot against `instance_count_cap`** — the Admin can create a new instance in its place immediately.
- After 30 days: knowledge is **hard-deleted**. Conversations + leads continue per retention window.

**UI:** A "Deactivate" button on every instance card, with a confirmation modal: "This will stop the Luciel and free up 1 of your 10 instance slots. Knowledge will be archived for 30 days, then deleted. Leads will be retained per your plan's audit window. Continue?"

### 6.2 Team Member Deactivation (Pro + Enterprise)

**Trigger:** Admin clicks "Remove" on a team member.

**Effects:**
- User account → `active = false`.
- All sessions invalidated.
- All `scope_assignment` rows revoked.
- The seat **frees a slot against `seat_cap`** — admin can invite a replacement.
- Audit log retained (who-did-what is forever, per audit retention window).

**Enterprise note:** With unlimited seats, cap reclamation is moot but the deactivation flow still applies for security.

### 6.3 Admin Account Deactivation (Self-Service Closure)

**Trigger:** Admin clicks "Close Account" in settings.

**Effects:**
- Subscription cancelled at Stripe (immediately or end-of-period — Admin's choice).
- All instances deactivated.
- All team members invalidated.
- All embed keys revoked.
- **30-day grace period:** Admin can reactivate by logging in and resubscribing.
- After 30 days: **GDPR-style hard delete** of all customer data; audit log archived to cold storage for legal retention window (7 years for paying customers, 30 days for Free).

### 6.4 Reactivation

- **Instance:** Admin clicks "Reactivate" within 30 days → knowledge restored, embed keys re-minted (new keys, old keys stay revoked), capacity slot consumed again.
- **Account:** Log in within 30 days → resubscribe → full restore.
- **Team member:** Re-invite (treated as new user; old data not auto-restored).

### 6.5 Hard Delete

- After grace windows expire.
- Audit log + minimal compliance record retained per legal requirements.
- The deletion itself is logged into the cold-storage audit chain.

> 📝 REVIEW:
> - 30-day grace on instance deactivation: right number? Some platforms do 7 days, some 90.
> - Account closure: should we allow "pause subscription" as a softer option (subscription paused, instances frozen, data preserved indefinitely until they un-pause)?
> - GDPR hard-delete after 30 days post-close: agree, or longer?

---

## 7. Tier-by-Tier Summary (Vision-Aligned)

Each tier exposes a subset of the platform's capabilities. The matrix below summarizes the **destination** — not all of these are shipped today (see §8).

| Capability | Free | Pro | Enterprise |
|---|---|---|---|
| **Instances** | 1 | 10 | Unlimited |
| **Channels per instance** | Widget only | Widget + Email + SMS | All channels (incl. voice, WhatsApp) |
| **Tool catalog access** | Basic (capture_lead, transfer_to_human) | Full v1 catalog | Full + BYO webhooks |
| **Knowledge: vector store** | ✅ (small quota) | ✅ (larger quota) | ✅ (unlimited) |
| **Knowledge: graph store** | ❌ | ✅ | ✅ |
| **Sibling Luciel delegation** | ❌ | Depth 2 | Unlimited |
| **Escalation contact** | 1 contact | Primary + secondary | Multiple, role-based |
| **Personality picklists** | Basic 4 axes | Full picklists + 280char free-text | Full + override hooks |
| **Cross-team isolation** | n/a (1 seat) | ✅ | ✅ with delegated admin |
| **Deactivation lifecycle** | Self-service | Self-service | Self-service + CSM-assisted |
| **Pre-built integrations** | Calendar (1) | Calendar + 1 CRM | Unlimited integrations |
| **Audit retention** | 30 days | 365 days | Unlimited |
| **Support** | Community | 48h email | 24h email + CSM |
| **Branding** | Powered-by VantageMind | Powered-by VantageMind, custom domain | Fully white-labeled |
| **Price** | $0 | TBD/mo or TBD/yr | $2,800/mo or $24,000/yr |

> 📝 REVIEW:
> - **Pro pricing is still TBD** — what's your gut? $99/mo? $199/mo? $299/mo? With annual discount?
> - Knowledge quotas (Free vs. Pro): should there be a hard cap or just rate-limit ingestion?
> - Free getting `transfer_to_human` tool — fair, or should escalation be a Pro feature?

---

## 8. Where We Are Today (Honest Status)

### 8.1 Shipped & Production (Arcs 1–8)

- ✅ Three-tier entitlement matrix, founder-locked
- ✅ Multi-tenant Postgres schema with `admin_id` / `tenant_id` scoping on most tables
- ✅ Stripe Live billing (Pro placeholder + Enterprise $2,800/$24,000)
- ✅ Chat widget channel (the *only* channel today)
- ✅ Embed key minting, rotation, revocation
- ✅ Rate-limit composition (per-admin / per-instance / per-key)
- ✅ Signup fraud gate (1-per-IP + hCaptcha)
- ✅ `/ready` + `/health` + smoke probe
- ✅ Audit log infrastructure (`admin_audit_log`)
- ✅ `agent_config` table with `escalation_contact`, `system_prompt_additions`, `policy_overrides`, `preferred_provider` columns — **the bones of personality + escalation are already in the DB**
- ✅ `knowledge` table with vector embedding column + instance scoping — **the bones of the vector KB are already in the DB**
- ✅ `scope_assignment` table — **the bones of team isolation are already in the DB**
- ✅ `instances.active` field — **the bones of deactivation are already in the DB**

### 8.2 Partially Shipped

- 🟡 **Instance composition** — entitlement allows it (`composition_enabled`, `max_composition_depth`); runtime wiring not verified.
- 🟡 **Tenant isolation** — column-level scoping exists; RLS posture not audited.
- 🟡 **Knowledge ingestion** — table exists; upload/crawl/embed pipeline not built.
- 🟡 **Escalation field** — column exists; UI + runtime trigger not built.

### 8.3 Not Yet Shipped (The Real Vision Gaps)

- ❌ Non-widget channels (email, SMS, voice, WhatsApp)
- ❌ Tool registry + tool catalog
- ❌ Knowledge ingestion pipeline (upload UI, crawler, embedding worker, retrieval at runtime)
- ❌ Graph knowledge store
- ❌ Dropdown-driven personality config UI
- ❌ Agentic runtime loop (plan → tool → reflect)
- ❌ Channel arbitration logic
- ❌ Deactivation UI + cap-reclamation logic
- ❌ Team member management UI
- ❌ Account closure flow
- ❌ RLS hardening

### 8.4 The Good News

**More of the foundation is already in the schema than I initially thought.** `agent_config`, `knowledge`, `scope_assignment`, `instances.active`, `escalation_contact` all exist as columns. We are not starting from zero on data model — we're starting from "data model exists, runtime + UI need to be built."

> 📝 REVIEW: Anything in §8 you'd dispute or want me to verify more carefully? I'd happily do a second-pass audit if any of these statuses feel optimistic.

---

## 9. Proposed Roadmap (Arc 9 → Arc 15)

| Arc | Theme | Duration | Why this order |
|---|---|---|---|
| **Arc 9** | **Tenant isolation audit + RLS hardening** | 1–2 weeks | The foundation. Before we let customers ingest knowledge, we must prove the four walls hold. |
| **Arc 10** | **Deactivation lifecycle (instance + team + account) + cap reclamation + UI** | 2–3 weeks | P0 vision gap. Today an Admin literally cannot deactivate cleanly. This must ship before customers depend on us. |
| **Arc 11** | **Knowledge base v1 (pgvector ingestion + retrieval, no graph yet)** | 3–4 weeks | Largest single quality lever. Stops hallucination. |
| **Arc 12** | **Tool registry + v1 tool catalog + sibling-Luciel composition runtime** | 3–4 weeks | Unlocks the "Luciel as employee" frame. |
| **Arc 13** | **Channels: email + SMS adapters (voice deferred to Arc 14b)** | 2–3 weeks | First non-widget channels. Email is easy; SMS is medium (Twilio integration). |
| **Arc 14** | **Agentic runtime + channel arbitration + escalation triggers** | 3–4 weeks | The intelligence layer. Ties knowledge + tools + channels together. |
| **Arc 14b** | **Voice channel (Twilio Voice + STT + TTS)** | 3–4 weeks | Optional / deferred — depends on demand. |
| **Arc 15** | **Configuration UX rewrite (dropdown-driven, minimalist)** | 2–3 weeks | The customer-facing polish. After the platform underneath is real, we make it beautiful. |
| **Arc 16** | **Graph knowledge store + hybrid retrieval (v2 KB)** | 3–4 weeks | After we validate vector-only KB works in production. |

**Total: ~5–6 months of focused execution to ship the full vision.**

**Live E2E posture:** I recommend we run E2E **after Arc 10** (post-deactivation) — that's the first arc that puts a customer-honest product on the table. The current Arc 8 E2E plan becomes a **regression test** we run between every arc.

> 📝 REVIEW:
> - Order: agree, or do you want to reshuffle? (e.g. some founders would put channels before KB; some put UX before runtime.)
> - Voice channel: ship in v1 or defer? My recommendation: defer to 14b unless real estate customers demand phone immediately.
> - Should we ship anything to *early-access customers* between arcs, or hold until Arc 15 closes? My recommendation: open early-access after Arc 11 (KB) — that's the first arc where Luciel becomes genuinely useful.

---

## 10. Open Questions for Founder

A short list of things I need a gut call on before I touch a single line of code:

1. **Pro pricing?** (Today: TBD. Range: $99–$299/mo with annual discount.)
2. **Vector store vendor?** (My pick: **pgvector** — no new vendor, fits our footprint. Alternative: Pinecone.)
3. **Telephony vendor when we get to voice?** (Likely Twilio. Alternative: Vonage, Plivo.)
4. **Knowledge ingestion: file size cap?** (Recommend: 10MB per file at Free, 50MB at Pro, 500MB at Enterprise.)
5. **Account closure grace period?** (Recommend: 30 days. Alternative: 7 days hard, with optional "pause subscription" as a softer middle ground.)
6. **Should Free have any tools beyond `capture_lead` + `transfer_to_human`?** (My instinct: no — tools are a Pro upsell.)
7. **Customer onboarding flow:** self-serve only, or do we offer a "guided setup" for Pro and a "white-glove" for Enterprise? (My recommendation: self-serve for both, but Enterprise gets a CSM-led kickoff call.)
8. **Live E2E:** run Arc 8 regression E2E *now* before starting Arc 9, or defer until post-Arc-10?

---

## 11. What I Need From You

Please review at your pace and mark up however feels natural — strike through what's wrong, write `>> ADJUST: ...` inline, or just hand me back highlights of the disagreements verbally. There are no wrong answers here. The doc is meant to **catch the vision in writing** so we can argue with it cleanly, not so we can rubber-stamp it.

Once you're back, we lock the doc, version it as **VISION_v1 (founder-approved)**, and every subsequent arc anchors against it.

---

**Doc end. Founder review pending.**
