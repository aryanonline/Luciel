# Arc 3 Work-Unit B.2 â€” JWT `kid` Rolling-Window Design Memo

**Date:** 2026-05-21
**Author:** VantageMind, paired with Aryan Singh
**Status:** DESIGN â€” awaiting partner approval before code touches land
**Closure drift:** `D-set-password-token-logged-plaintext-2026-05-17` (residual on 13 unmatched leaked JTIs)
**Closing tag:** `arc-3-paired-prod-touch`

---

## Problem

Today's `app/services/magic_link_service.py` uses HS256 with a **single
shared secret** loaded from `settings.magic_link_secret`. All four
token classes (`magic_link`, `session`, `set_password`,
`reset_password`) verify against the same secret. The module has:

- **no `kid` header** on `jwt.encode()`
- **no key-lookup table** on `jwt.decode()`
- **no key-versioning** in settings
- **no rotation runbook**

A naked rotation today (overwrite `magic_link_secret`, bounce the ECS
service) would atomically invalidate every signed-in customer's
30-day session cookie and every in-flight 24h set-password / reset-
password / magic-link token. That's not acceptable. The 13 leaked
JTIs from Arc 3 A.2a still need a closure path.

## Solution: `kid` rolling-window with grace period

Add a two-key rolling-window scheme:

- **PRIMARY (active) key** â€” used to sign new tokens. Carries a stable
  string `kid` like `"v2026-05-21"`.
- **SECONDARY (grace) key** â€” used **only for decode** of tokens minted
  before the last rotation. Carries the previous `kid`.
- **Active `kid` pointer** â€” settings string telling the minter which
  of the two keys is primary; the decoder uses the token's own `kid`
  header to look up the correct key from a two-entry table.

After a grace period (â‰¥ max token TTL = 30 days, the session-cookie
TTL), the secondary entry is removed and rotation is fully complete.

### Why two keys, not N

YAGNI. The auth surface is small (single backend issuer + single
backend verifier), token lifetimes are short (24h for ephemeral
classes, 30d for session cookies). One primary + one grace covers
every realistic rotation scenario. Step 32a self-serve identity can
expand to N if it ever needs to.

### Why a stable kid string, not a UUID or sequence number

Operator readability. When you read an audit row and see
`kid=v2026-05-21`, you immediately know when that token was minted.
A UUID kid would be opaque.

---

## Detailed contract

### Settings (`app/core/config.py`)

Add three new fields; deprecate one:

```python
# Legacy single-secret field. Still respected at decode time IFF
# jwt_signing_keys_json is empty (boot-time compatibility shim so
# this PR doesn't require simultaneous SSM and code update). Removed
# in Step 32a.
magic_link_secret: str = ""

# New: JSON map of {kid: secret}. In prod, populated from SSM under
# /luciel/production/jwt_signing_keys_json (SecureString JSON blob). Exactly two
# entries during a rotation; one entry steady-state.
#
# Example: {"v2026-05-21": "<primary-secret>", "v2025-08-12": "<grace-secret>"}
jwt_signing_keys_json: str = ""

# New: the kid that the minter should use for newly-issued tokens.
# Must be a key in jwt_signing_keys_json. Empty falls through to
# legacy magic_link_secret behavior.
jwt_active_kid: str = ""

# New: optional grace-period kid; advisory only (the decoder accepts
# any kid present in jwt_signing_keys_json). Recorded so the
# rotation runbook can assert "we are mid-rotation" vs. "we are
# steady-state".
jwt_grace_kid: str = ""
```

### Service (`app/services/magic_link_service.py`)

1. **New module-level helper `_resolve_keys() -> dict[str, str]`**
   * Parses `settings.jwt_signing_keys_json` once per process (cached).
   * If empty AND `settings.magic_link_secret` is set, returns
     `{"legacy": settings.magic_link_secret}` and sets the active-kid
     fallback to `"legacy"`. This is the boot-time shim that lets the
     code ship before the SSM blob lands.
   * If both are empty, raises `MagicLinkError("no signing key
     configured")` â€” fail closed, same posture as today.

2. **New module-level helper `_active_kid() -> str`**
   * Returns `settings.jwt_active_kid` if set, else `"legacy"`.
   * Raises if the active kid is not a key in `_resolve_keys()`.

3. **Mint paths (all four)** â€” add `headers={"kid": _active_kid()}` to
   `jwt.encode()`. Otherwise unchanged.

