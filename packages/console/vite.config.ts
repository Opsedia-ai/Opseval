import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// `opseval serve` listens on :8000 by default; the console proxies /api so the browser sees
// one origin (the API has no CORS). OPSEVAL_API points the proxy at another port.
const env = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env;
const api = env?.OPSEVAL_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Never inline assets as data: URIs: the console's CSP is font-src 'self' (no data:), so an
  // inlined small font subset would be refused by the browser (ADR-0034).
  build: { assetsInlineLimit: 0 },
  test: { environment: "jsdom" },
  server: { port: 5173, proxy: { "/api": api } },
});
