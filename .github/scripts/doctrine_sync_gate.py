#!/usr/bin/env python3
"""Doctrine doc-sync CI gate (Architecture §5.9.3).

Enforces: a PR that modifies any **doctrine-anchored path** MUST also
record the change in the in-repo doctrine changelog, else the gate FAILS.

Why a changelog and not "the architecture doc"
----------------------------------------------
§5.9.3 in the ratified doctrine requires the architecture doc to be
updated whenever a doctrine-anchored path changes. That doc is canon and
lives in the Space, NOT in this repo, so CI cannot diff it. The in-repo
proxy is ``DOCTRINE_CHANGELOG.md``: when a PR touches an anchored path,
the author adds a changelog entry, and the reviewer reconciles that entry
against the Space doc during review. The gate therefore checks for a
changelog change as a stand-in for "doc updated". This trade-off is
documented in DOCTRINE_ANCHORS.toml [meta].

Matching logic
--------------
The anchored paths come from ``DOCTRINE_ANCHORS.toml`` (the ``paths`` of
every ``[[anchor]]`` whose status marks it as live code, i.e. not
NO-MODULE-YET, which has no paths). A path entry ending in ``/`` is a
directory prefix; any changed file under it matches. A path entry that is
a file matches that exact file. The doc/changelog files themselves and
the anchors map are never treated as anchored code (changing them is the
remediation, not the trigger).

Exit codes: 0 = pass, 1 = fail (anchored path touched without a changelog
entry), 2 = usage / config error.

The core decision is the pure function :func:`evaluate` so it can be unit
tested with a synthetic changed-file list and a synthetic anchor map,
with no git or filesystem dependency.
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Files whose modification SATISFIES the gate (the "doc was updated"
# proxy) and which are themselves never treated as anchored code.
DOC_PROXY_FILES = frozenset(
    {
        "DOCTRINE_CHANGELOG.md",
    }
)

# Files that describe the doctrine mapping itself; editing them is part of
# remediation, not a trigger.
GATE_INFRA_FILES = frozenset(
    {
        "DOCTRINE_ANCHORS.toml",
    }
)

# Anchor statuses that contribute live code paths to the gate. NO-MODULE-YET
# has no paths; the exception statuses still point at real code that, if
# touched, should require a doctrine note, so they are included.
_STATUSES_WITH_LIVE_PATHS = frozenset(
    {
        "MATCHES-DOC",
        "CONFIG-BOUND-EXCEPTION",
        "ISOLATION-SUITE",
    }
)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    triggered: bool
    matched_anchors: list[str] = field(default_factory=list)
    matched_files: list[str] = field(default_factory=list)
    has_doc_proxy: bool = False
    message: str = ""


def _path_matches(anchor_path: str, changed: str) -> bool:
    """True when ``changed`` is governed by the anchor ``anchor_path``.

    A trailing ``/`` makes the anchor a directory prefix; otherwise it is
    an exact file match. Paths are compared as POSIX-style strings with no
    leading ``./``.
    """
    anchor_path = anchor_path.lstrip("./")
    changed = changed.lstrip("./")
    if anchor_path.endswith("/"):
        return changed.startswith(anchor_path)
    return changed == anchor_path


def anchored_paths(anchors_doc: dict) -> dict[str, list[str]]:
    """Map anchor id -> list of governed paths, for live-code anchors only."""
    out: dict[str, list[str]] = {}
    for anchor in anchors_doc.get("anchor", []):
        status = anchor.get("status", "")
        if status not in _STATUSES_WITH_LIVE_PATHS:
            continue
        paths = [p for p in anchor.get("paths", []) if p]
        if paths:
            out[anchor.get("id", "<unknown>")] = paths
    return out


def evaluate(changed_files: list[str], anchors_doc: dict) -> GateResult:
    """Pure gate decision — no git, no filesystem.

    Given the PR's changed files and the parsed DOCTRINE_ANCHORS.toml,
    decide whether the gate passes. The gate is *triggered* when any
    changed file is under a live anchored path (excluding the doc-proxy
    and gate-infra files). When triggered, the gate passes only if the PR
    also changed at least one doc-proxy file (DOCTRINE_CHANGELOG.md).
    """
    id_to_paths = anchored_paths(anchors_doc)

    matched_anchors: list[str] = []
    matched_files: set[str] = set()
    for changed in changed_files:
        norm = changed.lstrip("./")
        if norm in DOC_PROXY_FILES or norm in GATE_INFRA_FILES:
            continue
        for anchor_id, paths in id_to_paths.items():
            if any(_path_matches(p, norm) for p in paths):
                matched_files.add(norm)
                if anchor_id not in matched_anchors:
                    matched_anchors.append(anchor_id)

    has_doc_proxy = any(
        c.lstrip("./") in DOC_PROXY_FILES for c in changed_files
    )

    triggered = bool(matched_files)
    if not triggered:
        return GateResult(
            ok=True,
            triggered=False,
            has_doc_proxy=has_doc_proxy,
            message="No doctrine-anchored path changed; gate not triggered.",
        )

    if has_doc_proxy:
        return GateResult(
            ok=True,
            triggered=True,
            matched_anchors=sorted(matched_anchors),
            matched_files=sorted(matched_files),
            has_doc_proxy=True,
            message=(
                "Doctrine-anchored path(s) changed AND a "
                "DOCTRINE_CHANGELOG.md entry is present. Gate passes."
            ),
        )

    anchors = ", ".join(sorted(matched_anchors))
    files = ", ".join(sorted(matched_files))
    return GateResult(
        ok=False,
        triggered=True,
        matched_anchors=sorted(matched_anchors),
        matched_files=sorted(matched_files),
        has_doc_proxy=False,
        message=(
            "Doctrine-anchored path(s) changed but DOCTRINE_CHANGELOG.md "
            "was NOT updated.\n"
            f"  anchors touched: {anchors}\n"
            f"  files: {files}\n"
            "Per Architecture §5.9.3, a change to a doctrine-anchored path "
            "must be reflected in the architecture doc. Since the doc is "
            "canon in the Space, add a DOCTRINE_CHANGELOG.md entry "
            "describing the change for the reviewer to reconcile."
        ),
    )


def _load_anchors(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--anchors",
        default="DOCTRINE_ANCHORS.toml",
        help="Path to DOCTRINE_ANCHORS.toml (default: repo-root file).",
    )
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        dest="changed_files",
        help="A changed file path (repeatable). If omitted, read newline-"
        "separated paths from stdin.",
    )
    args = parser.parse_args(argv)

    anchors_path = Path(args.anchors)
    if not anchors_path.is_file():
        print(f"doctrine-gate: anchors map not found: {anchors_path}", file=sys.stderr)
        return 2

    changed = list(args.changed_files)
    if not changed:
        changed = [line.strip() for line in sys.stdin if line.strip()]

    try:
        anchors_doc = _load_anchors(anchors_path)
    except tomllib.TOMLDecodeError as exc:
        print(f"doctrine-gate: failed to parse {anchors_path}: {exc}", file=sys.stderr)
        return 2

    result = evaluate(changed, anchors_doc)
    stream = sys.stdout if result.ok else sys.stderr
    print(f"doctrine-gate: {result.message}", file=stream)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
