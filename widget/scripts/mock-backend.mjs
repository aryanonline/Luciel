#!/usr/bin/env node
// Step 30b commit (e): mock backend for the widget e2e + operator smoke.
//
// Mirrors the SSE contract emitted by app/api/v1/chat_widget.py:
//
//   frame 1: {"session_id": "<uuid>", "widget_config": {...}}
//   frames 2..N-1: {"token": "<chunk>"}
//   final:   {"done": true, "session_id": "<uuid>"}
//
// Intentionally does NOT enforce the embed-key / origin gate.
// Operators use this to prove the widget renders and streams
// correctly without spinning up Postgres + Redis + uvicorn; the
// real gate is exercised against the real backend in
// app/api/widget_deps.py and its 10-case offline test suite from
// commit (c).
//
// Usage:
//   node scripts/mock-backend.mjs           # default port 8765
//   PORT=9000 node scripts/mock-backend.mjs # custom port
//
// The script exits when stdin closes or on SIGTERM, which is what
// the e2e smoke driver uses to stop it cleanly.

import { createServer } from "node:http";
import { randomUUID } from "node:crypto";

const PORT = Number(process.env.PORT) || 8765;
const HOST = process.env.HOST || "127.0.0.1";

// Same headers the real backend's cors_response_headers builds. We
// echo Origin rather than '*' so the widget bundle (which sets
// credentials: "omit") still works under a wildcard-incompatible
// browser policy if one ever ships.
function corsHeaders(req) {
  const origin = req.headers.origin || "*";
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "600",
    Vary: "Origin",
  };
}

function sseFrame(obj) {
  return `data: ${JSON.stringify(obj)}\n\n`;
}

async function readJson(req) {
  return await new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf-8") || "{}"));
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

const server = createServer(async (req, res) => {
  // Health probe used by the e2e driver to wait for readiness.
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
    return;
  }

  if (req.url !== "/api/v1/chat/widget") {
    res.writeHead(404, { "Content-Type": "application/json", ...corsHeaders(req) });
    res.end(JSON.stringify({ detail: "not found" }));
    return;
  }

  if (req.method === "OPTIONS") {
    res.writeHead(204, corsHeaders(req));
    res.end();
    return;
  }

  if (req.method !== "POST") {
    res.writeHead(405, { "Content-Type": "application/json", ...corsHeaders(req) });
    res.end(JSON.stringify({ detail: "method not allowed" }));
    return;
  }

  let payload;
  try {
    payload = await readJson(req);
  } catch (err) {
    res.writeHead(400, { "Content-Type": "application/json", ...corsHeaders(req) });
    res.end(JSON.stringify({ detail: "bad json" }));
    return;
  }

  const sessionId = payload.session_id || randomUUID();
  const widgetConfig = {
    accent_color: "#0f62fe",
    greeting_message: "Hi! I'm the mock backend.",
    display_name: "Luciel (mock)",
  };

  // Stream a deterministic reply token-by-token so the e2e test can
  // assert exactly what gets rendered into the Shadow DOM.
  const reply =
    typeof payload.message === "string" && payload.message.length > 0
      ? `Mock reply: I heard "${payload.message}".`
      : "Mock reply: empty input.";
  const tokens = reply.match(/\S+\s*|\s+/g) || [reply];

  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
    ...corsHeaders(req),
  });

  res.write(sseFrame({ session_id: sessionId, widget_config: widgetConfig }));

  // Tiny delay between tokens so the streaming UI animation is
  // observable and so the e2e can verify the streaming class
  // toggles correctly. 25ms per token keeps the full reply under
  // 0.5s for typical inputs.
  for (const tok of tokens) {
    await new Promise((r) => setTimeout(r, 25));
    res.write(sseFrame({ token: tok }));
  }
  res.write(sseFrame({ done: true, session_id: sessionId }));
  res.end();
});

server.listen(PORT, HOST, () => {
  console.log(`[mock-backend] listening on http://${HOST}:${PORT}`);
});

function shutdown() {
  console.log("[mock-backend] shutting down");
  server.close(() => process.exit(0));
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
// Exit when the parent (e2e driver) closes stdin.
process.stdin.on("end", shutdown);
process.stdin.on("close", shutdown);
process.stdin.resume();
