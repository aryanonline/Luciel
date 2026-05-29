# Arc 12 — `agent_id` / `domain_id` Residual Map (authoritative, post-EX1d)

This file is the **contract EX3 / EX4 build against**. It enumerates every
remaining `\bagent_id\b` / `\bdomain_id\b` occurrence under `app/` after
the EX1a (auth+key-mint), EX1b (repositories+read/forensics),
EX1c (api+schemas), and EX1d (runtime-contracts + final-sweep) lanes
have all landed.

**Classification key**

- `ORM_COLUMN` — a `Mapped[]` column def on a SQLAlchemy model (or its
  index / constraint references). EX3 owns dropping the non-audit-chain
  columns; EX4 owns `admin_audit_log.{agent_id,domain_id}`.
- `AUDIT_CANONICAL` — entry in `app/repositories/audit_chain.py`'s
  `_CHAIN_FIELDS` tuple. EX4 owns: the field set drives the canonical
  hash and `admin_audit_log.{domain_id,agent_id}` are both hash-chained.
- `NOTNULL_BRIDGE` — code that writes a value (sentinel or pass-through)
  ONLY to satisfy a current NOT-NULL column (or to keep the audit-chain
  hashable shape valid). Disappears WITH the column drop in EX3, or
  with the EX4 chain reseal.
- `DOCSTRING/COMMENT` — descriptive text (module docstring, in-line
  comment, log message, repr / `__repr__`). Fine to leave; no behaviour
  attached. EX3 / EX4 may clean these up opportunistically when they
  edit the surrounding code.

**Methodology**

`grep -rn '\bagent_id\b\|\bdomain_id\b' app/ --include='*.py'` —
325 hits total post-EX1d. Every line is classified below.

Pillar: after EX1a-d the ONLY live `agent_id` / `domain_id` in `app/`
are ORM_COLUMN + AUDIT_CANONICAL + NOTNULL_BRIDGE + DOCSTRING/COMMENT.
No live service params / filters / fields outside those four classes.

---

## ORM_COLUMN

These are Mapped column definitions, index/uniqueconstraint references,
and the same column read on a session-attached row. Owned by **EX3**
(non-audit) and **EX4** (`admin_audit_log` — agent_id + domain_id are
both in the canonical hash chain).

### app/models/admin_audit_log.py  (EX4-owned — hash-chained)

- `app/models/admin_audit_log.py:23` — WHERE-shape docstring (legacy
  bullet list; can drop opportunistically with EX4).
- `app/models/admin_audit_log.py:814` — `domain_id: Mapped[str | None]`
  column def. **EX4 owns**.
- `app/models/admin_audit_log.py:817` — `agent_id: Mapped[str | None]`
  column def. **EX4 owns**.
- `app/models/admin_audit_log.py:866` — index column-list line
  referencing `agent_id`. **EX4 owns** (or kill alongside the column).

### app/models/api_key.py  (EX3-owned)

- `app/models/api_key.py:49` — `domain_id: Mapped[str | None]` column.
- `app/models/api_key.py:52` — `agent_id: Mapped[str | None]` column.

### app/models/conversation.py  (EX3-owned)

- `app/models/conversation.py:116` — `domain_id: Mapped[str]` column.
- `app/models/conversation.py:171` — composite-index column-list ref.
- `app/models/conversation.py:181` — composite-index column-list ref.

### app/models/identity_claim.py  (EX3-owned)

- `app/models/identity_claim.py:193` — `domain_id: Mapped[str]` column.
- `app/models/identity_claim.py:263` — unique-constraint column-list ref.
- `app/models/identity_claim.py:274` — composite-index column-list ref.
- `app/models/identity_claim.py:287` — composite-index column-list ref.

### app/models/knowledge.py  (EX3-owned)

- `app/models/knowledge.py:50` — `domain_id: Mapped[str | None]` column.
- `app/models/knowledge.py:165` — composite-index column-list ref
  (`ix_knowledge_scope`).