4. **Decoder `_decode()`** â€” rewrite the key-resolution leg only:
   ```python
   keys = _resolve_keys()
   try:
       unverified_header = jwt.get_unverified_header(token)
   except jwt.InvalidTokenError as exc:
       raise MagicLinkError("Invalid token.") from exc
   kid = unverified_header.get("kid", "legacy")
   secret = keys.get(kid)
   if secret is None:
       # Unknown kid means the token was signed with a key we have
       # since retired. Treated identically to a bad signature so a
       # probing client cannot distinguish "wrong kid" from "wrong
       # signature" (the existing posture).
       raise MagicLinkError("Invalid token.")
   # ... rest of jwt.decode(token, secret, algorithms=[...], ...) unchanged.
   ```

5. **`_secret_or_fail()`** â€” keep the function but redirect it to
   return `_resolve_keys()[_active_kid()]`. This preserves the e2e
   test fixture contract (`tests/e2e/step_30a_4_team_invite_live_e2e.py`
   imports `_secret_or_fail` to re-mint tokens with stamped jti). The
   helper's semantics shift from "the only secret" to "the secret a
   fresh mint would use right now," which is the right semantics for
   what the tests are actually doing.

### SSM contract (prod)

| Param path | Type | Shape |
|---|---|---|
| `/luciel/production/jwt_signing_keys_json` | SecureString | JSON: `{"v2026-05-21": "...", "v2025-08-12": "..."}` |
| `/luciel/production/jwt_active_kid` | String | `v2026-05-21` |
| `/luciel/production/jwt_grace_kid` | String (optional) | `v2025-08-12` during rotation; empty steady-state |
| `/luciel/production/magic_link_secret` | SecureString | **DEPRECATED** â€” kept until Step 32a for boot-time shim |

ECS task-def `secrets:` block adds three entries for `JWT_SIGNING_KEYS_JSON`,
`JWT_ACTIVE_KID`, `JWT_GRACE_KID`. The existing `MAGIC_LINK_SECRET`
entry stays for the shim.

### Backward compatibility

The shim is the critical piece. Order of operations:

1. **Ship the code** (no SSM changes yet). Behavior: `_resolve_keys()`
   sees empty `jwt_signing_keys_json`, falls back to
   `{"legacy": settings.magic_link_secret}`. Active kid = `"legacy"`.
   New tokens are minted with `kid="legacy"`. Old tokens (no kid
   header) decode under `kid="legacy"` default. **Zero behavior
   change for users.**

2. **Write the new SSM params** (with `magic-link-secret` still
   populated as `v2025-08-12` for grace). Re-deploy. New tokens minted
   with `kid="v2026-05-21"`. Tokens minted before the deploy (no kid
   header) decode under `kid="legacy"`, but **the `legacy` key value
   must be the same string as `v2025-08-12`** â€” i.e., the JSON map
   must include `"legacy": "<old-secret>"` for the first rotation only.

3. **Mint a fresh secret for `v2026-05-21`**, write it to the JSON
   blob alongside the legacy/v2025-08-12 entry, promote
   `jwt_active_kid=v2026-05-21`, re-deploy. New tokens are now signed
   with the new key. Old tokens still decode (because `legacy`/
   `v2025-08-12` are still in the map).

4. **Wait 30 days** (longest live token TTL).

5. **Remove the legacy/v2025-08-12 entry** from the JSON blob,
   clear `jwt_grace_kid`, re-deploy. Rotation complete.

After Step 32a ships, the `magic_link_secret` field + `legacy`
fallback get removed.

---

## Test plan

New file: `tests/security/test_jwt_kid_rolling.py`

1. **`test_mint_uses_active_kid`** â€” set `jwt_signing_keys_json={"a": "secret-a"}` + `jwt_active_kid="a"`; mint a token; assert the unverified header carries `kid="a"`.
2. **`test_decode_with_secondary_key`** â€” set two keys `{"a": "secret-a", "b": "secret-b"}`, active=`"b"`; mint with active `b`, then manually re-mint a token with kid=`a` using PyJWT; assert both decode correctly.
3. **`test_decode_rejects_unknown_kid`** â€” mint a token with kid=`"unknown"` using PyJWT; assert `_decode()` raises `MagicLinkError`.
4. **`test_legacy_shim_no_kid_header`** â€” set `magic_link_secret="legacy-secret"` and leave `jwt_signing_keys_json` empty; manually mint a token WITHOUT kid header (using PyJWT directly); assert it decodes under the `legacy` fallback.
5. **`test_legacy_shim_then_kid_promotion`** â€” set `magic_link_secret="OLD"`, `jwt_signing_keys_json={"legacy": "OLD", "v2": "NEW"}`, `jwt_active_kid="v2"`; assert the minter uses `v2`; assert a kidless legacy token still decodes.
6. **`test_no_keys_configured_fails_closed`** â€” both empty; assert `_decode()` raises.
7. **`test_active_kid_not_in_map_fails_closed`** â€” `jwt_active_kid="missing"`; assert mint raises.

