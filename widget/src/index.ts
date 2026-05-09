// Step 30b commit (d): public entry for the embeddable chat widget.
//
// Two ways to use this bundle on a customer site:
//
// 1) ES module import (preferred in modern build pipelines):
//
//      import { init } from "https://cdn.luciel.example.com/widget.js";
//      init({
//        embedKey: "luc_embed_...",
//        apiBase:  "https://api.luciel.example.com",
//      });
//
// 2) Drop-in script tag with auto-mount via data-* attributes:
//
//      <script
//        type="module"
//        src="https://cdn.luciel.example.com/widget.js"
//        data-luciel-embed-key="luc_embed_..."
//        data-luciel-api-base="https://api.luciel.example.com">
//      </script>
//
//    The script auto-mounts on DOMContentLoaded if both data
//    attributes are present. This is the path most customers will
//    use because it requires no JS authoring on their side.
//
// The auto-mount path attaches a global Luciel object to window
// with `init` and `destroy` so embedders who need imperative
// control after page load can still reach the widget.

import { mount } from "./widget";
import type { InitOptions, WidgetHandle } from "./types";

export function init(opts: InitOptions): WidgetHandle {
  if (!opts.embedKey || !opts.apiBase) {
    throw new Error("Luciel widget: embedKey and apiBase are required");
  }
  return mount(opts);
}

// Auto-mount via data-* attributes on the script tag itself.
// document.currentScript is the standard way to discover the
// originating <script> element from inside a module that was
// loaded synchronously; for type=module scripts we fall back to
// a [src] selector against this bundle's filename. The fileName
// match is loose by design (the CDN may rename via a hashed path).
function findOwnScript(): HTMLScriptElement | null {
  const current = document.currentScript as HTMLScriptElement | null;
  if (current) return current;
  const candidates = document.querySelectorAll<HTMLScriptElement>(
    "script[data-luciel-embed-key][data-luciel-api-base]",
  );
  return candidates[0] ?? null;
}

function tryAutoMount() {
  const script = findOwnScript();
  if (!script) return;
  const embedKey = script.getAttribute("data-luciel-embed-key");
  const apiBase = script.getAttribute("data-luciel-api-base");
  if (!embedKey || !apiBase) return;
  init({ embedKey, apiBase });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", tryAutoMount, { once: true });
} else {
  // Document is already parsed; mount immediately.
  tryAutoMount();
}

// Surface a small global so integrators can call init/destroy
// imperatively after page load if they prefer that path.
declare global {
  interface Window {
    Luciel?: {
      init: typeof init;
    };
  }
}
window.Luciel = window.Luciel ?? { init };