- `app/models/knowledge.py:172` — composite-index column-list ref.

### app/models/memory.py  (EX3-owned)

- `app/models/memory.py:60` — `agent_id: Mapped[str | None]` column.

### app/models/scope_assignment.py  (EX3-owned)

- `app/models/scope_assignment.py:133` — `domain_id: Mapped[str]` (NOT NULL).
  Drives every `NOTNULL_BRIDGE` entry that writes to this column.
- `app/models/scope_assignment.py:240` — index/constraint column-list ref.

### app/models/session.py  (EX3-owned)

- `app/models/session.py:27` — `domain_id: Mapped[str]` (NOT NULL).
  Drives the chat_widget / sessions-route / IdentityResolver bridges.
- `app/models/session.py:28` — `agent_id: Mapped[str | None]` column.

### app/models/trace.py  (EX3-owned)

- `app/models/trace.py:40` — `domain_id: Mapped[str | None]` column.
- `app/models/trace.py:41` — `agent_id: Mapped[str | None]` column.

### app/models/user_invite.py  (EX3-owned)

- `app/models/user_invite.py:152` — `domain_id: Mapped[str]` (NOT NULL).
  Drives the invite-route NOTNULL_BRIDGE.

### Same-column reads via ORM attribute access (still EX3 territory)

The following are reads of an ORM column attribute on a session-loaded
row (`api_key.domain_id`, `invite.domain_id`, `assignment.domain_id`,
etc.) Those attributes evaporate when the underlying ORM column is
dropped; classifying them as ORM_COLUMN keeps the EX3 changelist
self-contained.

- `app/services/api_key_service.py:435` — `domain_id=api_key.domain_id`
  on the embed-key-read projection (legacy public-shape envelope; EX1c
  removed the field from the schema, but the dict-build still reads
  the column). EX3 drops the column → this line goes with it.
- `app/services/api_key_service.py:436` — `agent_id=api_key.agent_id`
  (same as above).
- `app/services/invite_service.py:530` — `invite.domain_id` (lazy-expiry
  audit row prep).
- `app/services/invite_service.py:553` — `domain_id = invite.domain_id`
  (redeem flow internal local var; reads the ORM column).
- `app/services/invite_service.py:611` — `domain_id=domain_id` (audit-row
  write, value sourced from `invite.domain_id`).
- `app/services/invite_service.py:612` — `agent_id=agent_slug` (audit-row
  write; the slug derives from the invite email, NOT from a column).
  **AUDIT_CANONICAL** input — kept until EX4 resolves the hash field set.
- `app/services/invite_service.py:624` — `"domain_id": domain_id` (audit
  `after_json` payload field).
- `app/services/invite_service.py:725` — `invite.domain_id` (resend
  audit row).
- `app/services/invite_service.py:813` — `invite.domain_id` (revoke
  audit row).
- `app/repositories/scope_assignment_repository.py:376` — `domain_id=assignment.domain_id`
  (end-assignment audit-row write).
- `app/identity/bootstrap.py:228` — `domain_id=r.domain_id` (hydrate a
  transient ScopeAssignment from the SECDEF bootstrap function).
- `app/identity/bootstrap.py:257`, `:277` — `_hydrate_scope_assignment`
  parameter + ORM-write back into `ScopeAssignment.domain_id`. EX3 drops
  the column → both go with it.
- `app/identity/bootstrap.py:179`, `:197` — `_COLUMNS` list + SECDEF
  function SELECT list (referenced via raw SQL of
  `arc9_c22_bootstrap_identity(...)` — its DB-side return shape goes
  with the column drop).
