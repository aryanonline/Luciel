# Arc 8 — Security-hardening execution arc record

**Status:** OPEN — five work-units LANDED (WU-1 / WU-2 / WU-3 / WU-4 / WU-5), three work-units PENDING (WU-6 / WU-7 / WU-8). Closing tag `arc-8-security-hardening-complete` to be cut at WU-8 close (or its rename if a later WU is added). Arc 8 is the operational-maturity sprint named in CANONICAL_RECAP §12 row 28; this arc record is the structured continuation under the V2-anchored doctrine produced by Arc 9.

**Authored:** 2026-05-22 immediately after Arc 9 close (`arc-9-doctrine-reanchor-complete` tag at `722a945`). Arc 8 began earlier in the same calendar day with the WU-1 / WU-2 / WU-3 paired deploy ceremony (commits `02e1989` / `74df825` / `488c0b9`) before Arc 9 paused it for the doctrine rewrite. This record is authored retroactively for the LANDED work-units and prospectively for the PENDING work-units so that the arc is recoverable from a single artifact.

**Audience:** Partner-facing. Mixed code + AWS-infra arc. Every PENDING work-unit lands at least one code commit; some land multiple. Doc-only commits frame each work-unit's open and close.

**Triggering statement (partner, 2026-05-22 mid-morning):** *"we are designing a business we can get lazy and just ignore things"* (canonical carry-forward locked operational rule — meaning we **cannot** get lazy and **cannot** ignore things, including the SES feedback-loop posture, the Free-tier abuse-control surface, and the Enterprise metering hook). And immediately after the Arc 9 close (~14:25 EDT): *"okay partner it looks good let us continue. I hope what we are have done till now for Arc 8 and what we are going to do is also aligned with our vision?"* — which triggered the alignment audit that surfaced WU-7 and WU-8 as V2-doctrine-completeness gaps the original Arc 8 scope had not yet absorbed. Partner accepted Option 2 (expand Arc 8 to include WU-6 + WU-7 + WU-8 before proceeding to Arc 5) at ~14:30 EDT.

**Cross-refs:**

