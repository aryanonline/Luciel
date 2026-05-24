# VantageMind — Product Vision v1 (Founder-Approved)

**Status:** FINAL — Founder-approved
**Approved by:** Aryan Singh
**Date:** 2026-05-24
**Supersedes:** Chat-widget-only mental model of Arcs 1–8
**Anchor:** This is the canonical product vision. Every subsequent arc (Arc 9 onward) anchors to this document. If code, doctrine, or roadmap diverges from this vision, **this document wins** and the divergence must be either corrected or formally amended via VISION_v2.

---

## 0. How to Read This Document

Sections 1–6 describe the destination — what we are building toward.
Section 7 summarizes the tier-by-tier capability map.
Section 8 captures the honest status of what is shipped today.
Section 9 lays out the proposed execution roadmap (Arc 9 → Arc 16).
Section 10 records the founder's open decisions still to be locked.

---

## 1. The Product in One Sentence

VantageMind is a platform where any business owner can assemble an **AI employee ("Luciel")** in under 10 minutes — picking communication channels, tools, knowledge, and an escalation human from a clean dropdown UI — and that Luciel then **autonomously serves the business's customers** across whichever channels make sense, without hallucinating, without leaking data, and without the owner having to write a single line of code.

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

---

## 3. The Five Configuration Pillars (Admin-Facing)

When a business owner creates a Luciel, they configure **five pillars**. All five are dropdown-driven; none require the customer to write prose longer than a tweet.

### 3.1 Communication Channels

What the Luciel can listen on and speak through.

**v1 channels (in-scope for the Arc 9–15 roadmap):**
- Chat widget (already shipped — Arc 1–8)
- Email (inbound + outbound)
- SMS (inbound + outbound, via Twilio or equivalent)

**v2 channels (deferred to a later vision rev):**
- Voice (phone, via Twilio Voice + speech-to-text + text-to-speech)
- WhatsApp Business
- Slack (for internal-facing Luciels)
- Instagram DM / Facebook Messenger

**Per-instance selection.** An Admin running 10 instances might wire:
- Listing A's Luciel → chat widget only
- Listing B's Luciel → chat widget + email + SMS
- Internal HR Luciel → Slack only

**The UI:** A multi-select dropdown labeled "Channels this Luciel uses." That is the entire surface area.

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

**Per-instance selection.** Each Luciel only has the tools the Admin checks. **Default-deny** posture.

**The UI:** A checklist labeled "Tools this Luciel can use." Each tool has a one-sentence description.

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

**Storage architecture:**
- **Vector store** (semantic search) — for fuzzy questions ("does this listing have a pool?")
  - **v1 vendor: pgvector** — fits our Postgres footprint, no new vendor, no new cost surface.
- **Graph store** (structured relationships) — for relational questions ("which of my listings have 3 bedrooms AND are under $1M?")
  - **v2 add** — deferred to Arc 16 after we validate vector-only performance in production.
- **Hybrid retrieval at runtime** — the Luciel's runtime first hits graph (when available) for structured filters, then hits vector for semantic match, then hands the merged context to the LLM.

**Scoping (security-critical):**
- Knowledge is **scoped to an instance by default**.
- Across instances within the same Admin: knowledge can be **shared via composition** (Pro: depth 2; Enterprise: unlimited; Free: no composition).
- Across Admins: **never**. Hard tenant isolation.

**The UI:** "Knowledge Base" section with three buttons: Upload Files, Crawl Website, Paste Text. A list of ingested sources, each with a "Remove" button.

**File size caps (v1):**
- Free: 10 MB per file
- Pro: 50 MB per file
- Enterprise: 500 MB per file

**Website crawl posture:** We respect robots.txt by default. The Admin is responsible for ensuring they have rights to ingest any content they crawl — surfaced as an in-UI acknowledgement checkbox.

---

### 3.4 Escalation Contact

The human the Luciel pings when it is stuck or when the situation requires a person.

**Per-instance config:**
- **Primary escalation:** phone number, email, or Slack handle.
- **Secondary escalation:** (optional fallback).
- **Escalation triggers (multi-select):**
  - "When customer explicitly asks for a human"
  - "When the customer is frustrated" (sentiment-based)
  - "When the customer asks something I don't know"
  - "When the conversation exceeds N minutes / N turns"
  - "When a high-value lead is detected" (e.g. budget > $X)
  - "Never escalate, always try to answer"