- `app/identity/resolver.py:201`, `:234`, `:235`, `:246`, `:254`,
  `:266`, `:274`, `:287`, `:306`, `:332`, `:352`, `:388`, `:401`,
  `:419`, `:434`, `:440` — `IdentityResolver` keeps `domain_id` as
  a parameter on `resolve(...)` / `_lookup_claim(...)` / `_select_session(...)`
  / `_create_session(...)` because it writes to `IdentityClaim.domain_id`
  (NOT NULL) and `SessionModel.domain_id` (NOT NULL). EX3 drops both
  columns → the parameter chain collapses. Classify as **ORM_COLUMN**
  (consumer of the columns).
- `app/repositories/knowledge_repository.py:62`, `:92`, `:135`,
  `:159`, `:160`, `:163`, `:169`, `:236`, `:280`, `:285`, `:292`,
  `:300`, `:322`, `:355` — `KnowledgeRepository.search_similar` /
  `list_active_chunks_for_scope` retain `domain_id: str | None`
  because the column persists on `KnowledgeChunk` and the
  union-inheritance read still has a `domain_id IS NULL` predicate.
  EX1d's retriever surface always passes `None` here; the parameter
  and predicate evaporate WITH the column drop. **ORM_COLUMN**
  consumer.
- `app/repositories/scope_assignment_repository.py:87`, `:113`, `:129`,
  `:134`, `:150` — `ScopeAssignmentRepository.create(...)` requires
  `domain_id: str` because `ScopeAssignment.domain_id` is NOT NULL.
  Callers (service layer) supply the `_DOMAIN_COLLAPSE_SENTINEL`
  per the **NOTNULL_BRIDGE** rule.
- `app/repositories/session_repository.py:50`, `:76` —
  `SessionRepository.create_session(...)` requires `domain_id: str`
  because `SessionModel.domain_id` is NOT NULL. Same shape.
- `app/repositories/user_invites.py:62`, `:86` —
  `UserInviteRepository.create(...)` requires `domain_id: str` because
  `UserInvite.domain_id` is NOT NULL.
- `app/services/session_service.py:54`, `:64`, `:75`, `:119`, `:133` —
  `SessionService.create_session` / `.create_session_with_identity`
  thread `domain_id: str` down to the repo because the column is
  NOT NULL. EX3 drops the param chain.
- `app/services/invite_service.py:326`, `:397`, `:408`, `:416` —
  `invite_service.create_invite(...)` accepts `domain_id: str` and
  forwards it to the repo because `user_invites.domain_id` is NOT NULL.
  The route (`POST /admin/invites`) sources the value from the cookied
  user's active ScopeAssignment (the **NOTNULL_BRIDGE** entry below).
- `app/services/invite_service.py:582` — `ScopeAssignment.domain_id.is_(None)`
  predicate on the redeem path. Reads the ORM column; EX3 drop converts
  this to either a removed predicate or a remap. **ORM_COLUMN** consumer.
- `app/services/invite_service.py:591` — `domain_id=None` write on the
  redeem-time ScopeAssignment insert. This INSERT would actually
  IntegrityError today (NOT-NULL column with `=None`); the redeem path
  is currently exercised only in tests that don't reach this branch.
  EX3 will surface and fix this in the same migration as the column drop.
- `app/repositories/audit_chain.py:93,:94` — `domain_id` / `agent_id`
  entries in `_CHAIN_FIELDS`. **AUDIT_CANONICAL** — see below.
- `app/repositories/admin_audit_repository.py:160`, `:161`, `:216`,
  `:217` — `AdminAuditRepository.record(...)` accepts `domain_id` /
  `agent_id` kwargs and forwards them to the `AdminAuditLog` row.
  The repo signature is **AUDIT_CANONICAL** consumer (the columns it
  writes are in the canonical hash); every in-tree caller now passes
  `None` for both, but the repo signature stays until EX4 resolves the
  hash field set.

---

## AUDIT_CANONICAL  (EX4-owned)

The audit canonical hash field set in `app/repositories/audit_chain.py`
includes both `domain_id` and `agent_id`. Per `arc12_specs/02_EXCISION_PLAN.md`
EX4, the resolution is **(A) remove the fields from the canonical set +
drop the columns + reseal historical rows** — the founder mandate is
"column gone from the system."

