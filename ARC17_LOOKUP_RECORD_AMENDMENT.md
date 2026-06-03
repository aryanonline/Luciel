# Arc 17 amendment — record-source data infrastructure & live `lookup_record`

**Status:** founder-directed amendment. The canonical PDFs live outside the repo;
the founder will fold this note into `ARCHITECTURE_v2`. Recorded here so code and
docs do not drift.

## What is amended

§3.2, §3.3.2, and §6 are amended to **assign the record-source data
infrastructure to Arc 17.** Previously §3.2 named the `lookup_record` data source
as "an admin-uploaded CSV or a live data connector" but assigned it **no owning
arc** — a documented gap flagged for founder review at the Arc 12 closeout, which
the `lookup_record` interim body pinned as `owning_arc="UNASSIGNED"`.

## What shipped

The interim body of `lookup_record`
(`app/tools/implementations/lookup_record_tool.py`) — which returned
`{"success": false, "not_yet_available": true, "owning_arc": "UNASSIGNED"}` and
performed no lookup — is **replaced with the live implementation.** The
`UNASSIGNED` / `not_yet_available` markers are removed from the live output path.

The live source is read through a `RecordSource` interface
(`app/integrations/record_source/`):

- `LocalFileRecordSource` — local-path / `file://` / in-memory CSV via
  `csv.DictReader`. Used by tests and local dev. No AWS dependency.
- `S3RecordSource` — real boto3 `s3:GetObject`. **DEPLOY-GATED.**
- `resolve_record_source(store_ref, settings)` — dispatches by `store_ref`
  scheme (`s3://` → S3; `file://` / bare path → local).

The source location is the connection's **non-secret**
`config_json.store_ref` (Architecture §3.2 / §3.8.2). Any credential needed to
read it (if ever) rides behind `credential_ref` via the SecretStore — never
`config_json`. A CSV read needs no secret.

## Correctness boundary (§3.2, load-bearing)

`lookup_record` returns **LIVE, EXACT** records from its configured
`record_source`, read on every call. It is **NOT** the knowledge store and is
**never** blended with vector / graph retrieval. Results are framed as coming
from the admin's own record source.

## Domain-agnostic (Locked Decision #5)

No real-estate / vertical-specific wording or schema. The query surface is
generic `record_id` / `query` / `filters` only; query semantics reason only about
structural `id` / `record_id` identity columns.

## s3 live read is DEPLOY-GATED pending AWS

`record_source_live_enabled` (new, in `app/core/config.py`) is the master gate,
mirroring `connections_live_secrets_enabled`. When **False** (the boot-safe
default), an `s3://` store_ref returns an **honest** deploy-gated failure
(`success=false`, "record source not reachable in this environment") — never a
fake success, never a crash. **No boto3 client is constructed.** A local / `file://`
store_ref is always readable regardless of the flag. Production flips the flag True
in lockstep with the IAM `s3:GetObject` grant on the record-source bucket prefix.