**Default:** "When the customer asks something I don't know" is pre-checked. The Admin can opt out, but the platform never silently swallows an unknown question.

**The UI:** Two text fields (primary contact, secondary contact) + a multi-select for triggers.

**Backing field:** `agent_config.escalation_contact` already exists in the live schema — wiring through the UI + runtime is Arc 14 work.

---

### 3.5 Personality & Business Rules

Who the Luciel *is*, in dropdowns.

**v1 picklist axes:**
- **Tone:** Friendly / Professional / Direct / Warm
- **Verbosity:** Concise / Balanced / Thorough
- **Formality:** Casual / Business-casual / Formal
- **Pace:** Quick replies / Deliberate, considered replies
- **Persona name:** Free text, 1 field (e.g. "Sarah's Listing Helper")

**One optional free-text field** (capped at 280 chars): "Anything specific about your business this Luciel needs to know?" — for the edge case where dropdowns are not enough. The hard character cap keeps it tweet-sized so customers do not write essays.

**Behind the scenes**, these picklist selections compose into the system prompt — the customer never sees the prompt. The free-text field gets appended as a "special instructions" stanza.

**Backing field:** `agent_config.system_prompt_additions` already exists in the live schema — repurposed as the optional 280-char field. The picklist-derived prompt is built at runtime.

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

**Default rules:**
- **Inbound channel = default outbound channel.** A customer who texts gets a text back.
- **Override conditions:**
  - If a long answer is needed and inbound was SMS → switch to email (with customer permission) for the long version.
  - If urgent (lead detected) and inbound was email → switch to SMS / call for immediate follow-up.
  - If the customer asks for a callback → use voice channel (when available).
- **Customer-initiated channel switch always wins.** If the customer says "email me," the Luciel emails.

**Outbound posture (v1):** **Reactive only.** Luciel only responds to inbound. Proactive nurture outbound (e.g. "haven't heard from you in a week, here's a follow-up") is deferred to v2.

### 4.3 Sibling-Luciel Delegation

If `composition_enabled` and the Luciel's tools include `call_sibling_luciel`:
- The Luciel can ask another Luciel a question (e.g. listing Luciel asks brand-guide Luciel "what is our company's stance on lowball offers?")
- Bounded by `max_composition_depth` (Pro: 2; Enterprise: unlimited).
- Audited end-to-end.

### 4.4 Escalation Logic

If any of the configured escalation triggers fire:
- Pause the conversation (or continue in degraded mode).
- Notify the escalation contact via their preferred channel.
- Include a one-paragraph summary + a deep-link to the full conversation in the dashboard.
- Optionally wait for human response before continuing (or hand off entirely).

---

## 5. Security & Isolation Boundaries (Non-Negotiable)

These are the **four leakage walls** the platform must enforce, always, on every read and write.

### 5.1 Cross-Admin Isolation (Tenant Isolation)

**Definition:** Admin A's data never reaches Admin B under any code path, including bugs, including admin-level mistakes.

**Mechanism:**
- Every customer-data table has `admin_id` (or `tenant_id`) as a non-null indexed column.
- Every query in the service layer filters by the authenticated admin's ID.
- **PostgreSQL Row-Level Security (RLS)** policies fail-closed if the `admin_id` filter is missing from a query. Defense-in-depth so even a buggy query cannot return another admin's row.

**Status today:** Tenant-id columns exist on most tables. RLS posture: **needs audit** (Arc 9 work — the first arc post-vision-lock).

### 5.2 Cross-Team Isolation (Within an Admin, Pro + Enterprise Only)

**Definition:** If an Admin has 25 team members (Pro) or unlimited (Enterprise), some seats may have access to only a subset of instances/data.

**Mechanism:**
- Role + scope assignment table (`scope_assignment` already exists in the live schema).
- Every query checks both `admin_id` AND `scope_assignment` for the authenticated user.
- Roles (v1): `admin_owner`, `admin_manager`, `instance_operator`, `read_only_viewer`.

**Status today:** Table exists. Role catalog + UI surface: Arc 10–11 work.

### 5.3 Cross-Instance Isolation (Within an Admin)

**Definition:** Listing A's Luciel cannot see Listing B's conversations, leads, or knowledge — unless composition explicitly grants it and the depth is within tier limits.