- `app/repositories/audit_chain.py:93` — `"domain_id"` in `_CHAIN_FIELDS`.
- `app/repositories/audit_chain.py:94` — `"agent_id"` in `_CHAIN_FIELDS`.

When EX4 drops these from `_CHAIN_FIELDS`, the matching
`admin_audit_log.{domain_id,agent_id}` ORM columns (above) drop in the
same migration along with the chain-reseal of historical rows.

The following writes feed into the canonical hash and therefore couple
EX4's reseal scope. They all currently pass `domain_id=None` /
`agent_id=None` post-EX1a-d (with the exception of
`invite_service.py:611,612` which still emit non-NULL `agent_id` /
`domain_id` into the redeem audit row — bridge below). Listed here so
EX4 can audit the writes' shape ahead of the field-set change:

- `app/api/v1/admin_forensics.py:795,:796` — `domain_id=None, agent_id=None`
  audit-row writes on the verify-harness path.
- `app/memory/service.py:190,:191` — `domain_id=None, agent_id=None`
  audit-row writes on extractor save-fail.
- `app/services/api_key_service.py:271,:272`, `:356,:357` —
  `domain_id=None, agent_id=None` audit-row writes on key-mint /
  key-rotate.
- `app/services/email_suppression_service.py:306,:307`, `:383,:384` —
  `domain_id=None, agent_id=None` direct `AdminAuditLog(...)` constructs.
- `app/services/memory_admin_service.py:120,:121` —
  `domain_id=None, agent_id=None` audit-row write on deactivate.
- `app/worker/tasks/embed_source.py:467` — `domain_id=None` audit-row write.
- `app/worker/tasks/memory_extraction.py:490,:491` —
  `domain_id=None, agent_id=None` audit-row write on memory extraction.
- `app/services/invite_service.py:530,:611,:612,:725,:813` —
  redeem / lazy-expire / resend / revoke audit-row writes that read
  `invite.domain_id` / build `agent_id=agent_slug`. These are
  **NOTNULL_BRIDGE-into-AUDIT_CANONICAL** — see bridge section. EX4
  resolution must clear these specifically.
- `app/repositories/scope_assignment_repository.py:129,:134,:376` —
  ScopeAssignment audit-row writes that thread the ORM column value.
  **NOTNULL_BRIDGE-into-AUDIT_CANONICAL** — see bridge section.

---

## NOTNULL_BRIDGE  (EX3 removes WITH the column drop)

These are places that write a value SOLELY to satisfy a current
NOT-NULL constraint (the EX3 column drop deletes both the write and
the column in the same migration).

### `sessions.domain_id` (NOT NULL) bridge

- `app/api/v1/chat_widget.py:242` — synthesises
  `_legacy_session_domain_sentinel = f"instance-{luciel_instance_id}"`
  and writes it into the `sessions.domain_id` column on widget-driven
  session creation.
- `app/api/v1/chat_widget.py:317`, `:341` — uses the sentinel above
  on the two session-create call sites (widget POST + widget reuse).
- `app/api/v1/sessions.py:228` — same `_legacy_session_domain_sentinel`
  pattern on the programmatic `POST /sessions` route.
- `app/identity/resolver.py:401`, `:440` — `IdentityResolver._create_session(...)`
  writes `domain_id=domain_id` (caller-supplied) into the new
  `SessionModel` row. The caller chain bottoms out at the same
  identity-resolution path used by chat / widget; the value supplied
  is whatever the call site chose (sentinel for widget, real domain
  for legacy programmatic). All evaporate WITH the column drop.

### `scope_assignments.domain_id` (NOT NULL) bridge

- `app/services/scope_assignment_service.py:169`, `:399` — service
  supplies `_DOMAIN_COLLAPSE_SENTINEL = "default"` to satisfy the
  NOT-NULL column when the public payload schema (`ScopeAssignmentCreate`,
  EX1c) no longer carries `domain_id`.
