// Step 30b commit (d): shared types for the chat widget.
//
// WidgetConfig mirrors the JSONB column on api_keys (commit b).
// All three fields are optional on the wire because a poorly-issued
// key may have an empty config; the widget falls back to neutral
// defaults rather than refusing to render.

export interface WidgetConfig {
  accent_color?: string;       // 7-char hex, e.g. "#0f62fe"
  greeting_message?: string;   // shown as the first bot message
  display_name?: string;       // chat panel header
}

// SSE frame shapes emitted by POST /api/v1/chat/widget.
// First frame: session bootstrap + branding echo.
export interface FrameSession {
  session_id: string;
  widget_config?: WidgetConfig;
}

// Subsequent frames: incremental tokens.
export interface FrameToken {
  token: string;
}

// Final frame: stream completion sentinel.
export interface FrameDone {
  done: true;
  session_id: string;
}

// Error frame: sanitized server-side message; never leaks internal state.
export interface FrameError {
  error: string;
}

export type WidgetFrame = FrameSession | FrameToken | FrameDone | FrameError;

// Public init API surface.
export interface InitOptions {
  embedKey: string;             // Bearer token; never an admin key
  apiBase: string;              // e.g. "https://api.luciel.example.com"
  mountTo?: string | HTMLElement;  // CSS selector or element; default: document.body
  configOverride?: WidgetConfig;   // dev/sandbox use only
}

export interface WidgetHandle {
  destroy: () => void;
}

// Internal message-list entry consumed by the Preact view.
export interface UiMessage {
  role: "user" | "assistant";
  content: string;
  // True while the assistant message is still streaming; flipped to
  // false on FrameDone so the UI can stop showing the typing dot.
  streaming?: boolean;
}
