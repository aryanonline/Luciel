# Arc 3 Work-Unit B.2.5 — Deploy + Auth Smoke Record

**Date:** 2026-05-21 EDT
**Operator:** Aryan (paired with VantageMind agent)
**Code under deploy:** commit `14fee6f` — "JWT kid rolling-window remediation (zero-behavior-change deploy)"
**Outcome:** ✅ GREEN. Cluster on B.2 code. Shim active. No customer impact. No SSM touched.

## Deploy chronology

| Step | Detail |
|---|---|
| Commit | `14fee6f` pushed to `origin/main` (range `6ba2f54..14fee6f`) |
| Local pytest (Windows venv) | `tests/security/test_jwt_kid_rolling.py` — **9 passed, 8 warnings, 0.27s** |
| Image build | `arc3_ecs_oneshot.ps1 -Stage build` — pushed `luciel-backend:arc3-prod-ops` |
| Image digest | `sha256:b4c145eb3f876f30fec947e7d58080c570eabf5ddce587815eb28d98214c4dff` |
| ECR re-tag attempt | `b2-kid-rolling-20260521` push produced **double-index** anomaly (see drift below) |
| Task-def: `luciel-backend:77` | registered, image pinned by digest |
| Task-def: `luciel-worker:32`  | registered, image pinned by digest |
| `update-service` backend  | `:76 → :77`, stabilized in **159.3s** |
| `update-service` worker   | `:31 → :32`, stabilized in **142.9s** |
| Backend boot log tail | clean — `Application startup complete`, no `MagicLinkError` / `NoSigningKeyError` / `pydantic.ValidationError` |

## In-container smoke (paired ECS Exec)

Task: `arn:aws:ecs:ca-central-1:729005488042:task/luciel-cluster/fba01d2e636345db8f8e1da69b844582`
Container: `luciel-backend`
Session: `ecs-execute-command-sr5ishdnr4oaahb3zhiea3sia8`

### Shim state

```
active_kid: legacy
kids present: ['legacy']
legacy_kid_constant: legacy
alg: HS256  iss: luciel-backend
```

Confirms: `jwt_signing_keys_json=""` + populated `MAGIC_LINK_SECRET` → fabricated `{"legacy": MAGIC_LINK_SECRET}` map with `_active_kid()="legacy"`. Zero-behavior-change deploy as designed.

### Mint + header inspection

| Token class | header.kid | header.alg | header.typ |
|---|---|---|---|
| `magic_link`     | `'legacy'` | `'HS256'` | `'JWT'` |
| `session`        | `'legacy'` | `'HS256'` | `'JWT'` |
| `set_password`   | `'legacy'` | `'HS256'` | `'JWT'` |
| `reset_password` | `'legacy'` | `'HS256'` | `'JWT'` |

### Decode round-trip (kid-aware)

| Token class | decode | typ | iss_match | email_match |
|---|---|---|---|---|
| `magic_link`     | OK | `magic_link`     | True | True |
| `session`        | OK | `session`        | True | True |
| `set_password`   | OK | `set_password`   | True | True |
| `reset_password` | OK | `reset_password` | True | True |

### Cross-class confusion guard

```
cross_class    correctly_rejected  MagicLinkError: Wrong token class.
```

The existing `typ` binding check (decode of a `session` token under `expected_typ=magic_link`) still raises. B.2 did not weaken cross-class security.

### Composite signal

```
all_ok = True
```

## What this proves

1. **Shim contract works in prod** — boot-time fallback to `{"legacy": MAGIC_LINK_SECRET}` is the active path.
2. **kid stamping is live** — all four mint classes write `kid="legacy"` into the JWT header.
3. **kid-aware decode works** — `_decode()` reads the unverified header, looks up the active key, and verifies.
4. **Legacy posture preserved** — pre-B.2 kidless tokens (none expected in fresh mints, but covered by unit test 4) would still decode via the `_LEGACY_KID` fallback path.
5. **Cross-class guard intact** — the `typ` check is independent of kid.
6. **Zero customer impact** — no public-ALB hits, no magic-links emailed, no DB rows written for the smoke.

## Drifts logged

| Drift ID | Status | Description |
|---|---|---|
| `D-b2-ecr-double-index-2026-05-21` | open, cosmetic | Re-tagging `b2-kid-rolling-20260521` via PowerShell `Out-File`-reformatted manifest produced a second OCI image index (`sha256:2fd9e6ed…`) pointing at the same platform manifest (`sha256:9e936c58…`) as the original (`sha256:b4c145eb…`, untagged but referenced by task-defs `:77`/`:32`). Resolve in Arc 7. |

## What this does NOT yet close

- `D-set-password-token-logged-plaintext-2026-05-17` remains **open**. The 13 unmatched leaked set-password JTIs (A.2b scan summary) are still verifiable, because the signing key (`MAGIC_LINK_SECRET` aliased as `kid=legacy`) is unchanged. Closure requires **B.2.6 cutover**: mint a new secret, write to SSM, promote a new kid as active, demote `legacy` to grace, and 30 days later drop `legacy` from the keys map.
- Worker exec smoke not run. The worker task definition is on `:32` with the new image, but the worker tasks (`memory_extraction`, `retention`) do not import `magic_link_service`, so no JWT path exists to smoke there. Drift hygiene satisfied by symmetric image rev, no functional test needed.

## State at end of B.2.5

| Field | Value |
|---|---|
| `git log -1` | `14fee6f wip-arc-3-work-unit-b-2: JWT kid rolling-window remediation` |
| ECR digest in service | `sha256:b4c145eb3f876f30fec947e7d58080c570eabf5ddce587815eb28d98214c4dff` |
| `luciel-backend-service` task-def | `luciel-backend:77` (running 1/1) |
| `luciel-worker-service` task-def  | `luciel-worker:32` (running 1/1) |
| `_active_kid()` | `legacy` |
| `_resolve_keys()` keys | `['legacy']` |
| `jwt_signing_keys_json` SSM param | NOT YET CREATED |
| `jwt_active_kid` SSM param | NOT YET CREATED |
| `jwt_grace_kid` SSM param | NOT YET CREATED |

## Next step: B.2.6 cutover ceremony (paired prod-touch)

See `arc3-out/B2-kid-rolling-design.md` §4 ("Cutover Ceremony") for the full sequence. Summary:

1. Generate fresh 32-byte signing key (`openssl rand -hex 32`).
2. Compose JSON: `{"legacy":"<old-MAGIC_LINK_SECRET>", "v2026-05-21":"<new-key>"}`.
3. `aws ssm put-parameter` SecureString: `/luciel/jwt-signing-keys`, `/luciel/jwt-active-kid=v2026-05-21`, `/luciel/jwt-grace-kid=legacy`.
4. Register `luciel-backend:78` + `luciel-worker:33` with new `secrets:` block (`JWT_SIGNING_KEYS_JSON`, `JWT_ACTIVE_KID`, `JWT_GRACE_KID`).
5. `update-service` both, `wait services-stable`.
6. Smoke: confirm `_active_kid()` reads `v2026-05-21` and fresh mints stamp `kid='v2026-05-21'`; confirm a token minted before the cutover (kid=`legacy`) still decodes during grace.
7. 30-day grace window observed; then re-do steps 4-6 with `legacy` removed from the keys JSON. At that point the 13 unmatched A.2b leaked JTIs become permanently unverifiable, closing `D-set-password-token-logged-plaintext-2026-05-17`.