- `app/services/tier_provisioning_service.py:778` — Free-tier signup
  supplies the same `_DOMAIN_COLLAPSE_SENTINEL` when creating the
  initial owner ScopeAssignment.
- `app/repositories/scope_assignment_repository.py:113`, `:129`, `:134`
  — the repository's `create(...)` insert + matching audit-row write.
  The repo's `domain_id: str` parameter (line 87) is the consumer; the
  caller supplies the bridge value above.

### `user_invites.domain_id` (NOT NULL) bridge

- `app/api/v1/admin.py:941` — route resolves `domain_id = default_domain_id`
  from the cookied user's active ScopeAssignment so the create path
  can satisfy `user_invites.domain_id`.
- `app/api/v1/admin.py:957` — passes that value into `invite_service.create_invite`.
- `app/api/v1/admin.py:893` — `_resolve_invite_actor` returns
  `chosen.domain_id` (read off the cookied user's active ScopeAssignment
  row) which feeds the bridge above.
- `app/services/invite_service.py:326`, `:397`, `:408`, `:416` —
  `create_invite` accepts and forwards the bridge value to the repo
  (insert) + the audit-row write.
- `app/repositories/user_invites.py:62`, `:86` — repo's `create(...)`
  consumes the bridge value and writes the NOT-NULL column.

### `identity_claims.domain_id` (NOT NULL) bridge

- `app/identity/resolver.py:201`, `:234`, `:235`, `:266`, `:274` —
  `IdentityResolver.resolve(...)` accepts `domain_id: str` and writes
  it into `IdentityClaim.domain_id` when minting a new claim row.
  Caller chain bottoms out at the SessionService identity path.
  EX3 drops the column → the parameter chain collapses.

### Pass-through to AUDIT_CANONICAL columns

These write a non-NULL value to `admin_audit_log.domain_id` and/or
`admin_audit_log.agent_id` (both currently hash-chained). They survive
EX1d because the audit chain still hashes those columns; EX4's reseal
removes the columns from the canonical set, and these writes either
drop to NULL or vanish in the same change.

- `app/services/invite_service.py:611` — `domain_id=domain_id` on the
  `INVITE_REDEEMED` audit-row write (value sourced from the redeem-time
  invite row).
- `app/services/invite_service.py:612` — `agent_id=agent_slug` on the
  same audit row (the slug is computed at redeem time from the email
  prefix and exists for historical search continuity only).
- `app/services/invite_service.py:530`, `:725`, `:813` —
  `domain_id=invite.domain_id` on the lazy-expire / resend / revoke
  audit rows.
- `app/repositories/scope_assignment_repository.py:129`, `:134`, `:376`
  — `domain_id=domain_id` (create), `"domain_id": domain_id` (audit
  payload `after`), and `domain_id=assignment.domain_id` (end audit
  row) — all writing the V2 sentinel value into the canonical
  audit-row columns.

### Repository-side legacy ingestion compat

- `app/knowledge/ingestion.py:281` — `domain_id=None` ingestion
  audit-row write. **AUDIT_CANONICAL** input under EX4.
- `app/knowledge/ingestion.py:357`, `:361` — `domain_id` kwarg on
  `IngestionService.ingest_text(...)` retained as a no-op compat
  shim ("legacy compat, ignored" — docstring). Removable with EX3.

### Admin/forensics breakdown by ORM column

- `app/services/admin_service.py:186`, `:194`, `:199`, `:203`,
  `:288`, `:296`, `:301`, `:304` —
  `AdminService` aggregates over `MemoryItem.agent_id` for the
  per-memory-row delete-preview / pause breakdown. The agg is computed
  for the `after_json` audit payload only; v2 rows always have
  `agent_id=NULL` so the breakdown is informational and degrades
  cleanly when the column drops. **ORM_COLUMN** consumer; goes WITH
  the column drop in EX3.