**Mechanism:**
- Every conversation, lead, knowledge embedding has `instance_id` as a non-null column.
- Runtime retrieval filters by `instance_id` by default.
- Composition grants (when enabled) are explicit, audited, and bounded by `max_composition_depth`.

**Status today:** `luciel_instance_id` exists on `knowledge`. Verification on `conversation`, `message`, `memory`, `trace`: Arc 9 audit work.

### 5.4 Cross-Lead Isolation (Within an Instance)

**Definition:** Lead #1's conversation history does not bleed into Lead #2's session, even though both talked to the same Luciel.

**Mechanism:**
- `session_id` scoping on every message + memory entry.
- Conversational memory is per-session by default.
- Cross-session pattern learning is **out of scope at v1** — strict per-session isolation. If we later introduce cross-session learning (e.g. for Enterprise analytics), it goes through an explicit anonymization pipeline and is gated behind an opt-in flag.

**Status today:** `session` + `conversation` + `message` tables exist. Pipeline review: Arc 9 audit work.

---

## 6. The Lifecycle (Activation, Deactivation, Cap Reclamation)

Every entity (Admin account, team member, instance) has a clean activation → active → deactivation → (optional) reactivation → (eventual) hard-delete lifecycle.

### 6.1 Instance Deactivation

**Trigger:** Admin clicks "Deactivate" in the UI.

**Effects:**
- `instances.active = false` (already exists in schema).
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
- The seat **frees a slot against `seat_cap`** — Admin can invite a replacement.
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

**Optional middle ground:** A "Pause Subscription" path may be added in a later arc — subscription paused, instances frozen, data preserved indefinitely until they un-pause. Out of scope at v1; revisit after Arc 10.

### 6.4 Reactivation

- **Instance:** Admin clicks "Reactivate" within 30 days → knowledge restored, embed keys re-minted (new keys, old keys stay revoked), capacity slot consumed again.
- **Account:** Log in within 30 days → resubscribe → full restore.
- **Team member:** Re-invite (treated as new user; old data not auto-restored).

### 6.5 Hard Delete

- After grace windows expire.
- Audit log + minimal compliance record retained per legal requirements.
- The deletion itself is logged into the cold-storage audit chain.

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

**Knowledge quota defaults (v1, in MB total per Admin):**
- Free: 100 MB total
- Pro: 5 GB total
- Enterprise: unlimited

