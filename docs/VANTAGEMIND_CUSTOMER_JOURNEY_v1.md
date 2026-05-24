# VantageMind — Customer Journey v1 (Draft for Founder Review)

**Status:** DRAFT — Founder review pending
**Authored:** 2026-05-24
**Anchors to:** `docs/VANTAGEMIND_VISION_v1_FINAL.md` (canonical product vision), `docs/VANTAGEMIND_ARCHITECTURE_v1.md` (technical architecture)
**Purpose:** Walk through the **lived experience** of each customer tier — what they see, what they touch, what they feel — from the moment they hear about VantageMind through the moment they (hopefully don't) cancel. This is the **product narrative source of truth** that the marketing site, the onboarding emails, the dashboard copy, and the support docs all anchor to.

**Founder review action items** appear in `> 📝 REVIEW:` callouts. Mark them up however you want.

---

## 0. How to Read This Document

This document tells **three stories** end-to-end:

1. **Sarah — Free Tier.** Solo real-estate agent, one website, dipping a toe in. She is the proof that VantageMind is approachable without a sales call.
2. **Marcus — Pro Tier.** 12-person team at a regional brokerage. He runs the team and pays the bill. He is the proof that VantageMind scales past a single person without becoming enterprise software.
3. **RE/MAX-Regional — Enterprise Tier.** Hundreds of agents across multiple offices. Procurement, security review, MSA. She is the proof that VantageMind is defensible at audit time.

Each story walks the **same eight phases**, so you can compare them at any phase across the three personas:

| # | Phase | What happens |
|---|---|---|
| 1 | **Discovery** | How they hear about VantageMind |
| 2 | **Signup** | Account creation + payment |
| 3 | **Instance creation** | The first Luciel comes into existence |
| 4 | **Configuring the 5 pillars** | Channels, tools, knowledge, escalation, personality |
| 5 | **Embed + launch** | The Luciel goes live on their property |
| 6 | **First lead** | An end customer talks to the Luciel for the first time |
| 7 | **Steady-state operation** | Day-to-day use over months |
| 8 | **Deactivation / closure** | What happens when they stop |

The eight phases mirror the architecture's two-plane model (§ Architecture §1): phases 1–4 are **Control Plane** (admin configuring), phases 5–7 are **Data Plane** (end customer interacting), phase 8 touches both.

> 📝 REVIEW: Do the three personas cover the spread, or should we add a fourth (e.g. a mortgage brokerage, a property manager) to widen the story?

<div style="page-break-after: always;"></div>

# PART I — SARAH (Free Tier)

## Persona Snapshot

- **Name:** Sarah Chen
- **Role:** Solo real-estate agent, RE/MAX affiliate (independent)
- **Location:** Markham, ON
- **Business size:** Just her. One website, ~40 listings/year, ~8 active leads at any time.
- **Tech comfort:** Moderate. Uses Squarespace, Mailchimp, an iPhone. Has never embedded an iframe.
- **Pain:** Misses leads at night. Leads bounce when she does not reply in 5 minutes. She tried a generic "AI chatbot" tool once and the answers were so wrong she pulled it.
- **Budget for this:** $0 to start. Will pay once it proves itself.

> 📝 REVIEW: Is "$0 to start, prove value before paying" the right Free-tier promise, or should Free have a strong upgrade nudge after N days?

<div style="page-break-after: always;"></div>

## Phase 1 — Discovery

Sarah sees a LinkedIn post from another agent: "I sleep through the night now because VantageMind answers my leads after hours." She clicks through to vantagemind.com.

**What she sees on the landing page:**
- One hero line: **"Your business, answered."**
- A 20-second loop showing a property-listing site with a chat widget. A visitor asks "is 24 Oak Lane still available?" The Luciel replies in plain language, with the listing detail, and offers to book a viewing.
- One button: **"Start free — no credit card."**

She clicks it.

**Architecture surface:** Marketing site only — no auth, no API.
**Status:** ✅ LIVE (today)

<div style="page-break-after: always;"></div>

## Phase 2 — Signup

Sarah lands on `/signup`. She fills in:

- Email
- Password
- A captcha (one click — invisible recaptcha)

She clicks **Create account**. She gets a verification email within 10 seconds. She clicks the link. She is now signed in.

The dashboard opens to a single welcome card:

> **"Hi Sarah. Let's get your first Luciel running. It takes about 3 minutes."**
> [ Create my first Luciel ]

**No payment requested.** No "add a credit card to start the trial." She has not paid a cent and she is in.

**Architecture surface:**
- Admin record created in `admins` table with `tier='free'`
- Subscription record stub created with `status='active'` and no Stripe customer attached
- Audit log entry: `admin.created`, `tier_assigned: free`
- Verification email sent via SES

**Status:** ✅ LIVE (today)

> 📝 REVIEW: Should signup ask for "what's your business?" upfront to pre-fill the persona picklist later, or do we keep signup truly frictionless and ask later?

<div style="page-break-after: always;"></div>

## Phase 3 — Instance Creation

Sarah clicks **Create my first Luciel**. A short form opens.

**What she fills in:**
- **What should we call your Luciel?** → "Sarah's Listing Assistant"
- **What is the website it will live on?** → `sarahchen.realtor`
- **In one sentence, what is your business?** → "I sell residential homes in the GTA, mostly Markham and Richmond Hill."

She clicks **Create**. A progress strip animates for about 4 seconds while the system:
1. Inserts a row in `instances` with `admin_id=Sarah, name='Sarah's Listing Assistant', active=true`
2. Inserts a default `agent_config` row pre-filled with Free-tier defaults
3. Mints an `embed_key` and stores it
4. Emits `instance.created` to `admin_audit_log`

She is dropped into the **5-pillar configuration screen**.

**Architecture surface:**
- `instances` (Control Plane)
- `agent_config` (Control Plane — the 5 pillars live here)
- `embed_keys` table (Control Plane — Data Plane will authenticate by these)

**Status:** ✅ LIVE for the data writes; UI 🟨 SCAFFOLDED (needs polish in Arc 10).

<div style="page-break-after: always;"></div>

## Phase 4 — Configuring the 5 Pillars (Free Tier)

This screen is the **single most important UX in the product** for Sarah. On the Free tier, it is intentionally narrow — most decisions are made for her.

### 4.1 Channels
She sees a list with one item enabled:
- ✅ **Website chat widget** — On
- 🔒 Email — *Upgrade to Pro to enable*
- 🔒 SMS — *Upgrade to Pro*
- 🔒 Voice — *Coming soon*

She moves on.

### 4.2 Tools
She sees three tools:
- ✅ **Save conversation summary** — On
- ✅ **Escalate to me** — On
- ⬜ **Book a viewing** — Off (requires calendar — Pro)

She leaves it.

### 4.3 Knowledge
She sees: **"Upload anything you want your Luciel to know about."**
- A drag-and-drop area
- **Free tier limits:** 10 MB per file, 100 MB total
- A progress meter: 0 / 100 MB

She drops in:
- Her listing brochure PDF (2 MB)
- A "FAQ about working with Sarah" document (40 KB)
- A CSV of her current active listings (300 KB)

Each file uploads, gets parsed (PDF → text → chunks → embeddings via pgvector), and shows ✅ next to it within ~30 seconds.

### 4.4 Escalation contact
A simple form:
- **Phone:** her cell
- **Email:** her email
- **Escalation triggers:** ✅ Hot lead (default) ✅ Frustrated customer (default) ✅ Compliance question (default)

### 4.5 Personality
A dropdown with named presets:
- **Warm Concierge** ← she picks this
- Professional Advisor
- Friendly Expert
- Trusted Authority
- (others Pro/Enterprise only)

She clicks **Save and go to embed**.

**Architecture surface:**
- KB upload: `knowledge_documents`, `knowledge_chunks`, `knowledge_embeddings` rows written
- Pillar config: `agent_config` columns updated (`escalation_contact`, `system_prompt_additions` derived from personality preset, `policy_overrides` left default)
- Tier caps enforced at the API boundary by `app/policy/entitlements.py`

**Status:**
- Pillar config writes ✅ LIVE
- KB ingestion pipeline 🟨 SCAFFOLDED (Arc 11)
- Personality picklist UI 🔵 DESIGNED (Arc 11)
- Tool selection UI 🔵 DESIGNED (Arc 12)

> 📝 REVIEW: On Free, do we want the dropdown to show locked Pro/Enterprise personality options as upgrade nudges, or hide them entirely?

<div style="page-break-after: always;"></div>

## Phase 5 — Embed + Launch

A new screen shows:

> **"Your Luciel is ready. Add this one line to your website."**
>
> ```html
> <script src="https://embed.vantagemind.com/v1/luciel.js"
>         data-key="vm_live_a8f3c2..."></script>
> ```
>
> [ Copy ]   [ Email this to my web person ]   [ Test it here ]

Sarah hits **Test it here**. A preview opens — her own website is rendered in an iframe with the widget already injected. She types: *"is 24 Oak Lane still available?"* The Luciel reads her CSV, replies *"Yes, 24 Oak Lane is still listed at $899,000. Would you like to book a viewing?"*

She emails the line to her web person. By tomorrow morning, the widget is live on `sarahchen.realtor`.

**Architecture surface:**
- `embed.vantagemind.com` — a static CDN-fronted JS file
- The `data-key` authenticates every widget request to `/api/v1/chat-widget/*`
- Strict per-session isolation enforced (Vision §5)

**Status:** ✅ LIVE (today)

<div style="page-break-after: always;"></div>

## Phase 6 — First Lead

Tuesday night, 11:42 PM. A visitor on `sarahchen.realtor` opens the widget.

> **Visitor:** "Hi, are you open? I saw the Oak Lane house — what are the property taxes?"
> **Sarah's Luciel:** "Hi — I'm Sarah Chen's listings assistant. Yes, I'm always on. The annual property tax on 24 Oak Lane is approximately $4,820 based on the 2025 assessment. Would you like me to book you a viewing, or have Sarah call you tomorrow morning?"
> **Visitor:** "Tomorrow morning is great. Here's my number: 416-555-0143."

The Luciel:
1. Calls the `save_conversation_summary` tool — writes a `leads` row with name, phone, property of interest, intent: "viewing request, callback morning"
2. Triggers `escalate_to_admin` — Sarah gets an SMS and an email *"New hot lead — 24 Oak Lane, callback 9 AM"*
3. Replies to the visitor: "Got it. Sarah will call you at 416-555-0143 tomorrow morning. Have a good night."

Sarah wakes up to the SMS. She calls at 9 AM. She sells the house.

**Architecture surface:**
- `conversations` + `conversation_messages` (Data Plane)
- `leads` (Data Plane)
- Tool invocations traced in `trace` table
- Escalation goes through the channel arbiter → SMS via Twilio webhook + email via SES

**Status:**
- Widget conversation ✅ LIVE
- Tools (save_summary, escalate) 🟨 SCAFFOLDED (live wiring in Arc 12)
- SMS / email outbound 🔵 Arc 13

<div style="page-break-after: always;"></div>

## Phase 7 — Steady-State (Free)

Over the next three months Sarah:
- Gets 60–80 widget conversations a month
- Sees ~12 leads/month captured automatically
- Gets escalated about 4 times a month — all real hot leads
- Hits the 100 MB knowledge cap once (after uploading a few neighborhood guides) — sees a "Upgrade to Pro for 5 GB" banner in the dashboard
- Hits the 500-conversations/month soft cap once — sees the same nudge

She does not upgrade. She does not need to. Free is genuinely useful.

**That is the point.** Free is not a teaser — it is the proof that we ship value first.

**Architecture surface:**
- Tier-cap enforcement (`entitlements.py`)
- Soft-cap banner served via `/api/v1/dashboard/usage`
- Retention worker deletes conversation transcripts after 30 days on Free (Vision §3.4)

**Status:** ✅ LIVE (caps + retention); banner 🔵 Arc 11

<div style="page-break-after: always;"></div>

## Phase 8 — Deactivation / Closure (Free)

Six months in, Sarah decides to take a sabbatical for the summer. She logs into the dashboard.

**Three options under "Manage account":**
1. **Pause my Luciel** — instance set to `active=false`. The widget on her site renders an empty `<div>` (no error, no broken UI). Data retained. Reactivatable instantly.
2. **Delete this instance** — the instance row is soft-deleted. KB + conversations enter a 30-day grace window, then hard-deleted.
3. **Close my account** — all instances soft-deleted. 30-day grace, then hard-delete everything. Email confirmation required.

She picks **Pause**. The widget goes quiet on her site. She comes back in October, hits **Resume**, and the Luciel is back exactly as she left it — same KB, same persona, same escalation contact.

**Architecture surface:**
- `instances.active` ✅ LIVE
- Account-closure flow 🔵 Arc 10
- 30-day grace + hard-delete worker 🔵 Arc 10

> 📝 REVIEW: On Free, do we even offer "pause" as a distinct verb, or is "pause" just "delete that you can restore within 30 days"? Simpler model, fewer UI states.

<div style="page-break-after: always;"></div>

# PART II — MARCUS (Pro Tier)

## Persona Snapshot

- **Name:** Marcus Boateng
- **Role:** Team lead at a regional brokerage in the GTA. He owns the brokerage's tech stack.
- **Team:** 12 agents. Mix of full-time and part-time. Two admin staff.
- **Business size:** ~600 listings/year across the team. ~120 active leads at any moment.
- **Tech comfort:** High. Runs HubSpot, has integrated Twilio for SMS himself, knows what an iframe is.
- **Pain:** Leads slip through the cracks during evening hours and weekends. His agents have wildly different response times. His top agent reps and his newest reps give wildly different first impressions to leads.
- **Budget:** Has $200–$400/month available for this category. Has used Drift and Intercom before; left both because they did not understand real estate.

<div style="page-break-after: always;"></div>

## Phase 1 — Discovery (Pro)

Marcus finds VantageMind through a Reddit post in `r/realtors`. He spends 20 minutes on the marketing site. He watches the demo. He clicks **See how Pro works**.

He sees a **comparison page** with three columns (Free / Pro / Enterprise) and a clear narrative for Pro:

> **Pro — "For teams. One Luciel per agent. Or one shared, with channel routing."**
> $149/mo or $1,432/yr (saves ~20%)
> Up to 5 instances · 5 GB knowledge · email + SMS · escalation routing per instance

He signs up.

**Architecture surface:** Marketing site only.
**Status:** ✅ LIVE; Pro pricing page copy 🔵 Arc 11.

<div style="page-break-after: always;"></div>

## Phase 2 — Signup + Upgrade (Pro)

Marcus signs up the same way Sarah did — same flow, $0, no card. He lands on the dashboard.

A banner is visible: **"You're on Free. Upgrade to Pro to unlock email/SMS + team + 5 instances."**

He clicks **Upgrade to Pro**. He is taken to Stripe Checkout.
- **Plan:** Pro Monthly ($149) — toggle to Annual ($1,432, saves $356)
- He picks annual.
- Stripe Checkout collects card. He pays.

He is redirected back. The dashboard now reads **"Pro"** in the top-right. The banner is replaced with a checklist:

- ✅ Pro activated
- ⬜ Invite your team
- ⬜ Create your first Luciel
- ⬜ Connect email or SMS

**Architecture surface:**
- Stripe webhook fires (`checkout.session.completed` → `subscription.created` → tier upgrade)
- `subscriptions` row updated, `admins.tier='pro'`
- Pre-mint email validation (Arc 8 C2 — already shipped) catches bad emails before Stripe ever issues a receipt
- `admin_audit_log` entry: `tier_upgraded: free → pro`

**Status:** ✅ LIVE.

<div style="page-break-after: always;"></div>

## Phase 3 — Instance Creation (Pro)

Marcus has a strategic decision to make: **one shared Luciel for the brokerage, or one per agent?**

The dashboard offers a hint:

> **"Most Pro teams start with one shared Luciel for the brokerage's main site, then add a second per top-performing agent. You can have up to 5 instances on Pro."**

He picks **one shared** for now.

The instance-creation form has more fields than Sarah's:
- **Name:** "GTA Premier Realty Concierge"
- **Website:** `gtapremier.com`
- **Business description:** he writes a paragraph
- **Lead routing:** ← new on Pro

Lead routing presents a dropdown:
- Round-robin across all agents
- Geographic (by lead's neighborhood)
- Specialty match (by listing type)
- All leads to single contact

He picks **Geographic**. A sub-form opens letting him map postal-code prefixes to specific team members.

Click **Create**.

**Architecture surface:**
- `instances` row (Pro tier allows up to 5)
- `scope_assignment` table — already exists (Vision §4 ; routing rule rows go here)
- Tier-cap (max 5 instances) enforced by `entitlements.py`

**Status:**
- Instance create ✅ LIVE
- `scope_assignment` table ✅ LIVE (already exists)
- Routing-rule UI 🔵 Arc 12

<div style="page-break-after: always;"></div>

## Phase 4 — Configuring the 5 Pillars (Pro)

Same 5-pillar screen as Sarah's, but more options unlocked.

### 4.1 Channels
- ✅ Website chat widget
- ✅ Email — Marcus connects `concierge@gtapremier.com` (forwards via webhook)
- ✅ SMS — Marcus connects a Twilio number he already owns
- 🔒 Voice — Coming soon

### 4.2 Tools
- ✅ Save conversation summary
- ✅ Escalate
- ✅ **Book a viewing** — connects to the team's Google Calendar (round-robin to whoever is free)
- ✅ **Send listing details** — pulls structured data from the KB
- ⬜ **Send mortgage pre-qual link** — off; Marcus will turn on later

### 4.3 Knowledge
- **Pro caps:** 50 MB per file, 5 GB total
- Marcus uploads:
  - The team's full active-listings export (15 MB)
  - The brokerage's policies + FAQ document (2 MB)
  - Recent sold-comparables for each neighborhood (40 MB)
  - **Crawl this URL:** he points it at the team's public listings site — VantageMind crawls and indexes (Pro feature)

### 4.4 Escalation contact (Pro = routing rules)
This is where Pro gets meaningfully more sophisticated:
- Hot leads → route per Phase-3 geographic map
- Compliance questions → always to Marcus
- After-hours → also CC Marcus
- Frustrated customer → page Marcus by SMS

### 4.5 Personality
Full picklist available on Pro:
- Warm Concierge
- Professional Advisor
- Friendly Expert
- Trusted Authority
- **Custom** ← Pro can pick this, write 2–3 sentences of additional voice guidance

He clicks **Save**.

**Architecture surface:**
- Channel connectors (email webhook, SMS webhook) 🔵 Arc 13
- Tool registry (book viewing = calendar tool) 🟨 SCAFFOLDED → Arc 12
- KB ingestion w/ crawler 🔵 Arc 11
- Routing rules: `scope_assignment` ✅ LIVE
- Custom personality writes to `agent_config.system_prompt_additions` ✅ LIVE column

**Status:**
- Schema all in place
- UI for connecting Twilio + Gmail 🔵 Arc 13
- KB crawler 🔵 Arc 11

> 📝 REVIEW: Should "Custom personality" be free-text on Pro, or a constrained set of structured fields (tone slider, formality slider, etc.) for safety?

<div style="page-break-after: always;"></div>

## Phase 5 — Embed + Launch (Pro)

Marcus gets the embed snippet — same one as Sarah's, with his key. He has it on the brokerage site within an hour.

He also gets:
- **Inbound email address:** `concierge-gtapremier@inbound.vantagemind.com` — leads who email this address (or who forward emails to it) get a Luciel reply
- **SMS routing:** any inbound SMS to his Twilio number gets a Luciel reply
- **Team invite link:** he sends it to his 12 agents

His agents sign in (they get a team-member-scoped login — they can see the team's leads and conversations but cannot change configuration).

**Architecture surface:**
- `admin_team_members` (already exists)
- Team-member roles enforced in admin API middleware
- Inbound channel webhooks 🔵 Arc 13

**Status:**
- Team-member auth ✅ LIVE
- Channel webhooks 🔵 Arc 13

<div style="page-break-after: always;"></div>

## Phase 6 — First Lead (Pro)

Saturday morning, 8:30 AM. A visitor on `gtapremier.com` from Markham (M1B postal prefix) starts a chat.

> **Visitor:** "I'm looking for a 4-bedroom under $1.2M in north Markham."
> **GTA Premier Luciel:** "Great — there are 7 active listings matching that brief. The top 3 by recent buyer interest are: [shows 3 cards]. Want me to book a viewing for any of these?"
> **Visitor:** "Yes, the second one — Saturday at 2 PM?"
> **Luciel:** Calls the `book_viewing` tool. Calendar tool returns: "Agent Priya Singh available 2 PM Saturday." Luciel responds: "Booked — Priya Singh will meet you at 47 Maple Crescent at 2 PM. She'll text you the address shortly. Anything else?"

Simultaneously:
- A `leads` row is written, assigned to Priya by the routing rule
- An SMS goes to Priya's phone: "New booking — 47 Maple Cr, 2 PM Sat, visitor name + number attached"
- Marcus sees it in the dashboard's live feed

**Architecture surface:**
- Channel: widget
- Tool: `book_viewing` invokes a Google Calendar adapter
- Routing: postal-prefix → agent (resolved through `scope_assignment`)
- Outbound: SMS via Twilio for the assigned agent

**Status:**
- Tool registry + invocation 🟨 SCAFFOLDED, full wiring Arc 12
- Calendar adapter 🔵 Arc 12
- Routing resolution 🔵 Arc 12

<div style="page-break-after: always;"></div>

## Phase 7 — Steady-State (Pro)

Over six months Marcus's team experiences:
- ~3,000 conversations/month, ~600 leads captured
- ~85 viewings booked autonomously
- Dashboard analytics show conversion by source, by agent, by listing type
- One agent leaves the team — Marcus removes her from the team-member list; her access dies; the leads she was assigned to remain (re-routed)
- The team hits 4 GB of knowledge — sees a nudge but is not capped
- Marcus adds a second instance for the team's commercial-listings vertical

He is paying $1,432/year. He estimates the Luciel is saving him 40 hours of agent time per month and capturing leads he would otherwise have lost. His ROI math is comfortable.

**Architecture surface:**
- Multi-instance support ✅ LIVE
- Team member lifecycle (add/remove) ✅ LIVE
- Dashboard analytics — basic ✅ LIVE; richer Arc 11
- Tier soft-cap nudges 🔵 Arc 11

> 📝 REVIEW: On Pro, do we want "agent leaves the team" to trigger an automated reassignment workflow, or is "leads remain orphaned until Marcus reassigns" acceptable at v1?

<div style="page-break-after: always;"></div>

## Phase 8 — Deactivation / Closure (Pro)

Pro deactivation is the same shape as Free but with more rows touching the data plane. The three options exist (pause instance, delete instance, close account). The 30-day grace window applies.

One Pro-specific path: **downgrade to Free**. If Marcus chooses this:
- His subscription is canceled at the end of the billing period
- At the moment of downgrade, he is **already** over Free caps (12 GB of KB, 5 instances)
- The system enters a **read-only grace window** (30 days): existing Luciels keep running, but he cannot create new instances or upload new KB
- He gets nudges to either re-upgrade or trim down to Free caps
- At day 30, the system enforces caps: oldest instances over the cap go inactive; oldest KB documents over the cap are archived (not deleted) until he upgrades again

**Architecture surface:**
- Tier downgrade workflow 🔵 Arc 10
- Read-only grace window logic 🔵 Arc 10
- Archive (not delete) on downgrade 🔵 Arc 10

> 📝 REVIEW: On downgrade-to-Free, do we archive over-cap data (recoverable on re-upgrade) or hard-delete it after 30 days? Archive is friendlier; hard-delete is simpler.

<div style="page-break-after: always;"></div>

# PART III — RE/MAX-REGIONAL (Enterprise Tier)

## Persona Snapshot

- **Org:** RE/MAX-Regional (anonymized example) — a regional master franchise
- **Decision-maker:** **Dana Ortega**, VP of Operations
- **Buying committee:** Dana + CIO + General Counsel + IT Security
- **Scale:** 14 offices, ~280 agents, ~6,000 active listings, ~25,000 leads/year
- **Tech stack:** Salesforce CRM, Zendesk for support, custom listings portal, SSO via Okta
- **Pain:** Inconsistent lead handling across offices. Compliance audit trail required by their franchise agreement. Each office wants its own configuration but the brand needs to enforce minimum standards.
- **Procurement reality:** Will sign an MSA. Will run a 30-day security review. Will not click "Start free" on a website.
- **Budget:** $2,800/mo or $24,000/yr is comfortable.

<div style="page-break-after: always;"></div>

## Phase 1 — Discovery (Enterprise)

Dana does **not** find VantageMind on a Reddit post. She finds VantageMind one of three ways:
1. A founder-led outbound conversation
2. A referral from a Pro customer who grew up
3. An RFP that we are invited into

She is not asked to start a free trial. She is invited to a **30-minute discovery call**, after which she gets:
- A tailored deck (founder-built — the company is small enough)
- A reference customer or two
- A links to the **architecture document** and **vision document** — yes, the ones in this repo. (Polished externally first.)

**Architecture surface:** None — this is sales.
**Status:** Sales process 🔵 Arc 11 (early-access GA prep).

<div style="page-break-after: always;"></div>

## Phase 2 — Signup + Procurement (Enterprise)

Enterprise signup is a **process**, not a click. Roughly:

1. Discovery call
2. Technical-fit call (with Dana's CIO)
3. Security review (questionnaire, our SOC-2 readiness pack, architecture doc)
4. MSA + DPA review (their counsel + ours)
5. Pilot scope agreed: 2 offices, 8 weeks, single instance per office
6. Stripe Live invoice issued for the **annual plan** ($24,000) OR monthly ($2,800)
7. Once paid, the Enterprise admin record is provisioned with `tier='enterprise'`

The Stripe price IDs (locked in vision):
- Monthly: `price_1TacunRytQVRVXw71i6eCx1K`
- Annual: `price_1TacunRytQVRVXw72JTSAmmq`

**Architecture surface:**
- Enterprise tier flag exists ✅ LIVE
- Manual provisioning OK at this volume; self-serve enterprise signup deferred
- SSO (Okta SAML) 🔵 Arc 16

<div style="page-break-after: always;"></div>

## Phase 3 — Instance Creation (Enterprise)

Dana logs in. The dashboard shows an Enterprise-specific home: a **fleet view** instead of a single instance card.

She creates 2 instances (the pilot scope):
- **Markham Office Concierge** — assigned to that office's agents (12 of them)
- **Mississauga Office Concierge** — assigned to that office's agents (16 of them)

Each instance has its own configuration. Per Vision §5, **no data crosses between the two**.

Enterprise lets her:
- Define **organization-wide policies** that apply across all instances (e.g., "all Luciels must include the brokerage's RECO compliance disclaimer in initial greetings")
- Define **organization-wide branding** (logo, colors) that all instances inherit unless overridden
- Define **team-member roles** beyond just admin/member: viewer, auditor, office-manager, etc.

**Architecture surface:**
- Multi-instance ✅ LIVE
- Org-wide policies 🔵 Arc 16
- Org-wide branding 🔵 Arc 16
- Custom roles 🔵 Arc 16

> 📝 REVIEW: How many of these Enterprise org-wide features ship at GA vs. on-demand per the first enterprise customer's requirements? My instinct is "ship on demand for the first 3 enterprises."

<div style="page-break-after: always;"></div>

## Phase 4 — Configuring the 5 Pillars (Enterprise)

The 5-pillar screen looks similar to Pro's. The Enterprise differences are:

### 4.1 Channels
- Everything Pro has, plus:
- **Voice channel** when Arc 14b ships
- **Custom-domain widget** — Dana's widget can be served from `chat.remax-regional.com` (CNAME + cert)

### 4.2 Tools
- All Pro tools, plus:
- **Custom tool authoring** — Enterprise can define their own tools via a constrained config (signed webhook + JSON schema)
- **Salesforce CRM tool** — first-class adapter (most Enterprise customers want this)
- **Compliance-recording tool** — every conversation gets a structured summary written to their compliance archive

### 4.3 Knowledge
- **Unlimited storage**
- **Up to 500 MB per file**
- **Authenticated crawlers** (their internal listings portal requires auth — we accept OAuth tokens)
- **KB versioning** — every KB document has explicit versions; old versions retained 1 year

### 4.4 Escalation contact
- Same routing model as Pro, plus:
- **Escalation chains** (try agent → if no response in 5 min, try office manager → if no response, page on-call)
- **SLA tracking** — every escalation gets a deadline; SLA breaches are flagged in the audit log

### 4.5 Personality
- Full custom prompt authoring — not just a 2-sentence override, full sectional control
- **Approval workflow** — personality changes go through a documented approval (auditable)

**Architecture surface:**
- Custom domain TLS 🔵 Arc 16
- Custom tool authoring API 🔵 Arc 16
- KB versioning 🔵 Arc 16
- Escalation chains 🔵 Arc 16
- Personality approval workflow 🔵 Arc 16

<div style="page-break-after: always;"></div>

## Phase 5 — Embed + Launch (Enterprise)

The embed step is the same line of JavaScript. The differences are operational:
- Their security team reviews the JS payload
- They serve it through their own CDN (we provide an SRI hash)
- They embed on a staging site first
- They run a 1-week soft launch with 1 office before going wider
- The audit log is the artifact that proves to RECO and their franchise HQ that the system behaved correctly

**Architecture surface:**
- Subresource Integrity (SRI) hash on embed.js 🔵 Arc 16
- Sub-CDN serving 🔵 Arc 16
- Audit log export API 🔵 Arc 16

<div style="page-break-after: always;"></div>

## Phase 6 — First Lead (Enterprise)

Same shape as the Pro first-lead story, with more arms:
- The conversation is captured
- The Salesforce CRM tool writes a `Lead` record directly into their Salesforce org
- The compliance-recording tool writes a structured event to their Zendesk archive
- An audit-log row is written with full execution trace
- The 7-year retention window (Vision §3.4) begins for this row

**Architecture surface:**
- Salesforce adapter 🔵 Arc 16
- 7-year retention enforcement ✅ LIVE (retention worker, tier-conditional)
- Audit trail per conversation ✅ LIVE

<div style="page-break-after: always;"></div>

## Phase 7 — Steady-State (Enterprise)

In their first year on Enterprise, RE/MAX-Regional:
- Rolls from 2 instances → 14 (one per office)
- Adds ~3 custom tools (their listings portal, their mortgage-partner referral, their open-house RSVP)
- Generates ~250,000 conversations
- Captures ~25,000 leads
- Escalates ~1,200 times to humans, of which ~95% hit SLA
- Has one compliance audit; passes cleanly thanks to the audit log
- Renews annually

The renewal conversation is the moment of truth: did VantageMind become **load-bearing infrastructure** for them, or did it stay a nice-to-have? The architecture is designed to make it load-bearing — that is what the 4-pillar isolation guarantees, the 7-year retention, and the org-wide policies are for.

<div style="page-break-after: always;"></div>

## Phase 8 — Deactivation / Closure (Enterprise)

Enterprise closure is the **biggest gap** in the current v1 design and the most consequential.

Plausible scenarios:
- **They renew** — happy path, no action
- **They downgrade to Pro** — same read-only grace logic as Pro, but at a much larger data scale; we likely offer a guided 90-day migration instead of the standard 30-day grace
- **They cancel and want their data** — they get a structured export (JSONL of conversations, JSONL of leads, KB documents as originally uploaded, audit log as CSV). Export window: 90 days.
- **They cancel and want everything purged** — DPA-driven hard delete. Audit log entry retained that *records the deletion* but the data itself is gone. Retention window resets to 90 days for the deletion record itself.

**Architecture surface:**
- Structured export API 🔵 Arc 16
- DPA-compliant hard-delete workflow 🔵 Arc 16
- Deletion-record retention 🔵 Arc 16

> 📝 REVIEW: For Enterprise cancellation-with-purge, do we keep the audit-log entries that record customer-facing conversations (with payloads scrubbed) for legal recall, or are even the entries themselves deleted? The honest answer is "depends on the contract terms" — but I want a default.

<div style="page-break-after: always;"></div>

# Cross-Persona Comparison

A single-page cheatsheet that the marketing site and the in-app dashboard both anchor to.

| Phase | Sarah (Free) | Marcus (Pro) | Dana (Enterprise) |
|---|---|---|---|
| **Discovery** | LinkedIn post → landing page | Reddit / referral → comparison page | Outbound / RFP → founder call |
| **Signup** | $0, no card, instant | $0 then upgrade to $149/mo | MSA + procurement, $24K/yr |
| **Instance creation** | 1 instance, 3-min form | Up to 5 instances, routing rules | Fleet view, org-wide policies |
| **Channels** | Widget only | Widget + email + SMS | All channels + custom domain |
| **Tools** | Save summary + escalate | + book viewing, send details | + custom tool authoring, Salesforce |
| **Knowledge** | 100 MB total | 5 GB total, crawler | Unlimited, versioning, auth crawlers |
| **Personality** | Picklist (3 presets) | Full picklist + 2-sentence custom | Full custom prompt + approval workflow |
| **Escalation** | One contact | Per-instance routing rules | Escalation chains + SLA |
| **First lead** | Widget conversation | Multi-channel + auto-book | + CRM write + compliance archive |
| **Steady state** | 60–80 convo/mo, no caps hit | ~3K convo/mo, multi-instance | ~250K convo/mo, custom tools |
| **Audit retention** | 30 days | 1 year | 7 years |
| **Deactivation** | Pause / delete instance / close | + tier downgrade w/ grace | + structured export + DPA hard-delete |

> 📝 REVIEW: Is this cross-persona table the right marketing surface, or should it be even simpler (only 5 rows: cost / channels / KB / retention / support)?

<div style="page-break-after: always;"></div>

# Open Journey Questions (Founder Review)

These are journey decisions still pending — not architecture decisions (those are in `VANTAGEMIND_ARCHITECTURE_v1.md §8`):

| # | Question | Default proposed | Must decide by |
|---|---|---|---|
| J1 | Does Free have any upgrade nudge timing (e.g. after 30d, after Nth lead)? | Capacity-based nudge only — no time-based | Arc 11 |
| J2 | On signup, do we ask "what's your business?" upfront, or after first instance? | After — keep signup at 3 fields | Arc 11 |
| J3 | Free-tier knowledge caps: do we soft-cap (warn) or hard-cap (reject)? | Hard-cap (reject upload) — clearer mental model | Arc 11 |
| J4 | Pro custom personality: free text, or structured fields (sliders)? | Free text (2-3 sentences) at v1 | Arc 11 |
| J5 | When agent leaves a Pro team: orphan their leads or auto-reassign? | Orphan + nudge Marcus to reassign | Arc 12 |
| J6 | On Pro→Free downgrade: archive over-cap data or hard-delete after grace? | Archive (recoverable on re-upgrade) | Arc 10 |
| J7 | Enterprise org-wide features: ship at GA or per-customer on demand? | Per-customer on demand | Arc 11/16 |
| J8 | Enterprise DPA hard-delete: keep deletion-record entries or purge those too? | Keep deletion record (with payload scrubbed) | Arc 16 |

---

## Doctrine Anchors

- **Vision (canonical product source of truth):** `docs/VANTAGEMIND_VISION_v1_FINAL.md`
- **Architecture (canonical technical source of truth):** `docs/VANTAGEMIND_ARCHITECTURE_v1.md`
- **Tier policy runtime expression:** `app/policy/entitlements.py`

---

## What I Need From You (Partner)

1. **Mark up the `📝 REVIEW:` callouts** through the doc — keep, change, kill, or add nuance to each
2. **Make J1–J8 calls** — these unblock Arc 10/11 design
3. **Confirm the three personas** are the right ones, or name the fourth that's missing
4. **Approve the cross-persona table** for marketing-site reuse

Once approved I'll promote this to `VANTAGEMIND_CUSTOMER_JOURNEY_v1_FINAL.md` and commit.

— Partner
