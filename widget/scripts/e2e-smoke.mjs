#!/usr/bin/env node
// Step 30b commit (e): widget end-to-end smoke.
//
// Boots the mock backend, serves widget/ via a tiny static file
// server, drives Chromium via Playwright, and asserts that:
//
//   1) The widget bundle loads without import errors.
//   2) Mounting via window.init() inserts a Shadow DOM host.
//   3) Clicking the launcher opens the panel.
//   4) Typing a message + clicking Send sends a POST to the mock
//      backend and renders the streamed reply in the Shadow DOM.
//   5) The "done" frame flips streaming off (no typing dot).
//
// Why this is a script and NOT a CI job:
//   The widget-build-and-size CI job from commit (d) gives us the
//   important automated signal: the bundle compiles and fits the
//   budget. Running a real browser per push triples the runner
//   cost and per-push time. The e2e script is on-demand for local
//   verification before any CDN deploy, plus pre-merge if the
//   widget code changed.
//
// Usage:
//   cd widget && npm run test:e2e
//
// Self-skip: if Playwright is not installed (intentional in CI),
// the script logs a SKIP line and exits 0. The script is
// non-fatal by design -- it is a verification tool, not a gate.

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFileSync, statSync } from "node:fs";
import { join, dirname, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

const __dirname = dirname(fileURLToPath(import.meta.url));
const widgetDir = join(__dirname, "..");
const distFile = join(widgetDir, "dist", "luciel-chat-widget.js");

// Sanity: the bundle must already be built. The npm script chains
// build before test:e2e, so this is a defensive guard.
try {
  statSync(distFile);
} catch {
  console.error("[e2e] FAIL: dist/luciel-chat-widget.js missing. Run `npm run build` first.");
  process.exit(2);
}

let playwright;
try {
  playwright = await import("playwright");
} catch {
  console.log("[e2e] SKIP: playwright not installed. Run `npm install -D playwright` to enable.");
  process.exit(0);
}

const BACKEND_PORT = 8765;
const STATIC_PORT = 8766;

// ---- Static server for widget/ (so demo.html and dist/widget.js share an origin)

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".map": "application/json; charset=utf-8",
};

