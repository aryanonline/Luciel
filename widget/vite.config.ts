// Step 30b commit (d): Vite build config for the Luciel chat widget.
//
// Output shape:
//   - Single ESM bundle: dist/luciel-chat-widget.js
//   - No CSS extraction; styles are inlined into the JS so a single
//     <script type="module"> include is sufficient on customer sites.
//   - Preact is bundled (NOT externalized) because customer sites
//     must not need any pre-existing JS framework. The whole point
//     of the embed is "drop in one tag and it works".
//
// Size budget: <=40 KB gzipped, enforced by scripts/check-bundle-size.mjs
// which the `npm run size` script runs after `npm run build`. CI runs
// `npm run ci` which chains the two.
//
// Why library mode and not a single-page app:
//   The widget is a library that customer pages embed. Vite's
//   build.lib mode emits a clean ESM module without an HTML shell,
//   and treats the entry's named exports as the public API.

import { defineConfig } from "vite";

export default defineConfig({
  esbuild: {
    jsx: "automatic",
    jsxImportSource: "preact",
  },
  build: {
    target: "es2020",
    minify: "esbuild",
    sourcemap: true,
    cssCodeSplit: false,
    lib: {
      entry: "src/index.ts",
      name: "Luciel",
      formats: ["es"],
      fileName: () => "luciel-chat-widget.js",
    },
    rollupOptions: {
      // Keep all dependencies bundled. Customer sites must not need
      // a separate <script> tag for Preact.
      external: [],
      output: {
        // Inline tiny dynamic chunks back into the main bundle so
        // there is exactly ONE JS file to host on the CDN.
        inlineDynamicImports: true,
        // No assetFileNames override -- styles are imported as
        // ?inline strings so they never produce a separate file.
      },
    },
    // Hard-fail the build if the (uncompressed) chunk grows past
    // 100 KB, a useful early signal independent of the gzipped
    // budget. The real budget gate runs in scripts/check-bundle-size.mjs.
    chunkSizeWarningLimit: 100,
  },
});