- `app/services/admin_service.py:267` — docstring referencing the
  `agent_id` breakdown.
- `app/services/admin_service.py:363,:380,:382` —
  V2 no-op stub method that still accepts a legacy `domain_id` arg
  for stub continuity. **DOCSTRING/COMMENT** in spirit (the method is
  a stub); removable with EX3 along with the rest of the column.

### Service legacy stub: `ScopePromptPreflight`

- `app/services/scope_prompt_preflight.py:41`, `:44`, `:47`, `:60` —
  `ScopePromptPreflight` carries a `domain_id` attribute on the
  exception dataclass and the preflight-result struct. The module is
  not on a live runtime path (used by an older preflight surface).
  EX3 closeout pass can excise; tagging as **NOTNULL_BRIDGE-adjacent**
  (it surfaces data that ultimately bound to NOT-NULL columns).

---

## DOCSTRING / COMMENT  (no action required; clean up opportunistically)

Module / function / class docstrings, log-format strings, EX1a-d
provenance comments, `__repr__` formats, and Architecture cross-refs.
These have no behaviour and can stay; EX3 / EX4 may strip them out
when the surrounding code goes.

### Module / class docstrings

- `app/api/v1/admin.py:406, :461, :518, :587, :838, :878, :936, :938,
  :940, :1039` — EX1a/EX1b/EX1c provenance comments + the
  `_resolve_invite_actor` docstring.
- `app/api/v1/admin.py:1529` — `domain_id=None` in
  `get_effective_chunking_config` (passes None into a repo that still
  accepts the kwarg). **ORM_COLUMN** consumer call; comment-classed
  because the line is a literal `None`.
- `app/api/v1/admin_forensics.py:3, :8, :9, :11, :17, :188, :215,
  :244, :429, :794` — module docstring + EX1b provenance comments.
- `app/api/v1/audit_log.py:115, :226` — EX1b provenance comments
  + diff-key drop docstring.
- `app/api/v1/chat_widget.py:201, :202, :237, :238, :241, :316, :340`
  — EX1c bridge documentation. The line-242 / -317 / -341 SENTINEL
  writes are NOTNULL_BRIDGE (above); the comments are descriptive.
- `app/api/v1/dashboard.py:189, :190, :199, :200` — EX1c
  route-removal provenance.
- `app/api/v1/sessions.py:148, :149, :185, :186, :204, :210, :211,
  :212, :227` — EX1c provenance. Lines 211/212 are explicit
  `domain_id=None, agent_id=None` audit-row writes (NOTNULL_BRIDGE
  for AUDIT_CANONICAL).
- `app/knowledge/retriever.py:9, :11, :14, :16, :138, :143` —
  EX1d retriever docstring + the `domain_id=None` pass-through.
- `app/memory/cross_session_retriever.py:26, :27, :29` — EX1d
  module docstring.
- `app/middleware/auth.py:4, :5, :27, :53, :175, :202, :231, :234` —
  module docstring + EX1a provenance comments.
- `app/middleware/session_cookie_auth.py:305, :327` — EX1a comments.
- `app/models/admin_audit_log.py:23, :866` — column-shape docstrings.
- `app/models/api_key.py:10, :11, :12, :13` — module docstring.
- `app/models/conversation.py:18, :23, :26, :27, :29, :188, :195` —
  module docstring + `__repr__` format string.
- `app/models/identity_claim.py:19, :22, :266, :294, :304` —
  module docstring + `__repr__`.
- `app/models/knowledge.py:21` — module docstring (legacy column note).
- `app/models/scope_assignment.py:35, :129, :130, :131, :258` —
  module docstring + `__repr__`.
- `app/models/trace.py:74` — column-shape comment.
- `app/models/user_invite.py:18, :23, :51, :52, :55, :146, :147` —
  module docstring + service-validation comment.
