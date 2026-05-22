# Arc 3 Work-Unit B.2.6 — Cutover Ceremony (Incident-Bisected, Green Smoke)

**Date:** 2026-05-21
**Author:** VantageMind, paired with Aryan Singh
**Status:** COMPLETE — `:78` + `:33` live, three-pillar in-container smoke `all_ok=true`
**Preceded by:** B.2.5 (zero-behavior-change deploy on `:77` + `:32`)

---

## Goal

Promote `v2026-05-21` to active signing kid by populating SSM, rolling backend + worker task-defs to revisions that consume the new params, and verifying with an in-container smoke that:

1. `_active_kid() == "v2026-05-21"`
2. All four mint paths stamp the new kid
3. Tokens minted under the previous (legacy) key still decode via the grace path
4. Kidless (pre-B.2) tokens still decode via `_LEGACY_KID` fallback

## Sequence (final, post-incident)

### Phase 1 — Stage SSM (PASSED on 2nd attempt)

| Param path | Type | Final version | Notes |
|---|---|---|---|
| `/luciel/production/jwt_signing_keys_json` | SecureString | v2 | byte-for-byte sha256 round-trip verified |
| `/luciel/production/jwt_active_kid` | String | v1 | value `v2026-05-21` |
| `/luciel/production/jwt_grace_kid` | String | v1 | value `legacy` |
| `/luciel/production/magic_link_secret` | SecureString | v2 | rotated as part of incident remediation |

### Phase 2 — Register task-defs

- `luciel-backend:78` — image `@b4c145eb…214c4dff`, 17 secrets (14 + 3 JWT_*)
- `luciel-worker:33` — same image, 7 secrets (4 + 3 JWT_*)

### Phase 3 — Cutover flips

- Backend `:77 → :78` flip #1: `services-stable` in ~150s, app booted, BUT `_resolve_keys()` raised `MagicLinkError` on every JWT op. **Rolled back to `:77` (175.4s).**
- Backend `:77 → :78` flip #2 (after Phase 1 redo): `services-stable` in 157.6s, app booted clean, three-pillar smoke green.
- Worker `:32 → :33`: `services-stable` in 157.0s, HEALTHY.

## Incident: malformed SSM JSON

| Field | Value |
|---|---|
| Identifier | `D-b2.6-stored-jwt-signing-keys-json-not-valid-json-2026-05-21` |
| Severity | P0 — auth fully broken |
| Customer-impact window | 23:01:01 → 23:15:?? EDT (~14 min) |
| Detection | In-container three-pillar smoke (Pillar 1: `_resolve_keys()` raises) |
| Recovery | Roll back to `:77` (175.4s `services-stable`) |
| Root cause | Block 7c (initial Phase 1 staging) used a nested heredoc: `aws ecs exec -- bash -c "python3 -c '...boto3.put_parameter(Value=json_str)...'"`. The outer shell layer stripped the inner JSON `"` characters before they reached Python. Stored value: `{legacy:KEY1,v2026-05-21:KEY2}` — JS-object-literal shaped, not valid JSON. |
| Diagnosis evidence | `Get-Parameter` returned 130 bytes (expected ~158). First 11 bytes hex: `7b 6c 65 67 61 63 79 3a 32 4c 48` = `{legacy:2LH` — all four double-quotes missing. Local .NET `ConvertFrom-Json` reproduced the same failure mode as the container. |
| Secondary exposure | The decoded error from `_resolve_keys` revealed the first ~43 chars of the legacy `MAGIC_LINK_SECRET` in this session's scrollback. New drift `D-magic-link-secret-exposed-in-session-scrollback-2026-05-21` opened, closed in the same remediation pass via rotation. |

### Remediation (FIX-1 / FIX-2 / FIX-3)

