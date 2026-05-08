# Phase 1g — PIPEDA scope hardening

Reconstructed from code citations and resolution commits on `step-29y-impl`. See [`README.md`](./README.md) for methodology.

`G-*` findings are PIPEDA-significant scope and authorization gaps in tenant-scoped routes. Cluster 1 (G-3..G-7), Cluster 2 (G-2), and Cluster 6 (G-1) collectively close them.

## G-1 — Chat `/stream` SSE error frame leaks internal state

### Code citations
- `app/api/v1/chat.py:110` — "findings_phase1g.md G-1 for the documented attack."
- `tests/api/test_step29y_cluster6_chat_stream_sanitization.py:3` — "Static + behavioral tests for findings_phase1g.md G-1: the SSE..."

### Resolution commits (on `step-29y-impl`)
- `9981235` — Step 29.y Cluster 6 (G-1): sanitize chat_stream SSE error frame
- `3591eef` — Step 29.y Cluster 6: tests for G-1 chat_stream sanitization

### Reconstructed summary

A streaming chat error frame returned a raw exception representation on the SSE channel, which under specific failure modes leaked DB query fragments, SQL parameter values, or internal route paths. Cluster 6 sanitizes the error-frame body to a stable error code + opaque correlation id; details only appear in server-side logs.

## G-2 — Forensic-toggle deactivation didn't cascade to memories

### Code citations
- `tests/api/test_step29y_cluster2_g2_cascade.py:3` — "AST + bytes tests for findings_phase1g.md G-2"
- `tests/api/test_step29y_cluster2_g2_cascade.py:117` — "(PIPEDA P5 hole, findings_phase1g.md G-2)"
- `app/api/v1/admin_forensics.py:805` — "That is the exact PIPEDA P5 hole findings_phase1g.md G-2 calls"

### Resolution commits (on `step-29y-impl`)
- `1975b25` — Step 29.y Cluster 2 (G-2): cascade memory deactivation in forensic toggle
- `0d05c15` — Step 29.y Cluster 2: AST tests for G-2 forensic-toggle memory cascade

### Reconstructed summary

The Step 24.5 admin forensic toggle on `LucielInstance` did not cascade `active=False` to dependent `memory_items` rows. Memory writes after a toggle continued to succeed under the deactivated instance, violating PIPEDA Principle 5 (limiting use). Cluster 2 implements the cascade and pins it with AST and behavioral tests.

## G-3 — `ConsentRequest.tenant_id` was required, blocking valid platform-scoped grants

### Code citations
- `app/api/v1/consent.py:15` — "See findings_phase1g.md G-3 for the four documented..."
- `tests/api/test_step29y_cluster1_scope_hardening.py:5` — "findings_phase1g.md G-3, G-4, G-5, G-6, G-7"

### Resolution commits (on `step-29y-impl`)
- `80ce98f` — Step 29.y Cluster 1 (G-3): make ConsentRequest.tenant_id Optional
- `51f25a4` — Step 29.y Cluster 1 (G-3 + G-7): rewrite consent.py with scope enforcement
- `4551f70` — Step 29.y Cluster 1: register Cluster-1 audit actions and resources

### Reconstructed summary

`ConsentRequest.tenant_id` was required at the schema level, blocking legitimate platform-admin-issued consent grants where the consenter has no tenant binding yet. Cluster 1 makes it optional and rewrites the consent route to perform full scope enforcement (`enforce_tenant_scope`, with platform_admin bypass).

## G-4 — Sessions routes lacked tenant-scope enforcement

### Code citations
- `tests/api/test_step29y_cluster1_scope_hardening.py:5` — "G-4"

### Resolution commits (on `step-29y-impl`)
- `ee88d35` — Step 29.y Cluster 1 (G-4): harden sessions routes with scope enforcement

### Reconstructed summary

Several `/sessions` routes performed authentication but did not invoke `ScopePolicy.enforce_tenant_scope`, allowing a tenant-scoped key to read or list sessions belonging to other tenants. Cluster 1 wires the enforcer into every mutating and listing route, including a new `ACTION_SESSION_CREATE_CROSS_TENANT` audit action for the rare legitimate platform-admin path.

## G-5 — Retention routes lacked scope enforcement and audit

### Code citations
- `app/api/v1/retention.py:10` — "See findings_phase1g.md G-5 for the four..."

### Resolution commits (on `step-29y-impl`)
- `b3052e0` — Step 29.y Cluster 1 (G-5): rewrite retention routes with scope enforcement

### Reconstructed summary

Retention policies are PIPEDA legal-compliance artifacts. Mutations to them, and enforcement runs, must always leave an audit row. Pre-Cluster-1 the routes mutated without consistent audit and without scope enforcement. Cluster 1 rewrites the routes, adds `ACTION_RETENTION_ENFORCE` and `ACTION_RETENTION_MANUAL_PURGE`, and pipes everything through `enforce_tenant_scope`.

## G-6 — Teardown-integrity endpoint not rate-limited

### Code citations
- `tests/api/test_step29y_cluster1_scope_hardening.py:5` — "G-6"

### Resolution commits (on `step-29y-impl`)
- `b3052e0` — Step 29.y Cluster 1 (G-6): rate-limit teardown-integrity endpoint

### Reconstructed summary

The teardown-integrity admin endpoint was unrate-limited. Cluster 1 attaches the standard rate limiter so a buggy or malicious caller cannot flood it.

## G-7 — Mojibake / em-dash unicode in route handlers

### Code citations
- `tests/api/test_step29y_cluster1_scope_hardening.py:118` — "mojibake of em-dash (â€”) is the specific shape findings_phase1g..."

### Resolution commits (on `step-29y-impl`)
- `51f25a4` — Step 29.y Cluster 1 (G-3 + G-7): rewrite consent.py with scope enforcement
- `ad93d73` — Step 29.y Cluster 1: AST-based scope-hardening tests for G-3..G-7

### Reconstructed summary

A mojibake-encoded em-dash had crept into a route handler docstring/error message and mis-rendered in API responses. Cluster 1 normalizes the encoding and pins the fix with an AST test that scans the route source bytes for the specific mojibake shape.