const staticServer = createServer((req, res) => {
  // Trim query string and prevent path traversal.
  const url = (req.url || "/").split("?")[0];
  const safe = url.replace(/\.\./g, "");
  const path = safe === "/" ? "/demo.html" : safe;
  const full = join(widgetDir, path);
  try {
    const body = readFileSync(full);
    res.writeHead(200, { "Content-Type": MIME[extname(full)] || "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404);
    res.end("not found");
  }
});

// ---- Boot mock backend as a child process

const mock = spawn("node", [join(widgetDir, "scripts", "mock-backend.mjs")], {
  env: { ...process.env, PORT: String(BACKEND_PORT) },
  stdio: ["pipe", "pipe", "inherit"],
});
mock.stdout.on("data", (b) => process.stdout.write("[mock] " + b.toString()));

async function waitForBackend() {
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${BACKEND_PORT}/health`);
      if (r.ok) return;
    } catch {
      /* not yet */
    }
    await sleep(100);
  }
  throw new Error("mock backend did not become ready");
}

// ---- The actual test

let exitCode = 0;
let browser;
try {
  staticServer.listen(STATIC_PORT, "127.0.0.1");
  await waitForBackend();
  console.log(`[e2e] static: http://127.0.0.1:${STATIC_PORT}/`);
  console.log(`[e2e] backend: http://127.0.0.1:${BACKEND_PORT}/`);

  browser = await playwright.chromium.launch();
  const context = await browser.newContext();
  const page = await context.newPage();
  page.on("pageerror", (err) => console.error("[browser pageerror]", err.message));
  page.on("console", (msg) => {
    if (msg.type() === "error" || msg.type() === "warning") {
      console.error("[browser", msg.type(), "]", msg.text());
    }
  });
  page.on("requestfailed", (req) => {
    console.error("[browser requestfailed]", req.url(), req.failure()?.errorText);
  });
  page.on("response", (resp) => {
    if (resp.url().includes("chat/widget")) {
      console.log("[browser response]", resp.status(), resp.url());
    }
  });

  await page.goto(`http://127.0.0.1:${STATIC_PORT}/demo.html`);

  // Fill in apiBase + a non-empty embed key. Mock backend ignores
  // the key but the widget bundle requires both to be non-empty
  // before init() will run.
  await page.fill("#apiBase", `http://127.0.0.1:${BACKEND_PORT}`);
  await page.fill("#embedKey", "luc_embed_e2e_smoke");
  await page.click("#mountBtn");

  // Wait for the demo's mount hook to flip true.
  await page.waitForFunction(() => window.__lucielDemo?.mounted === true, null, {
    timeout: 5000,
  });

  // Verify the Shadow DOM host attached to the page.
  const hostExists = await page.evaluate(() => {
    return Boolean(document.querySelector("[data-luciel-widget]"));
  });
  if (!hostExists) throw new Error("widget host element missing after mount");

  // Click the launcher (inside the Shadow root).
  await page.evaluate(() => {
    const host = document.querySelector("[data-luciel-widget]");
    const root = host.shadowRoot;
    root.querySelector(".luciel-launcher").click();
  });

  // The panel should appear. Type a message + send.
  await page.waitForFunction(() => {
    const host = document.querySelector("[data-luciel-widget]");
    return Boolean(host.shadowRoot.querySelector(".luciel-panel"));
  }, null, { timeout: 2000 });

  await page.evaluate(() => {
    const host = document.querySelector("[data-luciel-widget]");
    const root = host.shadowRoot;
    const input = root.querySelector(".luciel-composer input");
    // Native value setter so React/Preact's value tracker sees the
    // change. Plain `input.value = ...` bypasses the property
    // descriptor that hooks the change-detection.
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    ).set;
    setter.call(input, "hello widget");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  // Wait for Preact to re-render and the button to be enabled.
  await page.waitForFunction(() => {
    const host = document.querySelector("[data-luciel-widget]");
    const root = host.shadowRoot;
    const btn = root.querySelector(".luciel-composer button");
    return btn && !btn.disabled;
  }, null, { timeout: 2000 });
  await page.evaluate(() => {
    const host = document.querySelector("[data-luciel-widget]");
    host.shadowRoot.querySelector(".luciel-composer button").click();
  });

  // Wait until the streaming reply contains the expected text from
  // the mock backend. Allow a generous timeout because the mock
  // delays 25ms per token.
  const fullReply = await page.waitForFunction(
    () => {
      const host = document.querySelector("[data-luciel-widget]");
      const root = host?.shadowRoot;
      if (!root) return null;
      const msgs = Array.from(root.querySelectorAll(".luciel-msg.assistant"));
      const last = msgs[msgs.length - 1];
      if (!last) return null;
      const text = last.textContent || "";
      return text.includes('Mock reply: I heard "hello widget".') ? text : null;
    },
    null,
    { timeout: 10000 },
  );
  const replyText = await fullReply.jsonValue();
  console.log(`[e2e] streamed reply: ${JSON.stringify(replyText)}`);

  // After the done frame, the streaming class should be gone.
  const stillStreaming = await page.evaluate(() => {
    const host = document.querySelector("[data-luciel-widget]");
    const root = host.shadowRoot;
    const msgs = Array.from(root.querySelectorAll(".luciel-msg.assistant"));
    const last = msgs[msgs.length - 1];
    return Boolean(last && last.classList.contains("streaming"));
  });
  if (stillStreaming) {
    throw new Error("streaming class still present after done frame");
  }

  console.log("[e2e] PASS");
} catch (err) {
  console.error("[e2e] FAIL:", err.message);
  exitCode = 1;
} finally {
  if (browser) await browser.close();
  staticServer.close();
  if (!mock.killed) mock.kill("SIGTERM");
}

process.exit(exitCode);
