# @luciel/chat-widget

Embeddable chat widget for Luciel. Preact + TypeScript, built to a
single ESM bundle that fits inside a 40 KB gzipped budget.

## Build

```bash
cd widget
npm ci
npm run build      # emits dist/luciel-chat-widget.js (+ sourcemap)
npm run size       # gzipped-budget gate, fails if > 40 KB
npm run ci         # build + size, what GitHub Actions runs
```

## Embed

Drop-in script tag (recommended):

```html
<script
  type="module"
  src="https://cdn.luciel.example.com/luciel-chat-widget.js"
  data-luciel-embed-key="luc_embed_..."
  data-luciel-api-base="https://api.luciel.example.com">
</script>
```

Imperative:

```js
import { init } from "https://cdn.luciel.example.com/luciel-chat-widget.js";

const handle = init({
  embedKey: "luc_embed_...",
  apiBase:  "https://api.luciel.example.com",
});

// Later:
handle.destroy();
```

## Design constraints

The widget is conversational only at v1. There are NO tool calls,
NO file uploads, NO markdown rendering, NO logo upload, NO font
choice, and NO free-form CSS. These are not oversights -- each one
is an XSS or trust surface we are deliberately keeping closed
until we have the policy machinery (Step 30c) to govern them.

The three branding knobs the customer controls are stored in
`api_keys.widget_config` JSONB and echoed to the bundle on the
first SSE frame:

  - `accent_color`     7-char hex
  - `greeting_message` plaintext
  - `display_name`     plaintext

## Security

- Bundle runs inside a Shadow DOM root: customer CSS cannot bleed
  in, widget CSS cannot bleed out.
- Embed key rides in `Authorization: Bearer ...`. Browsers cannot
  send that on a CORS preflight, so the backend OPTIONS handler is
  permissive; the actual POST is fully gated against the key's
  `allowed_origins` allowlist (see `app/api/widget_deps.py`).
- All assistant messages render as plaintext. Do NOT add
  `dangerouslySetInnerHTML` without a dedicated review pass.

## See also

- Backend endpoint: `app/api/v1/chat_widget.py`
- Embed-key gate: `app/api/widget_deps.py`
- Schema migration: `alembic/versions/a7c1f4e92b85_step30b_api_keys_widget_columns.py`
- Demo sandbox: `widget/demo.html` (commit (e))
