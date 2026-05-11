"""
Widget-surface SSE assertion script.

Step 30d, Deliverable C.

Run by ci/e2e/run_widget_e2e.sh against a live uvicorn. Exits 0 if the
SSE frame contract for the given mode matches expectations; exits 1
with a diagnostic on any deviation.

Mode contract
=============

happy
-----
Send a benign message. Expect:
  * HTTP status 200
  * frame 1: JSON object with key "session_id" (str)
  * frame 2..N-1: JSON objects with key "token" (str). At least one.
  * final frame: JSON object with key "done" (bool == True) and a
    "session_id" matching frame 1.

refusal
-------
Send a message containing the sentinel block term. Expect:
  * HTTP status 200 (NOT 4xx -- the refusal envelope is sanitized to
    prevent client-side fingerprinting; see Deliverable B locked
    judgment #2).
  * frame 1: JSON object with key "session_id".
  * One "token" frame whose value is the REFUSAL_MESSAGE constant
    from app.policy.moderation (we re-derive it via import rather
    than hardcode the string here so a future refactor that changes
    the wording at one site doesn't quietly desync the test).
  * Final "done" frame.

Why a separate script
=====================

The shell harness uses curl for the admin-side provisioning so the
log line matches what a real operator would type. SSE parsing in pure
bash, however, is fragile (event boundaries, JSON parsing, frame
ordering), so we drop into Python for the assertion step. httpx's
streaming client is the same library the app uses, which keeps the
test runtime aligned with the runtime under test.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterator

import httpx


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Assert widget SSE frame contract for Step 30d Deliverable C",
    )
    p.add_argument("--mode", choices=["happy", "refusal"], required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--embed-key", required=True)
    p.add_argument("--origin", required=True)
    p.add_argument(
        "--sentinel",
        default=None,
        help=(
            "Required when --mode=refusal. Must match exactly one entry of "
            "moderation_keyword_block_terms the running app was booted "
            "with. Mismatch -> assertion failure (which is the intended "
            "misconfig signal)."
        ),
    )
    return p.parse_args()


def _refusal_message() -> str:
    """Re-derive the canonical refusal text via import so a future
    rewording at the source automatically flows here, instead of
    silently desyncing.
    """
    from app.api.v1.chat_widget import REFUSAL_MESSAGE  # noqa: PLC0415
    return REFUSAL_MESSAGE


def _iter_sse_frames(resp: httpx.Response) -> Iterator[dict]:
    """Yield decoded JSON objects from an SSE stream.

    The widget endpoint emits frames of the shape:
        data: {"...": ...}\\n\\n

    We accumulate lines until a blank line, strip the leading 'data: '
    prefix, and json.loads the rest. Any other shape is reported.
    """
    buf: list[str] = []
    for raw_line in resp.iter_lines():
        # httpx delivers lines already-decoded as str.
        line = raw_line
        if line == "":
            if not buf:
                continue
            # Flush the accumulated event.
            event_body = "\n".join(buf)
            buf = []
            if not event_body.startswith("data: "):
                raise AssertionError(
                    f"SSE frame did not start with 'data: ': "
                    f"{event_body[:80]!r}"
                )
            payload = event_body[len("data: "):]
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"SSE frame body was not valid JSON: "
                    f"{payload[:200]!r} ({exc})"
                ) from exc
            yield obj
        else:
            buf.append(line)
    # Some servers omit the trailing blank line on the last frame; if
    # we have an accumulated buffer here, flush it too.
    if buf:
        event_body = "\n".join(buf)
        if event_body.startswith("data: "):
            try:
                yield json.loads(event_body[len("data: "):])
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"trailing SSE frame body was not valid JSON: "
                    f"{event_body[:200]!r} ({exc})"
                ) from exc


def _run(args: argparse.Namespace) -> int:
    if args.mode == "refusal" and not args.sentinel:
        print(
            "FATAL: --sentinel is required when --mode=refusal",
            file=sys.stderr,
        )
        return 1

    if args.mode == "happy":
        message = "Hello from the E2E harness. Please reply briefly."
    else:
        # Embed the sentinel inside otherwise-natural text so we are
        # also exercising the substring-match (not equality) semantics
        # of KeywordModerationProvider.
        message = (
            f"This message contains the {args.sentinel} sentinel "
            f"to trigger the moderation block."
        )

    url = f"{args.base_url}/api/v1/chat/widget"
    headers = {
        "Authorization": f"Bearer {args.embed_key}",
        "Origin": args.origin,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {"message": message}

    print(f"--> POST {url} (mode={args.mode})", file=sys.stderr)

    with httpx.Client(timeout=30.0) as client:
        with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code != 200:
                # Read the body for diagnostics.
                resp.read()
                print(
                    f"FAIL: expected HTTP 200, got {resp.status_code}; "
                    f"body={resp.text[:500]!r}",
                    file=sys.stderr,
                )
                return 1

            frames = list(_iter_sse_frames(resp))

    if not frames:
        print("FAIL: stream yielded zero frames", file=sys.stderr)
        return 1

    # Frame 1: session_id frame
    first = frames[0]
    if "session_id" not in first:
        print(
            f"FAIL: first frame missing 'session_id': {first!r}",
            file=sys.stderr,
        )
        return 1
    session_id = first["session_id"]

    # Final frame: done frame echoing session_id
    last = frames[-1]
    if not last.get("done"):
        print(
            f"FAIL: last frame missing 'done':true: {last!r}",
            file=sys.stderr,
        )
        return 1
    if last.get("session_id") != session_id:
        print(
            f"FAIL: last frame session_id != first frame session_id: "
            f"{last.get('session_id')!r} != {session_id!r}",
            file=sys.stderr,
        )
        return 1

    # Middle frames: tokens
    middle = frames[1:-1]
    token_frames = [f for f in middle if "token" in f]
    if not token_frames:
        print(
            f"FAIL: no token frames between first and last "
            f"(got {len(middle)} middle frames: {middle!r})",
            file=sys.stderr,
        )
        return 1

    if args.mode == "refusal":
        refusal = _refusal_message()
        joined = "".join(f.get("token", "") for f in token_frames)
        if refusal not in joined:
            print(
                f"FAIL: refusal mode -- REFUSAL_MESSAGE not found in token "
                f"stream. Expected substring: {refusal!r} "
                f"Got joined tokens: {joined!r}",
                file=sys.stderr,
            )
            return 1

    print(
        f"OK: mode={args.mode} session_id={session_id} "
        f"token_frames={len(token_frames)}"
    )
    return 0


if __name__ == "__main__":
    try:
        rc = _run(_parse_args())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc)