- `docs/CANONICAL_RECAP.md` §12 row 28 (Hardening / Operational maturity sprint) — Arc 8 is the Phase 4 continuation; WU-6 / WU-7 / WU-8 are the named remaining work
- `docs/CANONICAL_RECAP.md` §11.7 (public positioning) — Free / Pro / Enterprise tier shape that WU-7 (captcha closure) and WU-8 (Enterprise metering) operationalize
- `docs/ARCHITECTURE.md` §3.2.14 (V2-anchored, Arc 9 WU-9.5 landed) — metering hook design + `billing_model` enum + `admin_tier_overrides` extensions + `metering_emissions` cursor table (WU-8's design source-of-record)
- `docs/ARCHITECTURE.md` §3.2 tier-shape note + §4.7 (V2-anchored) — Free-tier CAPTCHA gate as one of the two new enforcement gates introduced under the tier-shape revision (WU-7's design source-of-record)
- `docs/DRIFTS.md` §3 — every Arc 8 work-unit anchors to one or more named drifts; see §3 below for the full mapping
- `arc4-out/A-tenancy-collapse-arc-record.md` (Arc 4 record, V2-rescoped 2026-05-22 at WU-9.7) — Arc 8-before-Arc 5 sequence row in the risk register
- `arc9-out/A-arc9-doctrine-reanchor-arc-record.md` — Arc 9 record; Arc 8 resumed at Arc 9 close
- `arc3-out/B3-ses-iam-no-op-record.md` and `arc3-out/B4-ses-sandbox-exit-request.md` — Arc 3 ledger-correction records that surfaced the SES cohort (WU-6)

---

## §1 — Scope and non-scope

### §1.1 — In scope (this arc's commits land these)

Arc 8 is the Phase 4 continuation of the security-hardening sprint named at CANONICAL §12 row 28. The arc has eight work-units, five LANDED before this record was authored and three PENDING. Every work-unit closes one or more named drifts; every commit references this arc-record by path.

1. **WU-1 — Misfiled gating drift closure (LANDED at `e691b19`).** Close `~~D-health-version-endpoint-gated-by-apikey-auth-2026-05-22~~` as a misfile (the real path `/api/v1/version` was already on `SKIP_AUTH_PATHS`); correct the path-string error on the paired payload drift. Doc-only commit. See §3.1 for the full record.
2. **WU-2 — Worker-not-root + BuildKit hardening (LANDED at `02e1989` and `74df825`).** Dockerfile `USER luciel` (uid=10001) directive; BuildKit deploy script hardening (`--platform linux/amd64 --provenance=false --sbom=false`); Stage 1b paranoid local `docker inspect` gate before ECR push. Closes `~~D-worker-runs-as-root-in-container-2026-05-22~~` and the in-flight new drift `~~D-buildkit-attestations-poison-fargate-image-selection-2026-05-22~~`. See §3.2 for the full record including the diagnostic-correction note.
3. **WU-3 — Version endpoint build-SHA observability (LANDED with WU-2 at `02e1989`).** `BUILD_GIT_SHA` build-arg threading; `app/core/build_info.py` singleton; `app/api/v1/version` payload superset (`git_sha` field added). Closes `~~D-version-endpoint-hardcoded-not-build-sha-2026-05-22~~`. See §3.3.
4. **WU-4 — Drift-stanza CLOSURE for WU-2 + WU-3 (LANDED at `488c0b9`).** Doc-only commit that wrote the WU-2 / WU-3 closure stanzas into DRIFTS.md and filed the new BuildKit footgun drift. Also corrected the `ps -eo` invocation in the deploy script ledger. See §3.4.
5. **WU-5 — Free-tier captcha infra prep (LANDED at `bf9abbc`).** Code-only commit (no deploy): `app/services/hcaptcha_service.py` verify shim; `POST /api/v1/billing/signup-free` route shell (captcha-pass → `status="pending-arc-5"`); `settings.hcaptcha_*` field reservations; `tests/api/test_signup_free_shape.py` contract pin. Partial landing of `D-free-tier-captcha-missing-2026-05-22` — full closure deferred to WU-7. See §3.5.
6. **WU-6 — SES feedback-loop + suppression + IAM rightshape + sandbox exit (PENDING).** Closes the five-drift SES cohort: `D-ses-feedback-loop-not-wired-2026-05-22`, `D-ses-suppression-app-layer-not-implemented-2026-05-22`, `D-ses-iam-overgrant-unused-actions-2026-05-22`, `D-ses-sandbox-exit-pending-2026-05-22`, `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22`. Mixed code + AWS-infra. Drive end-to-end without partner pause per agent-locked judgment 3. See §3.6.
7. **WU-7 — Free-tier captcha end-to-end closure (PENDING — NEW under this arc's scope expansion).** Builds on WU-5 infra prep. Wire hCaptcha verify live (SSM SecureString `HCAPTCHA_SECRET_KEY` operator provisioning); marketing-site widget wiring; Arc 5-coordinated first-Instance provisioning live (the `admins` row mint + email-send + `last_signup_ip` capture + 1-per-IP soft gate); `ADMIN_FREE_TIER_PROVISIONED` audit row. Closes `D-free-tier-captcha-missing-2026-05-22` end-to-end. **Pause for partner review at WU-7 close** per agent-locked judgment 3. See §3.7.
8. **WU-8 — Enterprise metering hook implementation (PENDING — NEW under this arc's scope expansion).** Implements the ARCHITECTURE §3.2.14 design: `metering_emissions` cursor table, `subscriptions.billing_model` enum column, `admin_tier_overrides` table, `app/workers/metering_worker.py` Celery beat worker, `METERING_USAGE_EMITTED` audit action, Stripe Enterprise hybrid Price pair + SSM keys, entitlement-policy extension to consult overrides. Closes `D-enterprise-metering-not-implemented-2026-05-22`. **Pause for partner review at WU-8 close** per agent-locked judgment 3. See §3.8.

**Arc 8 close (WU-8 close commit, doc-only):** symmetry verification across CANONICAL ↔ ARCHITECTURE ↔ DRIFTS for every Arc 8 commit; closing tag `arc-8-security-hardening-complete` (or final WU-named tag) stamps on this commit.

### §1.2 — Explicitly out of scope (deferred until Arc 8 closes)

1. **Arc 5 schema migration (V2-anchored).** Three staged Alembic revisions per `arc4-out/A-tenancy-collapse-arc-record.md` re-scoped plan (`tenants→admins`, drop `domains`, `agents→instances`). Arc 5 follows Arc 8 close because Arc 8 touches `app/middleware/*.py`, `app/repositories/audit_chain.py`, `app/main.py` files Arc 5 will rename — running Arc 5 before Arc 8 would force merge conflicts on every security-hardening commit. **Exception:** WU-7's `admins.last_signup_ip` column lands in Arc 5 Revision A (do not land standalone — the `admins` table does not exist pre-Arc-5); WU-8's `admin_tier_overrides` + `metering_emissions` tables land in Arc 5 Revision A. The Alembic-level work is sequenced with Arc 5, but the application-layer scaffolding (worker, route, service, audit constant, Stripe SKU set) lands in Arc 8 against the post-Arc-5 names.
2. **Arc 6 Stripe SKU restructure (~3 commits per WU-9.7 re-scoping).** Free / Pro / Enterprise SKU set per V2; Enterprise hybrid Price pair lands here. WU-8 lands the metering worker + entitlement-policy extension that consume the Stripe identifiers; the SKU set itself is Arc 6 scope.
3. **Pydantic V2→V3 deprecation, FastAPI 422 rename, slowapi asyncio Python 3.16 deprecation.** Latent hygiene drifts that surface in test runs as warnings. Not in Arc 8 scope (low operational impact; orthogonal to the security and tier-shape surface).
4. **Phase 6 Pass 0 E2E replay.** Deferred until Arc 5 + Arc 6 land against the V2-anchored doctrine and the Arc 8 closure surface (worker-not-root + version observability + SES feedback + Free captcha + Enterprise metering) is verifiable end-to-end.
5. **`luciel-ecs-backend-role` SSM-messages mirror** (sibling-cohort forward-ref from the WU-2 closure stanza). Backend exec capability was not the WU-2 closure-blocker; mirror is deferred as routine ops follow-up post-Arc-8.

---

## §2 — Why this arc exists (the security-hardening defect classes)

The Arc 8 cohort is the operational-maturity sprint named at CANONICAL §12 row 28. Phases 1–3 of that sprint landed pre-Arc-8 (DB role separation, audit-log hash chain, retention purge worker, et al.); Phase 4 is the remaining work. Five defect classes survive into this arc, named here so each work-unit has a discrete target.

### §2.1 — Defect class A: Container least-privilege gap (worker ran as root pre-WU-2)

Pre-WU-2, the Dockerfile carried no `USER` directive after the `pip install` layer; both backend (uvicorn) and worker (celery) ran as `uid=0(root)` inside the container. Celery's own boot log emitted `SecurityWarning: You're running the worker with superuser privileges`. Defense-in-depth gap — the container filesystem was read-mostly and the worker had no inbound network attack surface, but a process-level RCE would have yielded root inside the container with no further escalation. **Closed at WU-2.**

### §2.2 — Defect class B: Operator-visible build identity (`/api/v1/version` returned hardcoded `0.1.0` pre-WU-3)

Pre-WU-3, `/api/v1/version` returned a hardcoded `{"app":"Luciel Backend","version":"0.1.0","status":"ok"}` literal regardless of which build was running. Operators could not verify build identity from outside the auth perimeter; incident response was slowed because the live build SHA required `aws ecs describe-services` + `aws ecs describe-task-definition` chains rather than a single public curl. Traceability-pillar gap. **Closed at WU-3.**

### §2.3 — Defect class C: SES deliverability posture (sandbox + no feedback + no suppression + no reply-to + IAM overgrant)

The Arc 3 B.3 / B.4 ledger-correction pass surfaced five OPEN drifts against the SES surface: the account is in sandbox mode (`ProductionAccess=false`); no SNS topic / configuration set is wired for bounce/complaint events; no application-layer suppression list exists (a previously-bounced address will be sent to again); no `ReplyToAddresses` is set on outbound calls (replies black-hole at `noreply@vantagemind.ai`); the IAM inline policy grants `ses:SendBulkEmail` which the app never calls. This cohort is paired by AWS approval timing — the sandbox-exit ticket is a hard prerequisite for the deliverability posture work, and the feedback/suppression/reply-to/IAM work is what makes the approval bar credible. **Closes at WU-6.**

### §2.4 — Defect class D: Free-tier abuse-control gap (no CAPTCHA at signup pre-WU-7)

Under the V2 tier shape, Free signup ships at $0/month with no Stripe payment-method gate. Email verification (existing magic-link flow) is necessary but not sufficient — a bad-faith actor can mint N email aliases on a disposable-email provider and provision N Free Admins, consuming the per-Admin static allowance N times over. The required abuse-control surface is a CAPTCHA at signup + a 1-per-email-domain or 1-per-IP soft gate. WU-5 landed the infra prep (verify shim + route shell + settings reservation + contract test) but the wiring is not live (no SSM secret, no marketing-site widget, no first-Instance provisioning). **Closes at WU-7.**

### §2.5 — Defect class E: Enterprise metering hook absent (cannot sell Enterprise without WU-8)

Under the V2 tier shape, Enterprise ships with a hybrid billing model: platform fee (flat recurring) + included usage + overage (metered) + optional committed-use discount. Today none of the implementation artifacts exist: no `subscriptions.billing_model` column, no `admin_tier_overrides` table, no `metering_emissions` cursor table, no `app/workers/metering_worker.py`, no Stripe Enterprise hybrid Price pair, no Celery beat schedule entry, no `METERING_USAGE_EMITTED` audit constant, no entitlement-policy override consultation. Until this lands, Enterprise tier cannot be sold — a deal closure today would have Luciel collecting the platform-fee Price but silently never invoicing the overage. **Closes at WU-8.**

---

## §3 — Work-unit plan

Each work-unit lands at least one commit (some land multiple). Every commit references this arc-record by path. The five LANDED work-units are recorded post-hoc with their actual commit identity, closure verification, and any agent-error notes; the three PENDING work-units carry input / output / method / partner-involvement / estimated-diff descriptions ready for execution under the locked judgments.

### §3.1 — WU-1 — Misfiled gating drift closure (LANDED 2026-05-22-late at `e691b19`)

**Input:** `~~D-health-version-endpoint-gated-by-apikey-auth-2026-05-22~~` (filed during Arc 3 Work-Unit C deploy ceremony, 2026-05-22 ≈02:23 EDT) and its paired payload drift `D-version-endpoint-hardcoded-not-build-sha-2026-05-22`.

**Output:** Doc-only commit closing the gating drift as a misfile and correcting the path-string error on the paired payload drift. Live wire evidence captured in the closure stanza: `curl https://api.vantagemind.ai/api/v1/version` → HTTP 200 (public, no JWT, already on `SKIP_AUTH_PATHS`); `curl https://api.vantagemind.ai/api/v1/health/version` → HTTP 401 (path does not exist; auth middleware short-circuits before FastAPI route dispatch).

**Method (actual, post-execution):** Walked the path string `/api/v1/health/version` from the original Arc 3 smoke walk and confirmed against the live router chain in `app/main.py` + `app/api/router.py` + `app/api/v1/health.py` that the real path is `/api/v1/version`. Re-issued the smoke against the correct path and got HTTP 200. Walked `app/middleware/auth.py:63` and confirmed `/api/v1/version` is already in `SKIP_AUTH_PATHS`. No code change required.

**Diagnostic-correction note (agent error, recorded honestly):** The Arc 3 deploy ceremony smoke walk issued a curl against `/api/v1/health/version` (presumed-but-wrong path) and read the resulting 401 as evidence of an auth gate. The 401 was evidence of a wrong path, not an auth gate — the auth middleware runs before FastAPI route dispatch, so any nonexistent path under the `/api/v1` prefix returns 401 instead of 404. The drift filing on 2026-05-22 propagated the path error into both the original drift body and the paired payload drift's design claim and validation-evidence sentences. Corrections live in the drift closure stanzas, not by rewriting the ceremony record.

**Partner involvement:** None — surgical doc-only correction, no judgment call.

**Actual diff size:** One commit (`e691b19`), ~30 lines in DRIFTS.md (closure stanza + path-string correction note on the paired drift).

### §3.2 — WU-2 — Worker-not-root + BuildKit hardening (LANDED 2026-05-22 at `02e1989` and `74df825`)

**Input:** `D-worker-runs-as-root-in-container-2026-05-22` (filed during Arc 3 Work-Unit C deploy ceremony, 2026-05-22 ≈02:36 EDT, when Celery's boot log emitted `SecurityWarning: You're running the worker with superuser privileges`).

**Output:** Dockerfile gained `RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin luciel && chown -R luciel:luciel /app` followed by `USER luciel` before the final `CMD`. The same image runs both backend (uvicorn) and worker (celery) processes, so non-root posture applies to both services. BuildKit deploy-script flags added (`--platform linux/amd64 --provenance=false --sbom=false`) to eliminate attestation-manifest ambiguity in Fargate image pulls. Stage 1b paranoid local `docker inspect` gate (`User=='luciel'` + `Architecture=='amd64'`) hard-fails the script before any ECR push if the build does not match expected shape.

**Method (actual, post-execution):** First deploy at ≈11:34 EDT (image digest `sha256:9414390f...`) was diagnosed mid-ceremony as failing because `aws ecs execute-command ... -- id` reported `uid=0(root)`. The diagnosis was wrong — see the diagnostic-correction note below. A second build (`74df825`) was shipped with the BuildKit attestation-suppression flags as a defensive measure against the (hypothesized-but-not-actual) cause of the misread signal. The second build deployed with image digest `sha256:5983a2af8859ef4be44993a23a91fffa4a4a590623a1c57ba53d19af4931d85a` on `luciel-backend:81` + `luciel-worker:36`. ECS Exec into the running worker task ran `ps -eo pid,user,uid,gid,comm` and confirmed PID 1 (celery) was `luciel uid=10001`. New IAM inline policy `luciel-worker-ssmmessages-exec` added to `luciel-ecs-worker-role` so ECS Exec could establish a session channel against the non-root container (the existing inline policy only covered SQS + SSM Parameter Store, not the SSM messaging API surface).

**Diagnostic-correction note (agent error, recorded honestly):** `id` inside an ECS Exec session reports the exec session worker's uid (root, by AWS design), not the container's main process uid. The correct diagnostic is `ps -eo pid,user,uid,gid,comm` and reading PID 1 / the application process row. The wrong diagnostic led the agent to chase a BuildKit attestation-manifest theory (real footgun, but unrelated to the uid signal we were misreading) and ship a second build with `--platform linux/amd64 --provenance=false --sbom=false`. The second deploy was redundant from a USER-directive correctness standpoint — the first image (`9414390f`) was already running celery as `luciel uid=10001`. However, the build hardening is **kept** because (a) it eliminates BuildKit attestation-blob ambiguity in future Fargate pulls (real defensive value) and (b) Stage 1b paranoid local `docker inspect` gate prevents image-misbuild from ever reaching ECR. Net effect: two deploys instead of one, ~30 min of operator time, but no production state corruption. The Dockerfile change landed correctly on the first build; the agent's diagnostic was wrong.

**Paired new drift filed at this WU close:** `~~D-buildkit-attestations-poison-fargate-image-selection-2026-05-22~~` — closed-at-detection in the same commit-pair (`74df825`). See §3.4 for the full stanza authoring at WU-4.

**Partner involvement:** Mid-ceremony — partner observed the diagnostic-correction realization in real time and approved the "keep both builds" call (the WU-2 build hardening is permanent defensive value even though the originating signal was misread). No formal sign-off gate; the realization happened inside the live deploy window.

**Actual diff size:** Two code commits (`02e1989` Dockerfile USER + BUILD_GIT_SHA threading + `74df825` BuildKit hardening + Stage 1b gate) totaling ~80 lines (Dockerfile + `scripts/deploy_arc8.ps1` + IAM JSON inline). One AWS-infra change: the `luciel-worker-ssmmessages-exec` IAM policy attachment.

### §3.3 — WU-3 — Version endpoint build-SHA observability (LANDED 2026-05-22 at `02e1989`, paired with WU-2)

**Input:** `D-version-endpoint-hardcoded-not-build-sha-2026-05-22` (filed during Arc 3 Work-Unit C deploy ceremony, 2026-05-22 ≈02:23 EDT, observed at the corrected path `/api/v1/version` during the Arc 8 WU-1 scoping pass on 2026-05-22-late).

**Output:** Dockerfile declares `ARG BUILD_GIT_SHA="unknown"` + `ENV BUILD_GIT_SHA=$BUILD_GIT_SHA` before any RUN/COPY layers; `app/core/build_info.py` reads the env at module import via `_read_git_sha()` (defaulting to `"unknown"` on empty/whitespace) and populates a module-level singleton `BUILD_INFO` dict with keys `app`, `version`, `git_sha`, `status`; `app/api/v1/health.py` returns `dict(BUILD_INFO)` (fresh copy per call to prevent mutation-poisoning). The version field reads via `importlib.metadata.version("luciel-backend")`. 18 contract tests at `tests/api/test_version_endpoint_shape.py` split across 4 classes (`TestBuildInfoSingleton`, `TestBuildInfoReaders`, `TestVersionRoute`, `TestSkipAuthPathsMembership`).

**Method (actual, post-execution):** Bundled with the WU-2 commit (`02e1989`) because the Dockerfile change is shared between WU-2 (USER directive) and WU-3 (BUILD_GIT_SHA ARG/ENV). Deploy SHA is `74df825` (the second build from the WU-2 BuildKit-hardening re-spin). Live response from `https://api.vantagemind.ai/api/v1/version`: `{"app":"Luciel Backend","version":"0.1.0","git_sha":"74df825","status":"ok"}` — the `git_sha` field is the new field introduced by this drift's closure; the legacy three keys (`app`, `version`, `status`) are preserved verbatim so any operator script that parsed the prior shape still works.

**Deferred:** The image-digest field (ECS provides it via `ECS_CONTAINER_METADATA_URI_V4` at runtime) was deferred — operators have the digest via `aws ecs describe-tasks`; the git_sha is the smaller and more actionable signal. If future ops need it, it's an additive change to `build_info.py` with no breaking effect.

**Partner involvement:** None — bundled with WU-2's mid-ceremony partner-observed window.

**Actual diff size:** Combined with WU-2's `02e1989` commit; the WU-3-attributable diff is ~90 lines (the new `build_info.py` module + the `health.py` rewrite + the 18 contract tests).

### §3.4 — WU-4 — Drift-stanza CLOSURE for WU-2 + WU-3 (LANDED 2026-05-22-late at `488c0b9`)

**Input:** Three drifts requiring closure stanzas after the WU-2 + WU-3 deploy ceremony settled: `D-worker-runs-as-root-in-container-2026-05-22`, `D-version-endpoint-hardcoded-not-build-sha-2026-05-22`, and the new in-flight drift `D-buildkit-attestations-poison-fargate-image-selection-2026-05-22` that needed authoring as closed-at-detection.

**Output:** Doc-only commit that:

1. Wrote the closure stanza for `~~D-worker-runs-as-root-in-container-2026-05-22~~` including the diagnostic-correction note (ECS Exec `id` reports session shell uid, not container main process uid; correct diagnostic is `ps -eo pid,user,uid,gid,comm` reading PID 1).
2. Wrote the closure stanza for `~~D-version-endpoint-hardcoded-not-build-sha-2026-05-22~~` including the path-string correction (the original drift body referenced `/api/v1/health/version`, the real path is `/api/v1/version`) and the image-digest deferral note.
3. Authored the new drift `~~D-buildkit-attestations-poison-fargate-image-selection-2026-05-22~~` as closed-at-detection in the same commit-pair (`74df825`), recording the BuildKit footgun honestly even though it was not the actual cause of the originating uid signal.
4. Corrected the `ps -eo` invocation in the deploy script ledger (the original was `ps -ef` which doesn't isolate the user column the way PID-1 verification requires).

**Method (actual, post-execution):** Doc-only commit; no application-layer or infra-layer touch. Pre-closure drift bodies preserved verbatim under `~~strikethrough~~` so the audit chain remains intact.

**Partner involvement:** Sign-off at the closure-stanza language (the diagnostic-correction notes are partner-readable and required partner acknowledgment that the agent-error recording is the right shape).

**Actual diff size:** One commit (`488c0b9`), ~150 lines in DRIFTS.md (three closure stanzas + one new drift authoring) + small `scripts/deploy_arc8.ps1` ledger correction.

### §3.5 — WU-5 — Free-tier captcha infra prep (LANDED 2026-05-22-late at `bf9abbc`)

**Input:** `D-free-tier-captcha-missing-2026-05-22` (filed during Arc 4 tier-shape revision pass, 2026-05-22-late, when the Free-tier abuse-control surface was committed in CANONICAL §11.7 and §14 but the implementation layer carried none of it).

**Output:** Code-only commit (no deploy):

1. `app/services/hcaptcha_service.py` — verify shim using `httpx` against the hCaptcha verify endpoint; returns success/fail + error codes; boot-safe with no secret configured (returns a `"not_configured"` sentinel that the route uses to short-circuit to HTTP 501).
2. `POST /api/v1/billing/signup-free` — route shell in `app/api/v1/billing.py` accepting `{email, display_name, captcha_token}`. Captcha-fail → HTTP 422 with `error_codes` list; captcha-not-configured → HTTP 501 (boot-safe); captcha-pass → HTTP 200 with `status="pending-arc-5"` (admin-row mint blocked because the `admins` table does not exist yet pre-Arc-5).
3. `settings.hcaptcha_secret_key` + `settings.hcaptcha_verify_url` + `settings.hcaptcha_site_key` reservations in `app/core/config.py` (all optional, all default to None — the route reads the secret and downgrades to 501 if absent).
4. `tests/api/test_signup_free_shape.py` — contract pin for the three response shapes (200 / 422 / 501) and the route's request-body validation.

**Method (actual, post-execution):** Code-only commit because the infra prerequisites for full closure (SSM secret provisioning, marketing-site widget, Arc 5 `admins` table) are not all available. The partial landing keeps the route shape immutable across the gap so WU-7 can wire the remaining surfaces without re-litigating the contract.

**Partner involvement:** Light — partner reviewed the route-shell shape and the contract test set; no per-line sign-off because the shape is reversible until WU-7 wires it live.

**Actual diff size:** One commit (`bf9abbc`), ~250 lines across `app/services/hcaptcha_service.py` (new) + `app/api/v1/billing.py` (route added) + `app/core/config.py` (three field reservations) + `app/schemas/billing.py` (request/response models) + `tests/api/test_signup_free_shape.py` (new).

### §3.6 — WU-6 — SES feedback-loop + suppression + IAM rightshape + sandbox exit (PENDING)

**Input:** Five OPEN drifts in the SES cohort: `D-ses-feedback-loop-not-wired-2026-05-22`, `D-ses-suppression-app-layer-not-implemented-2026-05-22`, `D-ses-iam-overgrant-unused-actions-2026-05-22`, `D-ses-sandbox-exit-pending-2026-05-22`, `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22`. The sandbox-exit ticket text is already authored at `arc3-out/B4-ses-sandbox-exit-request.md` (committed at `d46caa8` during Arc 3 B.4).

**Output:** A coordinated set of commits + AWS-infra changes:

1. **SNS topic + SES configuration set** — create `luciel-ses-events` SNS topic in `ca-central-1`; create SES configuration set `luciel-default` with event-destination pointing at the topic for `Bounce`, `Complaint`, `Reject`, `RenderingFailure`. AWS-infra only.
2. **`email_service.py` threading** — thread `ConfigurationSetName=luciel-default` into all three `send_email` call sites (L270, L442, L605 per the drift body). Thread `ReplyToAddresses=["support@vantagemind.ai"]` (or the agreed monitored inbox) into the same three call sites. Code commit.
3. **`app/api/v1/ses_events.py`** — receive the SNS HTTPS subscription; validate the SNS message signature; parse Bounce/Complaint/Reject/RenderingFailure events; write a row into a new `email_send_event` table; if the event is a `Bounce` or `Complaint`, also call `EmailSuppressionService.record_suppression(address, reason, source_event_id)`. Code commit.
4. **`email_suppression` table** — Alembic migration adding the table with columns `address`, `reason` (`HardBounce` / `Complaint` / `ManualBlock`), `first_suppressed_at`, `last_suppressed_at`, plus a unique index on `address`. **Coordination note:** this lands in Arc 5 Revision A as part of the V2-anchored schema sweep, OR as a standalone migration if Arc 5 sequencing makes it cleaner. Decision deferred to the WU-6 execution moment based on Arc 5's exact landing posture.
5. **`email_send_event` table** — Alembic migration with `event_id` (SNS message id), `event_type`, `address`, `received_at`, `raw_payload_json`. Same coordination note as above.
6. **`EmailSuppressionService`** — `app/services/email_suppression_service.py` with `is_suppressed(address) -> bool` and `record_suppression(address, reason, source_event_id) -> None`. The `is_suppressed` precheck lands at the top of every `email_service.py` send-call site, returning `SuppressedRecipientError` rather than calling `client.send_email`. Code commit.
7. **IAM policy rightshape** — edit the inline policy `LucielSESSendEmail` on `luciel-ecs-web-role` to drop `ses:SendBulkEmail` from the `Action` array while widening `Resource` to `arn:aws:ses:ca-central-1:729005488042:identity/*` per the post-sandbox-exit shape. One `iam put-role-policy` call.
8. **SES sandbox-exit ticket submission** — operator action; the ticket text is already authored. Track the case ID; the closure of `D-ses-sandbox-exit-pending-2026-05-22` is gated on AWS Support approval (typical 24-72h turnaround). The other four drifts can close before approval lands if the application-layer + IAM work is verifiable against the known-allowlist send paths.

**Method (planned):** Three commit phases:

- **Phase A — schema + service** (one commit, code-only, no deploy): Alembic migrations (if standalone path chosen), `EmailSuppressionService`, the precheck wiring in `email_service.py`, contract tests.
- **Phase B — SNS + SES configuration set + IAM rightshape** (one commit, AWS-infra changes documented + a `scripts/deploy_arc8_wu6.ps1` runbook): the AWS changes are operator-executed; the script captures the exact `aws` CLI invocations and the verification probes.
- **Phase C — `ses_events.py` route + paired deploy** (one commit, code + deploy): the SNS HTTPS endpoint wires up; paired backend redeploy; closure stanzas land in DRIFTS for the four drifts whose closure does not depend on sandbox-exit approval.

**Partner involvement:** **Drive end-to-end without partner pause per agent-locked judgment 3.** No mid-WU-6 handback. Partner sees the closure summary at WU-6 close and reviews before WU-7 begins.

**Estimated diff size:** ~600-800 lines of code (service + route + service-side hooks + migrations) + ~150 lines of `scripts/` runbook + the IAM JSON.

**Sandbox-exit dependency:** `D-ses-sandbox-exit-pending-2026-05-22` is the one drift whose closure cannot be forced at the WU-6 commit moment — it waits on AWS Support. The other four drifts close at WU-6; the sandbox-exit drift closes asynchronously when the AWS Support case resolves. The closure of WU-6 itself does **not** wait on AWS Support (the deliverability posture work is complete even if the sandbox-exit case is still open).

#### §3.6.X — Phase A + Phase B execution record (2026-05-22, Arc 8 resume)

**Phase A landed at commit `c3d974f` (origin/main, 2026-05-22 ~15:00 EDT) — 11 files / +2273 lines:**

- `alembic/versions/a91c4d2e7f08_arc8_wu6_email_send_event.py` — creates `email_send_event` (durable SES feedback record; `UNIQUE event_id` for SNS idempotency; `CHECK` on `event_type`)
- `alembic/versions/b2e5f17a3d9c_arc8_wu6_email_suppression.py` — creates `email_suppression` (`CHECK` on `reason`; FK `source_event_id` → `email_send_event.event_id` `ON DELETE SET NULL`; UNIQUE expression index `ux_email_suppression_lower_address` on `LOWER(address)`); chain: `b4d8a2e7c1f3` (Step 30a head) → `a91c4d2e7f08` → `b2e5f17a3d9c`
- `app/models/email_send_event.py` + `app/models/email_suppression.py` — SQLAlchemy ORM models for the two new tables
- `app/services/email_suppression_service.py` — `SuppressedRecipientError`, `is_suppressed(session, address)`, `record_suppression(session, address, reason, source_event_id=None, *, actor_label=None, note=None)`, `clear_suppression(session, address, *, actor_label=None, note=None)`. All mutating ops write tamper-evident `admin_audit_log` rows in the same session (chained by `app/repositories/audit_chain.py` `before_flush` handler)
- `app/services/email_service.py` — `_precheck_suppression(to_email, db, marker)` helper (fail-OPEN on lookup error so a precheck outage never blocks sends); precheck wired before every `client.send_email()` at all three send sites (magic-link, welcome-set-password, pilot-refund); `ConfigurationSetName=settings.ses_configuration_set_name` and `ReplyToAddresses=[settings.ses_reply_to_address]` threaded through the same three calls
- `app/core/config.py` — `ses_configuration_set_name: str = "luciel-default"` + `ses_reply_to_address: str = "support@vantagemind.ai"` settings
- `app/models/admin_audit_log.py` — five new constants whitelisted: `ACTION_EMAIL_SUPPRESSION_RECORDED`, `ACTION_EMAIL_SUPPRESSION_CLEARED`, `ACTION_EMAIL_SEND_EVENT_RECEIVED`, `RESOURCE_EMAIL_SUPPRESSION`, `RESOURCE_EMAIL_SEND_EVENT`
- `tests/services/test_email_suppression_service.py` (23 tests) + `tests/api/test_email_service_suppression_precheck.py` (24 tests). **Test surface: 47/47 new tests green; 94/94 across the email-related suite green.** Pre-existing unrelated test environment failures (celery import error in 2 `tests/integrity/test_worker_audit_failure_counter.py` cases; `database_url` env missing for 10 `tests/api/test_step30a_billing_shape.py` cases) verified at HEAD via `git stash` technique — not WU-6-caused.

**Phase B landed in AWS (B1+B2+B3 operational, B4 submitted) 2026-05-22 ~15:33–16:37 EDT:**

- **B1 — SNS topic created** (2026-05-22 ~15:25 EDT). `aws sns create-topic --name luciel-ses-events --region ca-central-1` returned `arn:aws:sns:ca-central-1:729005488042:luciel-ses-events`. Idempotent; verified.
- **B2.1 — SES configuration set created** (2026-05-22 ~15:31 EDT). `aws sesv2 create-configuration-set --configuration-set-name luciel-default --sending-options SendingEnabled=true --reputation-options ReputationMetricsEnabled=true --region ca-central-1` succeeded silently; `aws sesv2 get-configuration-set` confirmed `ConfigurationSetName: luciel-default`, `SendingEnabled: true`, `ReputationMetricsEnabled: true`.
- **B2.2 — Event destination attached** (2026-05-22 ~15:33 EDT). Event destination `luciel-feedback-to-sns`, enabled, `MatchingEventTypes: [BOUNCE, COMPLAINT, REJECT, RENDERING_FAILURE]`, `SnsDestination.TopicArn: arn:aws:sns:ca-central-1:729005488042:luciel-ses-events`. Verified via `aws sesv2 get-configuration-set-event-destinations`. Event types `SEND`/`DELIVERY`/`OPEN`/`CLICK` deliberately **excluded** (noise at current scale).
- **B3 — IAM rightshape on `LucielSESSendEmail`** (2026-05-22 ~15:40 EDT). Discovery surfaced the policy as an **inline policy on `luciel-ecs-web-role`**, not a customer-managed policy (the doctrine slug name was correct; my initial `iam list-policies --scope Local` query was wrong — self-correction logged in segment). Pre-change backup captured at operator-local `iam-backup-LucielSESSendEmail-pre-arc8-wu6.json` (716 bytes). New document written via `[IO.File]::WriteAllText` (Standard #10, BOM-safe), applied with `aws iam put-role-policy --role-name luciel-ecs-web-role --policy-name LucielSESSendEmail --policy-document file://iam-LucielSESSendEmail-new.json`. Diff: Sid `AllowSESSendFromVantagemindIdentity` → `AllowSESSendUnderLucielConfigSet`; `Action: [ses:SendEmail, ses:SendRawEmail, ses:SendBulkEmail]` → `[ses:SendEmail, ses:SendRawEmail]` (dropped); `Resource: […identity/vantagemind.ai, …identity/aryans.www@gmail.com]` → `[arn:aws:ses:ca-central-1:729005488042:identity/*]` (widened, region+account still pinned). Read-back via `aws iam get-role-policy` exact-match-verified.
- **B4 — SES sandbox-exit ticket submitted** (2026-05-22 ~16:37 EDT). AWS Support case **`177948223100786`** opened against Account ID `729005488042`, Subject "SES: Production Access", Severity `low`. The new streamlined AWS form (Mail type + Website URL + Acknowledgement only; legacy long-form fields removed by AWS) was used; substantive review surface is account-state introspection of the work landed in B1+B2+B3. Submission record at operator-local `arc3-out/B4-ses-sandbox-exit-request.md` (gitignored). Awaiting AWS async approval (24–72h typical SLA).

**Drift state after Phase A + Phase B execution (mirror of DRIFTS §3 SES cohort):**

- ~~`D-ses-iam-overgrant-unused-actions-2026-05-22`~~ — **RESOLVED** (B3 applied + verified)
- `D-ses-feedback-loop-not-wired-2026-05-22` — **PARTIALLY-RESOLVED** (infrastructure leg landed at B1+B2; HTTPS subscriber leg ships at Phase C)
- `D-ses-suppression-app-layer-not-implemented-2026-05-22` — **CODE-COMPLETE-AWAITING-DEPLOY** (commit `c3d974f`; deploys at Phase C)
- `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22` — **CODE-COMPLETE-AWAITING-DEPLOY-AND-MAILBOX-CONFIRM** (commit `c3d974f`; mailbox confirm operator-side)
- `D-ses-sandbox-exit-pending-2026-05-22` — **SUBMITTED-AWAITING-AWS** (case `177948223100786`)

**Self-correction logged this segment:** the initial agent summary of the product to partner described Instance as "upload-and-qualify" (batch enrichment processor). Partner caught the drift immediately. The vision — already correctly authored in CANONICAL_RECAP §1/§11 Q6/Q7/§13 T7/T8/T10/§14 — is: an Instance is an **autonomous AI agent deployed by the Admin on their own website / phone line / inbox**; Leads are the **output** captured by the Instance from those channels, not input uploaded into it. No doc change required (the doctrine is right; the drift was in agent prose only). Logged here as a self-audit beat for future Arc-8 segments to read cold.

**Phase C (still owed):** prod-deploy ceremony. (a) run the two Alembic migrations on prod RDS (`a91c4d2e7f08` + `b2e5f17a3d9c`); (b) build + push backend image #82 (`docker buildx build --platform linux/amd64 --provenance=false --sbom=false` per Standard #11); (c) update ECS service to image #82; (d) verify `/version` shows new git_sha; (e) author + deploy `app/api/v1/ses_events.py` route to receive SNS HTTPS subscription; (f) subscribe the route URL to the SNS topic; (g) end-to-end test: synthetic bounce → SNS publish → backend writes `email_send_event` + `email_suppression` rows → subsequent send to that address aborts cleanly with `SuppressedRecipientError`. Phase C closes `D-ses-feedback-loop-not-wired` and `D-ses-suppression-app-layer-not-implemented` fully; `D-ses-reply-to-monitored-inbox-not-confirmed` closes when operator confirms mailbox monitoring; `D-ses-sandbox-exit-pending` closes when AWS approves case `177948223100786`.

### §3.7 — WU-7 — Free-tier captcha end-to-end closure (PENDING — NEW)

**Input:** `D-free-tier-captcha-missing-2026-05-22` (partial landing at WU-5; full closure requires the remaining four surfaces named in the WU-5 partial closure note). Cross-refs: `D-tenancy-collapse-admin-instance-lead-2026-05-22` (parent — Admin→Instance→Lead canonical shape that the first-Instance provisioning surface belongs to); `D-same-admin-tier-transition-doctrine-hole-2026-05-22` (sibling — Free→Pro upgrade is the natural exit from the Free-tier surface this WU lands).

**Output:** A coordinated set of commits closing the captcha surface end-to-end:

1. **SSM SecureString `HCAPTCHA_SECRET_KEY` provisioning** — operator action; AWS Systems Manager Parameter Store key with the hCaptcha account's verify-side secret. Documented in a `scripts/deploy_arc8_wu7.ps1` runbook.
2. **Marketing-site widget wiring** — add the hCaptcha widget to the Free signup form on the marketing site. Marketing site is a separate repo / static site; this commit lives in the marketing-site repo, with a cross-ref note in this arc record. The widget reads `HCAPTCHA_SITE_KEY` from the marketing-site build config and submits `captcha_token` in the `POST /api/v1/billing/signup-free` request body.
3. **`TierProvisioningService.provision_free_admin(email, ip)`** — author the service entry point (consumed by the existing route shell from WU-5). The service mints an `admins` row, captures `last_signup_ip`, sends the welcome email via the (post-WU-6) deliverability stack, and runs the 1-per-IP soft gate. **Arc 5 dependency:** the `admins` table is created in Arc 5 Revision A; this service cannot land its full body until Arc 5 has shipped. **Coordination plan:** WU-7 Phase A lands the SSM key + the marketing widget + the route's secret-reading path while WU-7 Phase B (post-Arc-5) lands the `provision_free_admin` body. The route's response shape changes from `status="pending-arc-5"` to a 200 + Admin id at Phase B.
4. **`ADMIN_FREE_TIER_PROVISIONED` audit row** — add the action constant to `app/models/admin_audit_log.py`; emit the row from `provision_free_admin` with `{email_domain, ip_subnet}` field set for forensics. Chain-hashed per §4.3. Phase B alongside the service body.
5. **Soft-gate response shape** — captcha-pass + soft-gate-fail returns HTTP 409 with `error.code="free_tier_quota_exceeded"`; the website shows a "You've already created a Free account from this email domain or network. Sign in or upgrade to Pro." message. Phase B.
6. **Synthetic abuse test** — mint 10 Free Admins from the same /24 subnet; expect the soft gate to reject after the first; expect the audit row's `ip_subnet` field to match across all attempts. Phase B.

**Method (planned):** Two phases:

- **Phase A (pre-Arc-5)** — SSM key + marketing widget + route secret-reading. Closes the captcha-verify-pass-through-to-501 gap (route now returns 200 with `status="pending-arc-5"` to confirmed captcha-pass requests; previously the route was the same shape but unconfirmed because the secret was never readable).
- **Phase B (post-Arc-5)** — `provision_free_admin` body + audit constant + soft-gate response shape + synthetic abuse test. Closes the drift end-to-end.

**Partner involvement:** **Pause for partner review at WU-7 close** per agent-locked judgment 3. The pause is at the end-to-end closure moment (after Phase B), not between phases. If Arc 5 timing splits Phase A and Phase B with a long gap, the agent surfaces a status update but does not formally pause until the full closure.

**Estimated diff size:** Phase A ~50 lines (SSM key reading + marketing widget commit lives in separate repo). Phase B ~300-400 lines (service body + audit constant + soft-gate response + synthetic abuse test).

**Cross-arc coordination:** Phase B is technically post-Arc-5, which means it lands in the Arc 8 → Arc 5 → Arc 8 sequence. To avoid confusion at the audit-chain level, the WU-7 close commit lands in Arc 8's tag namespace (the closing tag `arc-8-security-hardening-complete` is cut at WU-8 close — see §3.8 — but the WU-7 close commit is attributed to Arc 8 regardless of where it falls in calendar time). Alternatively, if the Arc 5 ↔ Arc 8 timing makes the split awkward, the agent can opt to defer WU-7 Phase B to post-Arc-5 entirely and close WU-7 at Phase A boundary with the explicit "Phase B deferred to post-Arc-5" stanza; the decision is at the agent's judgment per the lifted gate.

### §3.8 — WU-8 — Enterprise metering hook implementation (PENDING — NEW)

**Input:** `D-enterprise-metering-not-implemented-2026-05-22` (filed during Arc 4 tier-shape revision pass, 2026-05-22-late, when the Enterprise hybrid billing model was committed in CANONICAL §11.7 and §14 + ARCHITECTURE §3.2.14 but the implementation layer carried none of it).

**Output:** Implementation of the ARCHITECTURE §3.2.14 design:

1. **`subscriptions.billing_model` enum column** — Alembic migration adding `subscriptions.billing_model VARCHAR(16) NULL` with in-migration `UPDATE` backfilling pre-existing rows to `'flat'`. Enum values: `flat` / `hybrid` / `consumption`. Lands in Arc 5 Revision A as part of the V2-anchored schema sweep.
2. **`admin_tier_overrides` table** — new table per ARCHITECTURE §3.2.14 with columns: `admin_id` (FK to `admins`), `billing_model`, `included_usage_per_period`, `overage_rate_cents`, `committed_use_discount_bps`, `period_start`, `period_end`, plus the standard audit columns. Lands in Arc 5 Revision A.
3. **`metering_emissions` cursor table** — new append-only table with `(admin_id, period, emission_ts)` primary key + `stripe_idempotency_key` + `quantity_emitted` columns. Lands in Arc 5 Revision A.
4. **`METERING_USAGE_EMITTED` audit action** — add the action constant to `app/models/admin_audit_log.py` with the standard chain-hashed field set per ARCHITECTURE §4.3.
5. **`app/workers/metering_worker.py`** — Celery beat worker per the ARCHITECTURE §3.2.14 design. Hourly schedule during a billing period and once at period close. Reads the metered unit's running total from the appropriate source-of-truth table (`leads.created_at` or `traces.created_at` per the Admin's configured unit in `admin_tier_overrides`); computes the delta since the last successful emission via the `metering_emissions` cursor; emits via `stripe.SubscriptionItem.create_usage_record(subscription_item_id, quantity=delta, action='increment')` with an idempotency key; writes the cursor row + the `METERING_USAGE_EMITTED` audit row in the same SQLAlchemy transaction.
6. **Celery beat schedule entry** — add the hourly schedule to `app/celeryconfig.py` (or equivalent); wire it into the same `--beat` worker container used by the retention purge worker (§3.2.4).
7. **Stripe Enterprise hybrid Price pair** — create the Enterprise hybrid product in Stripe: one recurring platform-fee Price + one metered usage Price on the same subscription (Stripe `recurring.usage_type='metered'` + `recurring.aggregate_usage='sum'` on the usage Price). Configure SSM keys `STRIPE_PRICE_ENTERPRISE_PLATFORM_FEE` and `STRIPE_PRICE_ENTERPRISE_USAGE_METERED`. **Coordination note:** the Stripe SKU set itself is Arc 6 scope; WU-8 reads the SSM keys but does not create the Stripe Prices. The Arc 6 ↔ Arc 8 sequencing means the WU-8 worker can land its code in Arc 8 against `None`-defaulting SSM reads, with the worker short-circuiting until the SSM keys are populated by Arc 6.
8. **`app/policy/entitlements.py` extension** — read `admin_tier_overrides.billing_model` for Enterprise Admins and select the correct gate path (static-map gate for Free/Pro; override-consulted gate for Enterprise).
9. **Unit + integration tests** — cover the four failure modes per the drift body: (a) Stripe API failure on emit leaves the cursor uncommitted and the next run retries the full delta; (b) idempotency-key collision is treated as success at the cursor level; (c) an Enterprise Admin with `billing_model='flat'` (negotiated flat-rate Enterprise) skips the emitter entirely; (d) the `consumption` enum value is reserved but not actively routed.

**Method (planned):** Three commit phases:

- **Phase A (Arc 5 Revision A)** — the three schema artifacts (`billing_model` column + `admin_tier_overrides` + `metering_emissions`) land in the Arc 5 V2-anchored schema sweep. Coordination is by reference, not by commit ordering within WU-8: the schema commits are Arc 5's, but they are sequenced to land before WU-8 Phase B.
- **Phase B (Arc 8)** — `app/workers/metering_worker.py` + Celery beat schedule entry + `METERING_USAGE_EMITTED` audit action + `app/policy/entitlements.py` extension + the four failure-mode tests. The worker boots with `None`-defaulting SSM reads (no Stripe Price configured yet) and short-circuits at the top of the loop until the SSM keys are populated.
- **Phase C (Arc 6)** — Stripe Enterprise hybrid Price pair creation + SSM key population. At this point the worker's short-circuit lifts and the first emission cycle runs.

**Partner involvement:** **Pause for partner review at WU-8 close** per agent-locked judgment 3. The pause is at the WU-8 Phase B close moment (the worker code is in `main` and the failure-mode tests pass even though Phase C has not yet happened); WU-8 closes against the drift's "design + scaffolding complete; live emission pending Arc 6 Stripe SKU" closure-evidence shape.

**Estimated diff size:** Phase A ~400 lines (Alembic migration + new table models). Phase B ~600-800 lines (worker + beat entry + audit constant + policy extension + four test suites). Phase C ~50 lines (SSM key reads + Stripe Price coordination).

**Cross-arc coordination:** Identical to WU-7's posture — the WU-8 close commit lands in Arc 8's tag namespace regardless of where it falls in calendar time relative to Arc 5 / Arc 6. The closing tag `arc-8-security-hardening-complete` is cut at the WU-8 Phase B close commit.

---

## §4 — Discipline and symmetry locks (carry-forward for every WU)

These rules govern every commit in Arc 8. Violating any of them re-opens the work-unit.

1. **Three-doc triangulation.** Every fact must appear in at least two of {CANONICAL_RECAP, ARCHITECTURE, DRIFTS}, from each doc's own angle. CANONICAL is the buyer-facing source; ARCHITECTURE is the engineer-facing source; DRIFTS is the integrity-and-debt source. Arc 8 commits must update at least DRIFTS (closure stanzas) and (where the surface changes the public posture) CANONICAL §12 row 28 or §11.7 / §14.
2. **Truth on first read.** Doctrine docs read as current truth at every line post-commit. Closure stanzas use `~~strikethrough~~` for the pre-closure body and live text for the closure verification. No version-history sediment in current text.
3. **Diagnostic-honesty.** When an agent diagnostic was wrong (see the WU-2 `id` vs `ps -eo` correction), record it honestly in the closure stanza. Agent errors are owned and documented; they are not erased from the audit chain.
4. **Surgical edits only.** No mass rewrites that obscure provenance. Every WU's commit message names exactly what changed and why. Closure stanzas preserve the pre-closure body under `~~strikethrough~~`.
5. **Partner sign-off where it matters.** Mid-WU partner pauses only at the agent-locked judgment points (WU-7 close, WU-8 close). The five LANDED work-units (WU-1 through WU-5) carry their partner-involvement notes inline. WU-6 drives end-to-end without pause.
6. **Six pillars enforced at every commit.** Scalability, reliability, maintainability, traceability, security, simplicity — every Arc 8 commit must improve at least one and degrade none. The WU-2 BuildKit hardening was a redundant deploy but is **kept** because it improves reliability (single-manifest images) and traceability (Stage 1b gate) without degrading any other pillar.
7. **Audit-chain integrity.** Every code commit that touches the audit-chain repository (`app/repositories/audit_chain.py`) must maintain the per-row hash chain invariant from Step 28 Phase 3 Commit 6 (the existing P3-E.2 chain). The post-Arc-5 rename sweep adjusts the import paths but not the chain semantics.
8. **Arc 8-before-Arc 5 sequence respected.** Arc 8 touches `app/middleware/*.py`, `app/repositories/audit_chain.py`, `app/main.py` files Arc 5 will rename. Arc 8 must close before Arc 5 begins (except for the explicit cross-arc coordination points named at WU-7 Phase B and WU-8 Phase A, where the schema-side work lands in Arc 5 Revision A by reference but the application-layer scaffolding is in Arc 8).

---

## §5 — Audit chain entry points

This arc opens at commit `02e1989` (the WU-2 / WU-3 paired deploy ceremony commit that landed Dockerfile USER + BUILD_GIT_SHA threading + the version endpoint payload superset). Closes at the WU-8 Phase B close commit stamping `arc-8-security-hardening-complete`. Every WU commit references this arc-record by path.

**LANDED commit chain:**

- `e691b19` — WU-1 misfiled gating drift closure (doc-only)
- `02e1989` — WU-2 + WU-3 worker-not-root + build-SHA observability (code; deploy paired)
- `eaa5bd3` — Arc 8 hygiene (gitignore arc3/4 operator scratch; preflight clean-tree gate for deploy_arc8.ps1)
- `74df825` — WU-2 patch: BuildKit hardening + Stage 1b paranoid gate (code; deploy paired)
- `488c0b9` — WU-4 closure stanzas for WU-2 + WU-3 + new BuildKit footgun drift (doc-only)
- `bf9abbc` — WU-5 free-tier captcha infra prep (code; no deploy)
- `c90b9f2` — Arc 8 doc-truthing: Q1 row rewrite + D-same-admin-tier-transition-doctrine-hole drift filed (doc-only)

**Arc 9 paused Arc 8 between `c90b9f2` and the next Arc 8 commit.** Arc 9 doctrine-rewrite arc commits (`e0f80da` through `722a945`) interleave in the git log but are not part of Arc 8's audit chain. Arc 9 close (tag `arc-9-doctrine-reanchor-complete` at `722a945`) re-opens Arc 8 for WU-6 / WU-7 / WU-8 under the V2-anchored doctrine.

**PENDING commit chain (planned shape):**

- **Doc-only scope-expansion commit** (this commit-pair) — lands this arc-record + updates CANONICAL §12 row 28 + cross-refs in DRIFTS.md for the WU-6 / WU-7 / WU-8 cohort.
- **WU-6 Phase A** — `email_suppression` service + precheck wiring (code; no deploy)
- **WU-6 Phase B** — SNS topic + SES configuration set + IAM rightshape (AWS-infra + runbook)
- **WU-6 Phase C** — `ses_events.py` route + paired backend redeploy (code + deploy); closure stanzas for the four non-sandbox-exit drifts
- **WU-7 Phase A** — SSM `HCAPTCHA_SECRET_KEY` + marketing widget + route secret-reading (operator + marketing-repo commit + small backend commit; no backend deploy if the secret-reading is already wire-clean)
- **WU-7 Phase B** — `provision_free_admin` body + audit constant + soft-gate response (code + deploy; post-Arc-5)
- **WU-8 Phase A** — three schema artifacts in Arc 5 Revision A (by reference, not by WU-8 commit)
- **WU-8 Phase B** — `metering_worker.py` + beat entry + audit constant + policy extension + tests (code + deploy; closing tag `arc-8-security-hardening-complete` cuts here)
- **WU-8 Phase C** — Stripe Enterprise hybrid Price pair + SSM key population (Arc 6 scope; lifts the worker's short-circuit)

**Triangulation symmetry check (deferred to Arc 8 close):** at the WU-8 Phase B close moment, walk every Arc 8 closure stanza in DRIFTS.md against the CANONICAL §12 row 28 status text and the ARCHITECTURE §3.2 + §3.2.14 + §4.7 surfaces. Confirm every fact appears in at least two of the three docs. Confirm no version-history sediment in current text. Confirm no agent-error stanza was erased.

**Umbrella tag:** `arc-8-security-hardening-complete` (or a final WU-named tag if a later WU is added). Tag stamps on the WU-8 Phase B close commit.
