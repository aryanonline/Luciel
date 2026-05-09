// Step 30b commit (d): SSE client for POST /api/v1/chat/widget.
//
// EventSource is NOT used because it cannot send POST or custom
// headers; the embed key must ride in Authorization. We consume the
// SSE stream via fetch() + a ReadableStream reader, parsing the
// "data: {...}\n\n" frame format ourselves. This is a small, stable
// surface -- we own both ends of the wire (see app/api/v1/chat_widget.py).

import type { WidgetFrame } from "./types";

export interface StreamCallbacks {
  onFrame: (frame: WidgetFrame) => void;
  onError: (error: Error) => void;
  onClose: () => void;
}

export interface StreamArgs {
  apiBase: string;
  embedKey: string;
  message: string;
  sessionId: string | null;
  signal?: AbortSignal;
}

// Minimal SSE frame parser. The server-side contract is one JSON
// object per "data:" line, separated by a blank line. We do not
// support multi-line data fields or named events because the server
// never emits them. A defensive guard ignores comment lines (lines
// beginning with ":").
function parseFrame(rawFrame: string): WidgetFrame | null {
  const lines = rawFrame.split("\n");
  for (const line of lines) {
    if (line.startsWith(":")) continue; // SSE comment, skip
    if (!line.startsWith("data:")) continue;
    const payload = line.slice(5).trim();
    if (!payload) continue;
    try {
      return JSON.parse(payload) as WidgetFrame;
    } catch {
      // Malformed frame: ignore rather than tear the stream down.
      // The error frame contract still applies if the server itself
      // hits trouble.
      return null;
    }
  }
  return null;
}

export async function streamChat(
  args: StreamArgs,
  cb: StreamCallbacks,
): Promise<void> {
  const url = `${args.apiBase.replace(/\/+$/, "")}/api/v1/chat/widget`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      mode: "cors",
      credentials: "omit", // embed keys do not use cookies
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${args.embedKey}`,
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        session_id: args.sessionId,
        message: args.message,
      }),
      signal: args.signal,
    });
  } catch (err) {
    cb.onError(err instanceof Error ? err : new Error(String(err)));
    cb.onClose();
    return;
  }

  if (!response.ok || !response.body) {
    // 4xx / 5xx: read the JSON detail if present so the embedder
    // can see the stable error code in their console (e.g.
    // origin_not_allowed). Network/abort cases land here too.
    let detail = "";
    try {
      const text = await response.text();
      detail = text;
    } catch {
      /* ignore */
    }
    cb.onError(
      new Error(`Widget request failed: HTTP ${response.status} ${detail}`),
    );
    cb.onClose();
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by blank lines (\n\n).
      let sep = buffer.indexOf("\n\n");
      while (sep !== -1) {
        const rawFrame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const frame = parseFrame(rawFrame);
        if (frame) cb.onFrame(frame);
        sep = buffer.indexOf("\n\n");
      }
    }
    // Drain any trailing partial frame (server should always close
    // on a frame boundary, but be defensive).
    const tail = buffer.trim();
    if (tail) {
      const frame = parseFrame(tail);
      if (frame) cb.onFrame(frame);
    }
  } catch (err) {
    cb.onError(err instanceof Error ? err : new Error(String(err)));
  } finally {
    cb.onClose();
  }
}
