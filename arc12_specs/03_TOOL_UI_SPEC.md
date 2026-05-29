# Arc 12 — Tool UI Spec (website repo: aryanonline/Luciel-Website)

Vite + React + TypeScript + shadcn/ui + Tailwind. Existing admin surfaces under `src/components/admin`. This builds the Tool configuration surface on the 5-pillar instance-config screen + the sibling-grant authoring UI. Architecture §3.3 + the Arc 12 brief item 5.

## Backend contracts to build against (already shipped on the backend; base = `/api/v1`)

### Tool authorization API (per instance)
- `GET /api/v1/admin/instances/{instance_id}/tools` → `ToolListResponse`:
  ```
  { instance_id: int, admin_id: str, admin_tier: "free"|"pro"|"enterprise",
    tools: ToolView[] }
  ToolView = { tool_id, display_name, description,
    requires_tier: string[], requires_channels: string[],
    execution_mode: "in_process"|"subprocess",
    authorized: bool, authorization_id: int|null, authorized_at, authorized_by_user_id,
    tier_available: bool, channels_available: bool }
  ```
  Returns the 8 v1 catalog tools: book_appointment, send_email, send_sms, lookup_property, schedule_callback, push_to_crm, call_sibling_luciel, bring_your_own_webhook. Cognition (escalate/save_memory/summarize) is NOT here by design.
- `POST /api/v1/admin/instances/{instance_id}/tools/{tool_id}/authorize` → `ToolAuthorizationRead` (201 first time / 200 if already live).
- `POST /api/v1/admin/instances/{instance_id}/tools/{tool_id}/revoke` → `ToolAuthorizationRead` (200; 404 if no live row).
- 403 if tier-locked (tool's requires_tier excludes admin_tier) or role not in {admin_owner, admin_manager}.

### Sibling-grant authoring API
- `POST /api/v1/admin/sibling-grants` body `{ caller_instance_id:int, callee_instance_id:int }` → `SiblingGrantRead`.
- `GET /api/v1/admin/sibling-grants` → `{ grants: SiblingGrantRead[] }`.
- `POST /api/v1/admin/sibling-grants/{grant_id}/approve|reject|revoke` → `SiblingGrantRead`.
  ```
  SiblingGrantRead = { grant_id, admin_id, caller_instance_id, callee_instance_id,
    approval_state: "live"|"pending_approval"|"revoked",
    granted_by_user_id, granted_at, approved_by_user_id, approved_at,
    revoked_at, created_at, updated_at }
  ```
- Backend enforces Wall-2 (author must have scope on BOTH instances → 403 otherwise), tier (Free rejected, Pro→live, Enterprise→pending_approval), and approval (admin_owner approves).

## UI deliverables

### A. Tool configuration surface (on the 5-pillar instance config screen) — Architecture §3.3 item 5
TWO visually distinct bands:
1. **Built-in cognition band** — display-only, NO checkboxes. List the cognition behaviors with labels + the one-line explanation verbatim: "Every Luciel does these. There is nothing to enable — it is how Luciel works." (These are NOT fetched from the tool API — they're static copy; cognition is always-on, Decision #20.) Show e.g. lead capture, transcription, summarization, escalation, live human handoff.
2. **Add-on tools band** — one checkbox per tool from the GET response, each with its one-sentence `description`. Checkbox state = `authorized`. Toggling on → POST authorize; off → POST revoke (optimistic UI with rollback on error). Tier-locked tools (`tier_available=false`) shown GREYED OUT with an "Upgrade to {Tier}" label (derive {Tier} from the tool's `requires_tier` minus the admin's tier — show the lowest tier that unlocks it). Tools whose channel isn't connected (`channels_available=false`, e.g. send_email/send_sms pre-Arc-13) shown with a "Channel not connected" affordance (disabled or annotated — do not allow authorize if backend will 403; surface the state truthfully).

### B. Sibling-grant authoring UI — Architecture §3.3.4 + brief item 5
A directed-grant editor (simple list UI is acceptable; a directed-graph editor is a nice-to-have). The user authors (caller → callee) grant pairs.
- CRITICAL scope rule: the instance dropdowns must ONLY show instances the authenticated user has scope on. The backend enforces Wall-2 and will 403 a cross-scope grant — the UI must NOT offer instances the user can't author for (fetch the user's scoped instances from the existing instances/admin API the dashboard already uses; match how the current admin UI lists instances).
- Show existing grants with approval_state (live / pending_approval / revoked) badges. Provide approve/reject (for pending) and revoke (for live) actions — gate the approve action to admin_owner in the UI (backend enforces too). Pro grants appear live immediately; Enterprise grants appear pending_approval until approved.
- Handle 403 (scope/tier) gracefully with a clear message.

## Build rules
- Match the existing website's patterns: how it does API calls (find the existing api client / fetch wrapper + auth/session handling), component structure under src/components/admin, shadcn/ui components, Tailwind tokens, routing. Do NOT introduce a new state/data-fetching library if one exists — reuse it.
- TypeScript types for all API shapes above.
- The 11 public-contract fields + 2 HTTP endpoints removed in the backend EX1c sweep (domain_id/agent_id) — if any existing website code references those removed fields/endpoints, fix it (v2 alignment). Grep the website for agent_id/domain_id usage against the backend and reconcile.
- Tests: the repo uses vitest — add component/contract tests for the two-band rendering (tier-locked greying, cognition band has no checkboxes) and the sibling-grant scope-restricted dropdown. Run `bun run test` (or npm) green.
- Build must pass: `bun run build` (vite). No type errors.

## Out of scope
- Backend changes (all backend contracts are shipped). If a contract gap is found, REPORT it — do not patch the backend from the website repo.
- The runtime guardrails (cycle detection, fan-out budget) are runtime-internal and must NOT appear in any UI (Architecture §3.3.4).
