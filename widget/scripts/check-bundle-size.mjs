#!/usr/bin/env node
// Step 30b commit (d): widget bundle-size gate.
//
// CI runs `npm run ci` which is `npm run build && npm run size`.
// This script enforces the documented contract from CANONICAL_RECAP
// and the four step-30b design decisions: the gzipped widget bundle
// must be at most 40 KB. Above that, the cost of carrying Preact
// over vanilla DOM stops being justifiable, and we should revisit.
//
// Why a hard ceiling and not a soft warning:
//   The 40 KB number is the budget that lets us promise customers
//   "the widget will not noticeably affect your site speed". A
//   silent drift to 45 / 60 / 80 KB across many small changes is
//   exactly how third-party widgets become unwelcome. If a future
//   feature genuinely needs more, raise the ceiling deliberately
//   in a commit that explains the trade-off.

import { gzipSync } from "node:zlib";
import { readFileSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoDir = join(__dirname, "..");
const bundlePath = join(repoDir, "dist", "luciel-chat-widget.js");

const MAX_GZ_BYTES = 40 * 1024;

let raw;
try {
  raw = readFileSync(bundlePath);
} catch (err) {
  console.error(`[size] FAIL: could not read ${bundlePath}: ${err.message}`);
  console.error("[size] Did you run `npm run build` first? `npm run ci` does both.");
  process.exit(2);
}

const gz = gzipSync(raw, { level: 9 });
const rawKb = (raw.byteLength / 1024).toFixed(1);
const gzKb = (gz.byteLength / 1024).toFixed(1);
const ceilingKb = (MAX_GZ_BYTES / 1024).toFixed(0);

console.log(`[size] bundle:    ${bundlePath}`);
console.log(`[size] raw:       ${rawKb} KB`);
console.log(`[size] gzipped:   ${gzKb} KB`);
console.log(`[size] ceiling:   ${ceilingKb} KB gzipped`);

if (gz.byteLength > MAX_GZ_BYTES) {
  const overKb = ((gz.byteLength - MAX_GZ_BYTES) / 1024).toFixed(1);
  console.error(
    `[size] FAIL: widget bundle exceeds the ${ceilingKb} KB gzipped ceiling by ${overKb} KB.`,
  );
  console.error(
    "[size] Either trim the bundle or raise the ceiling deliberately in vite.config / this script.",
  );
  process.exit(1);
}

// Sanity: also reject empty / impossibly-small bundles which would
// indicate a broken build (e.g. tree-shaking ate the entry).
const stat = statSync(bundlePath);
if (stat.size < 1024) {
  console.error("[size] FAIL: bundle is suspiciously small (<1 KB). Build likely broken.");
  process.exit(3);
}

console.log("[size] OK");