- `app/policy/scope.py:23, :24, :25, :91, :175` — EX1d module
  docstring + `_caller` tuple-shape comment + delegation-removal note.
- `app/repositories/knowledge_repository.py:144, :146, :148, :245,
  :246, :247, :261` — `list_active_chunks_for_scope` / `search_similar`
  docstrings describing the union-inheritance shape.
- `app/repositories/memory_repository.py:11, :12, :14, :80` —
  module docstring (Arc 12 EX1b note) + legacy filter comment.
- `app/repositories/scope_assignment_repository.py:97, :99, :150` —
  service-contract docstrings.
- `app/repositories/session_repository.py:6, :14, :17, :19, :20,
  :22, :23, :24, :25, :71, :117` — module docstring + EX1b notes.
- `app/repositories/trace_repository.py:9, :81, :82, :85, :86` —
  module docstring + EX1b notes.
- `app/runtime/contracts.py:9, :10` — EX1d module docstring.
- `app/runtime/orchestrator.py:222` — EX1d inline comment on the
  record_trace call.
- `app/schemas/api_key.py:11, :90, :91, :143, :144, :278, :304,
  :309, :433, :436` — module / class docstrings.
- `app/schemas/audit_log.py:63` — EX1b class docstring.
- `app/schemas/invite.py:36, :38, :87` — EX1c class docstrings.
- `app/schemas/knowledge.py:111, :112` — EX1c class docstring.
- `app/schemas/memory.py:23, :25` — EX1c class docstring.
- `app/schemas/onboarding.py:120` — EX1c class docstring.
- `app/schemas/scope_assignment.py:55, :57, :154, :185` — EX1c class
  docstrings.
- `app/schemas/session.py:9, :12, :26` — EX1c class docstrings.
- `app/schemas/team_member.py:24` — EX1c class docstring.
- `app/services/admin_service.py:135, :136, :267` — comments on the
  V2 column-NULL invariant.
- `app/services/api_key_service.py:158, :353` — EX1a provenance.
- `app/services/dashboard_service.py` — (no remaining
  `agent_id`/`domain_id` references after EX1d).
- `app/services/invite_service.py` — bridge usages above; no docstring-
  only ones to call out.
- `app/services/memory_admin_service.py:107, :108` — EX1b comment.
- `app/services/onboarding_service.py:237` — EX1a comment.
- `app/services/scope_assignment_service.py:77, :79, :139, :164,
  :394` — EX1d sentinel comment + create/promote docstrings.
- `app/services/tier_provisioning_service.py:80, :99` — sentinel
  comment.
- `app/services/trace_service.py:7, :8, :10, :89` — EX1d module
  docstring + inline comment on the Trace insert.
- `app/worker/tasks/memory_extraction.py:21, :231, :477, :478, :479` —
  EX1b/d worker-task docstrings.

---

## Gate

After EX1a-d, the residuals partition is:

| Class            | Count* | Owner | Resolves at      |
| ---------------- | ------ | ----- | ---------------- |
| ORM_COLUMN       | many   | EX3 (non-audit) / EX4 (audit) | column drop |
| AUDIT_CANONICAL  | 2 in `_CHAIN_FIELDS` + ~20 writers | EX4 | hash-field-set change + reseal |
| NOTNULL_BRIDGE   | ~25 sites across `sessions`, `scope_assignments`, `user_invites`, `identity_claims` | EX3 | DROP NOT NULL → DROP COLUMN |
| DOCSTRING/COMMENT | bulk of remaining 325 hits | (free) | opportunistic |

*Aggregate counts; the per-line classification above is the authoritative
list. No live service param / filter / response-field appears in
`app/` post-EX1d **outside** these four classes. Tests have been
updated to the v2 shape and the pre-existing 11-failure baseline
(`rls_c4_3 ×6`, `audit_script ×3`, `lookup_property UNASSIGNED ×2`)
holds.
