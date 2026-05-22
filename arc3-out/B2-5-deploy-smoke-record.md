# Arc 3 Work-Unit B.2.5 — Zero-Behavior-Change Deploy + Initial Smoke

**Date:** 2026-05-21
**Author:** VantageMind, paired with Aryan Singh
**Status:** COMPLETE (record reconstructed in B.2.6 commit; the work landed live this session before B.2.6 began)
**Preceded by:** B.2.4 (9 unit tests for kid-rolling, all green)
**Follows into:** B.2.6 (SSM cutover ceremony)

---

## Goal

Ship the kid-rolling code (B.2 / commit `14fee6f`) into prod **without** any user-visible behavior change. The shim path inside `_resolve_keys()` fabricates `{"legacy": <MAGIC_LINK_SECRET>}` when `jwt_signing_keys_json` is empty, so the code can land before the SSM blob exists.

## What deployed

| Component | Identifier |
|---|---|
| Image tag | `arc3-prod-ops` |
| Image digest | `sha256:b4c145eb3f876f30fec947e7d58080c570eabf5ddce587815eb28d98214c4dff` |
| Backend task-def | `luciel-backend:77` |
| Worker task-def | `luciel-worker:32` |
| Backend service | 1/1 RUNNING |
| Worker service | 1/1 RUNNING (HEALTHY — has container healthcheck) |
| Image cardinality | Symmetric: backend + worker share the same image |

## Verification

- Boot log: clean — Uvicorn started, application startup complete, ALB `/health` 200 OK from two ALB nodes.
- No `MagicLinkError`, no `_resolve_keys` traceback at boot.
- Live API responding on `https://api.vantagemind.ai`.
- `MAGIC_LINK_SECRET` still wired via `valueFrom` SSM SecureString — shim path active.
- Worker `services-stable` time: 142.9s.

## Six-pillar posture

- **Reliability** — Shim fallback validates the load-bearing claim from the design memo (code can ship before SSM blob).
- **Security** — No surface widening; the new env-vars are absent, code falls back to the existing single-secret path.
- **Maintainability** — One commit, one image, one shim contract.
- **Traceability** — Image digest pinned; service references it by digest.
- **Simplicity** — Single env-var-driven branch in `_resolve_keys()`.
- **Scalability** — Untouched.

## What this enables

B.2.6 — promote `v2026-05-21` to active by populating SSM and rolling task-defs.

## Drifts touched

- `D-set-password-token-logged-plaintext-2026-05-17` — pre-rotation phase done, awaiting B.2.6 to flip active kid.