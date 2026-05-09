// Step 30b commit (d): Preact widget root component.
//
// Renders a launcher bubble + slide-up chat panel inside a Shadow
// DOM root so customer-site CSS cannot leak into widget styles or
// vice versa. The mount() function is the bridge between the Preact
// tree and the Shadow DOM container; index.ts is the public entry
// that calls it.
//
// The widget is conversational only at v1 (Step 30c lockstep). No
// tool buttons, no file upload, no markdown rendering -- assistant
// messages render as plaintext with <pre>-like white-space handling
// to keep the XSS surface zero. Anyone reading this in 2026: do
// NOT add dangerouslySetInnerHTML or v-html-equivalent without a
// dedicated review pass against widget_config and message content.

import { h, render } from "preact";
import { useEffect, useRef, useState, useCallback } from "preact/hooks";
import inlineStyles from "./styles.css?inline";
import { streamChat } from "./api";
import type {
  InitOptions,
  UiMessage,
  WidgetConfig,
  WidgetFrame,
  WidgetHandle,
} from "./types";

interface ChatPanelProps {
  embedKey: string;
  apiBase: string;
  configOverride?: WidgetConfig;
}

function ChatPanel(props: ChatPanelProps) {
  const [open, setOpen] = useState(false);
  const [config, setConfig] = useState<WidgetConfig>(props.configOverride ?? {});
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const messagesRef = useRef<HTMLDivElement>(null);

  // Seed greeting on first open. We do not auto-open the panel --
  // customer sites strongly prefer a quiet launcher until clicked.
  const greeting = config.greeting_message;
  useEffect(() => {
    if (open && messages.length === 0 && greeting) {
      setMessages([{ role: "assistant", content: greeting }]);
    }
  }, [open, messages.length, greeting]);

  // Auto-scroll to the bottom whenever a token lands.
  useEffect(() => {
    const el = messagesRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || streaming) return;
    setDraft("");
    setError(null);
    setStreaming(true);

    // Optimistically render the user message and a placeholder
    // assistant message we will fill token-by-token.
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text },
      { role: "assistant", content: "", streaming: true },
    ]);

    const onFrame = (frame: WidgetFrame) => {
      if ("session_id" in frame && "widget_config" in frame) {
        sessionIdRef.current = frame.session_id;
        if (frame.widget_config && !props.configOverride) {
          setConfig((prev) => ({ ...prev, ...frame.widget_config }));
        }
        return;
      }
      if ("token" in frame) {
        setMessages((prev) => {
          const next = prev.slice();
          const last = next[next.length - 1];
          if (last && last.role === "assistant") {
            next[next.length - 1] = {
              ...last,
              content: last.content + frame.token,
            };
          }
          return next;
        });
        return;
      }
      if ("done" in frame && frame.done) {
        sessionIdRef.current = frame.session_id ?? sessionIdRef.current;
        setMessages((prev) => {
          const next = prev.slice();
          const last = next[next.length - 1];
          if (last && last.role === "assistant") {
            next[next.length - 1] = { ...last, streaming: false };
          }
          return next;
        });
        return;
      }
      if ("error" in frame) {
        setError(frame.error);
      }
    };

    await streamChat(
      {
        apiBase: props.apiBase,
        embedKey: props.embedKey,
        message: text,
        sessionId: sessionIdRef.current,
      },
      {
        onFrame,
        onError: (err) => setError(err.message),
        onClose: () => {
          setStreaming(false);
          // If the assistant slot is still flagged streaming (no
          // FrameDone arrived), unset it so the typing dot stops.
          setMessages((prev) => {
            const next = prev.slice();
            const last = next[next.length - 1];
            if (last && last.role === "assistant" && last.streaming) {
              next[next.length - 1] = { ...last, streaming: false };
            }
            return next;
          });
        },
      },
    );
  }, [draft, streaming, props.apiBase, props.embedKey, props.configOverride]);

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const accent = config.accent_color;
  const headerName = config.display_name ?? "Chat";

  return (
    <div style={accent ? { ["--luciel-accent" as any]: accent } : undefined}>
      {!open && (
        <button
          class="luciel-launcher"
          aria-label="Open chat"
          onClick={() => setOpen(true)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2z" />
          </svg>
        </button>
      )}
      {open && (
        <div class="luciel-panel" role="dialog" aria-label={headerName}>
          <div class="luciel-header">
            <span>{headerName}</span>
            <button aria-label="Close chat" onClick={() => setOpen(false)}>
              ×
            </button>
          </div>
          <div class="luciel-messages" ref={messagesRef}>
            {messages.map((m, i) => (
              <div
                key={i}
                class={`luciel-msg ${m.role}${m.streaming ? " streaming" : ""}`}
              >
                {m.content}
              </div>
            ))}
          </div>
          {error && <div class="luciel-error">{error}</div>}
          <div class="luciel-composer">
            <input
              type="text"
              value={draft}
              placeholder="Type a message…"
              disabled={streaming}
              onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
              onKeyDown={onKeyDown}
            />
            <button onClick={send} disabled={streaming || !draft.trim()}>
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function mount(opts: InitOptions): WidgetHandle {
  const target =
    typeof opts.mountTo === "string"
      ? document.querySelector(opts.mountTo)
      : opts.mountTo ?? document.body;
  if (!target) {
    throw new Error("Luciel widget: mountTo target not found");
  }

  // Host element + shadow root. The shadow root isolates styles
  // from the customer page in both directions.
  const host = document.createElement("div");
  host.setAttribute("data-luciel-widget", "");
  // Pin the host to the viewport corner so customer CSS that sets
  // overflow:hidden on body doesn't clip the launcher.
  host.style.position = "fixed";
  host.style.top = "0";
  host.style.left = "0";
  host.style.width = "0";
  host.style.height = "0";
  host.style.zIndex = "2147483647";
  target.appendChild(host);

  const shadow = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = inlineStyles;
  shadow.appendChild(style);
  const mountPoint = document.createElement("div");
  shadow.appendChild(mountPoint);

  render(
    <ChatPanel
      embedKey={opts.embedKey}
      apiBase={opts.apiBase}
      configOverride={opts.configOverride}
    />,
    mountPoint,
  );

  return {
    destroy: () => {
      render(null, mountPoint);
      host.remove();
    },
  };
}