**Pro pricing:** Locked at a later decision point (§10). Range under consideration: $99–$299/mo with ~28% annual discount (mirroring Enterprise's monthly-vs-annual structure).

---

## 8. Where We Are Today (Honest Status, 2026-05-24)

### 8.1 Shipped & Production (Arcs 1–8)

- Three-tier entitlement matrix, founder-locked
- Multi-tenant Postgres schema with `admin_id` / `tenant_id` scoping on most tables
- Stripe Live billing (Pro placeholder + Enterprise $2,800/$24,000)
- Chat widget channel (the *only* channel today)
- Embed key minting, rotation, revocation
- Rate-limit composition (per-admin / per-instance / per-key)
- Signup fraud gate (1-per-IP + hCaptcha)
- `/ready` + `/health` + smoke probe
- Audit log infrastructure (`admin_audit_log`)
- `agent_config` table with `escalation_contact`, `system_prompt_additions`, `policy_overrides`, `preferred_provider` columns — **the bones of personality + escalation are already in the DB**
- `knowledge` table with vector embedding column + instance scoping — **the bones of the vector KB are already in the DB**
- `scope_assignment` table — **the bones of team isolation are already in the DB**
- `instances.active` field — **the bones of deactivation are already in the DB**

### 8.2 Partially Shipped

- **Instance composition** — entitlement allows it; runtime wiring not verified.
- **Tenant isolation** — column-level scoping exists; RLS posture not audited.
- **Knowledge ingestion** — table exists; upload/crawl/embed pipeline not built.
- **Escalation field** — column exists; UI + runtime trigger not built.

### 8.3 Not Yet Shipped (The Real Vision Gaps)

- Non-widget channels (email, SMS, voice, WhatsApp)
- Tool registry + tool catalog
- Knowledge ingestion pipeline (upload UI, crawler, embedding worker, retrieval at runtime)
- Graph knowledge store
- Dropdown-driven personality config UI
- Agentic runtime loop (plan → tool → reflect)
- Channel arbitration logic
- Deactivation UI + cap-reclamation logic
- Team member management UI
- Account closure flow
- RLS hardening

### 8.4 The Good News

**More of the foundation is already in the schema than we initially thought.** `agent_config`, `knowledge`, `scope_assignment`, `instances.active`, `escalation_contact` all exist as columns. We are not starting from zero on the data model — we are starting from "data model exists, runtime + UI need to be built."

---

## 9. Approved Roadmap (Arc 9 → Arc 16)

| Arc | Theme | Duration | Why this order |
|---|---|---|---|
| **Arc 9** | **Tenant isolation audit + RLS hardening** | 1–2 weeks | The foundation. Before customers ingest knowledge, the four walls must be proven. |
| **Arc 10** | **Deactivation lifecycle (instance + team + account) + cap reclamation + UI** | 2–3 weeks | P0 vision gap. Today an Admin cannot deactivate cleanly. Must ship before customers depend on us. |
| **Arc 11** | **Knowledge base v1 (pgvector ingestion + retrieval, no graph yet)** | 3–4 weeks | Largest single quality lever. Stops hallucination. |
| **Arc 12** | **Tool registry + v1 tool catalog + sibling-Luciel composition runtime** | 3–4 weeks | Unlocks the "Luciel as employee" frame. |
| **Arc 13** | **Channels: email + SMS adapters (voice deferred to Arc 14b)** | 2–3 weeks | First non-widget channels. Email is easy; SMS is medium (Twilio integration). |
| **Arc 14** | **Agentic runtime + channel arbitration + escalation triggers** | 3–4 weeks | The intelligence layer. Ties knowledge + tools + channels together. |
| **Arc 14b** | **Voice channel (Twilio Voice + STT + TTS)** | 3–4 weeks | Deferred / on-demand — ship when real-estate customers explicitly ask. |
| **Arc 15** | **Configuration UX rewrite (dropdown-driven, minimalist)** | 2–3 weeks | The customer-facing polish. After the platform underneath is real, we make it beautiful. |
| **Arc 16** | **Graph knowledge store + hybrid retrieval (v2 KB)** | 3–4 weeks | After we validate vector-only KB works in production. |

**Total: ~5–6 months of focused execution to ship the full vision.**

**Live E2E posture:** Run E2E **after Arc 10 closes** — that is the first arc that puts a customer-honest product on the table. The current Arc 8 E2E plan becomes a **regression test** we run between every arc.

**Early-access customer posture:** Open early-access after Arc 11 (KB) — that is the first arc where Luciel becomes genuinely useful versus what it is today.

---

## 10. Open Decisions (To Be Locked Before the Arcs That Need Them)

These are decisions the founder still needs to lock. None block Arc 9 (tenant isolation audit). Each is tagged with the arc by which it must be resolved.

| # | Decision | Default if no decision by deadline | Must-decide-by Arc |
|---|---|---|---|
| 1 | **Pro pricing** ($/mo + annual rate) | $149/mo or $1,432/yr (~20% annual discount) | Arc 12 |
| 2 | **Telephony vendor** when we get to voice | Twilio | Arc 14b |
| 3 | **Account closure: hard 30-day grace, or add a "pause" middle ground?** | Hard 30-day only at v1; revisit post Arc 10 | Arc 10 |
| 4 | **Free tier tool access**: just `capture_lead` + `transfer_to_human`, or more? | Just those two | Arc 12 |
| 5 | **Customer onboarding flow**: pure self-serve, or CSM-led kickoff for Enterprise? | Self-serve + Enterprise CSM kickoff call | Arc 11 |

---

## 11. Doctrine Anchors

This vision document is the canonical reference. The following downstream artifacts must remain consistent with it; when they diverge, this document wins:

- `app/policy/entitlements.py` — tier entitlement matrix
- `docs/CANONICAL_RECAP.md` — running history of doctrine commits
- `docs/DRIFTS.md` — drift register
- `arc8-out/arc8-commit6-e2e-test-plan.md` — E2E test plan (Arc 8 regression scope)
- All future arc plans + deploy records

**Amendment process:** Any change to this vision is a `VISION_v2` revision — never an in-place edit. v1 is preserved in git history as the founder-approved baseline.

---

**Document end.**

**Status:** FINAL — Founder-approved 2026-05-24.
**Next action:** Begin Arc 9 — Tenant Isolation Audit + RLS Hardening.