- **FIX-1** — Generated two fresh 32-byte (64-hex-char) keys via .NET `RandomNumberGenerator` directly into PowerShell variables. Built a `[ordered]` keymap, serialized with `ConvertTo-Json -Compress`, wrote to a UTF8-no-BOM file. Local parse confirmed valid JSON. Hex check: first 11 bytes `7b 22 6c 65 67 61 63 79 22 3a 22` = `{"legacy":"`. Quote characters present.
- **FIX-2** — `aws ssm put-parameter --value file://<keymap-path> --overwrite` for `jwt_signing_keys_json` → v2. Same for `magic_link_secret` (rotated the leaked legacy secret to a fresh 64-char hex). Read both back; **sent file sha256 == verify file sha256** (byte-for-byte SSM round-trip — the missing gate from FIX-0).
- **FIX-3** — Local key files overwritten with random bytes, then deleted. In-memory PowerShell variables cleared.

## Final in-container smoke (`all_ok=true`)

| Pillar | Result |
|---|---|
| 1 — keymap | `active_kid="v2026-05-21"`, kids loaded `["legacy", "v2026-05-21"]` |
| 2 — mints (all 4) | each: `kid_in_header="v2026-05-21"`, `roundtrip_ok=true`, `sub_matches=true`, `typ_matches=true`; token lengths 404 / 414 / 436 / 404 |
| 3 — grace decode | synthetic token signed under `keys["legacy"]` with `kid="legacy"` header decoded cleanly to `typ="magic_link"` |
| 4 — kidless backward compat | token with NO `kid` header decoded via `_LEGACY_KID` fallback |

## Standards adopted from this incident

1. **All SSM secret puts MUST use `--value file://…`**. Inline `--value '…'` is banned for JSON or any structured payload.
2. **All SSM secret puts MUST be followed by a `get-parameter` round-trip + sha256 match** before any service consumes the new value. The bytes you shipped are not necessarily the bytes that landed.
3. **All in-container smokes MUST be preceded by a module-inspection pass** that prints actual function signatures + source for the code under test. The v1 smoke called `mint_magic_link_token` positionally; the real signature is keyword-only. False-negative pillar failures cost ~10 min of misdiagnosis.

## Closed drifts in this commit

- `D-set-password-token-logged-plaintext-2026-05-17` — phase 1 done (`v2026-05-21` is now active); phase 2 (remove `legacy` key from map) scheduled +30 days, longest live token TTL.
- `D-magic-link-secret-exposed-in-session-scrollback-2026-05-21` — closed via FIX-2 v2 rotation.
- `D-b2-design-memo-ssm-paths-wrong-2026-05-21` — design memo amended in this commit (SSM paths corrected to `/luciel/production/jwt_*`).
- `D-b2.6-stored-jwt-signing-keys-json-not-valid-json-2026-05-21` — closed via FIX-2 file:// + round-trip.
- `D-b2.6-smoke-v1-script-wrong-2026-05-21` — closed via corrected smoke `kid_smoke2.py`.

## Opened drifts (deferred to Arc 7 / Arc 8)

- `D-untracked-file-backlog-pre-b2.6-2026-05-21` — 38 untracked files in working tree (Arc 7).
- `D-no-container-healthcheck-on-backend-2026-05-21` — worker has one, backend doesn't (Arc 8).
- `D-worker-log-stream-name-mismatch-2026-05-21` — log group naming convention drift (Arc 7).

## Production state at commit

| Service | Task-def | Image | Status |
|---|---|---|---|
| `luciel-backend-service` | `:78` | `@b4c145eb…214c4dff` | 1/1 RUNNING |
| `luciel-worker-service` | `:33` | same | 1/1 RUNNING / HEALTHY |

## Six-pillar check

- **Scalability** — Unchanged. `_resolve_keys()` lru-cached per process.
- **Reliability** — Validated: smoke caught the malformed-SSM incident before the cutover record went out, rollback worked first try, retry came up green.
- **Maintainability** — Phase 2 (legacy demote) is a single SSM put + service restart, scheduled by date.
- **Traceability** — Every freshly-minted token carries `kid="v2026-05-21"`. Audit rows now have a free temporal fingerprint.
- **Security** — Old leaked legacy secret rotated. Unknown-kid still collapses to `Invalid token.` (no oracle).
- **Simplicity** — Two keys, one active pointer, one grace pointer. Same shape the design memo specified.