E2E test fixture compatibility:

- `tests/e2e/step_30a_4_team_invite_live_e2e.py` re-imports
  `_secret_or_fail` to re-mint replay tokens with a stamped jti.
  After our refactor `_secret_or_fail()` returns the active-kid's
  secret (semantically: "what would a fresh mint use right now"),
  which is exactly what the test wants. No test change needed.

---

## Six-pillar check

- **Scalability** â€” Two-entry key map; O(1) decode lookup. Caches resolved keys at module level so we parse the JSON once per process boot.
- **Reliability** â€” Boot-time shim means we can ship the code change BEFORE the SSM blob exists. Zero deploy-ordering hazards. Rotation ceremony is a 5-step runbook with a 30-day grace window, not a guess.
- **Maintainability** â€” The contract lives in three places: `config.py` field docs, this design memo, and the new test file. `_resolve_keys()` is the single chokepoint for "which secret signs which kid".
- **Traceability** â€” Every minted token now carries a `kid` claim that says **when** it was issued. Audit rows that capture token payloads get a free temporal fingerprint.
- **Security** â€” Unknown-kid is collapsed to `"Invalid token."` (no oracle). Removing a key from the map is the atomic revocation primitive â€” we did not have this before. The shim does NOT widen the surface (legacy fallback only activates when the new JSON blob is empty).
- **Simplicity** â€” Two keys, one active pointer, one grace pointer. No N-key generalization, no rotation worker, no kid-as-UUID. Just a date-stamped string. The complexity buys exactly one capability: rotate the signing key without forcing every customer to re-login.

## Out of scope (deferred to Step 32a, intentionally)

- Database-backed JWT blacklist (still relies on TTL + one-shot-by-convention for class-level replay protection).
- RS256 / asymmetric keys (HS256 remains correct for single-issuer + single-verifier).
- Automatic / scheduled rotation (manual runbook is fine at this scale; automation lands when we have more than one prod environment to coordinate).
- Per-tenant key isolation (no business case).

## Risks honestly named

1. **The boot-time shim is load-bearing.** If the code lands but the SSM blob is set incorrectly on the first deploy after, we could still get a wrong-secret bounce. Mitigation: end-to-end smoke test in Work-Unit B.2.5 covers the shim path before we touch SSM.
2. **PyJWT's `get_unverified_header()` is a separate parse call.** Adds ~one regex's worth of CPU per decode. At our traffic volume this is invisible. At 10k QPS we'd cache; we're not at 10k QPS.
3. **The two-key map is JSON-in-a-SecureString.** A typo at SSM write time fails closed (we can't decode) but doesn't expose anything. A typo in the `jwt_active_kid` pointer also fails closed (mint raises). No silent-corruption mode exists.

---

## Amendment — 2026-05-21 (post-B.2.6)

The "SSM contract (prod)" table originally specified `/luciel/jwt-*` paths
with hyphens. The actual prod SSM convention is `/luciel/production/<snake_case>`
— confirmed during B.2.6 pre-flight (`describe-parameters` showed
`magic_link_secret` under `/luciel/production/`, not `/luciel/`). The table
above has been corrected in-place to reflect what was actually populated.

This amendment also notes the post-incident standard adopted in B.2.6:
**all `put-parameter` operations on SecureString JSON payloads MUST use
`--value file://<path>`**, never an inline `--value '<json>'`. The
in-flight shell layers (PowerShell + bash + heredocs) cannot be relied
upon to preserve the JSON's double-quote characters. Verification is by
`get-parameter` round-trip with sha256 match. See
`arc3-out/B2-6-cutover-record.md` for the full incident narrative.

Drifts closed: `D-b2-design-memo-ssm-paths-wrong-2026-05-21`.