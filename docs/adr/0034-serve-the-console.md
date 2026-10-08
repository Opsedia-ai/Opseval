# ADR-0034: `opseval serve` serves the built console

- **Status:** Proposed
- **Date:** 2026-10-08
- **Deciders:** Opseval maintainers

## Context

Using the console used to take two processes: `opseval serve` for the API and `npm run dev`
for the console, joined by the Vite proxy. Someone who only wants to look at their runs had
to install Node, and the printed link pointed at a port the API did not own. ADR-0004 always
intended the API to serve the built console itself.

## Decision

When a built console is available, `opseval serve` serves it from `/`, on the same origin as
`/api/v1`:

- **Where it comes from.** It uses `OPSEVAL_CONSOLE_DIR`; a set folder without `index.html`
  is a configuration error (exit 3), not something to ignore quietly. Otherwise it uses a
  source checkout's `packages/console/dist`, found only when the package sits under
  `<repo>/packages/`. If neither exists, only the API answers, and `serve` says how to build
  the console.
- **Routing.** There is no catch-all route, so the API routes exactly as before: slash
  redirects, 405s and 404s are unchanged. The console is consulted only for a GET or HEAD
  outside `/api` that the router answered with 404:
  - a file inside the folder is served by Starlette's `StaticFiles`, which handles
    traversal, NUL bytes, HEAD and ranges;
  - anything else gets `index.html`, because the console routes in the browser;
  - `/api` itself and anything under it is never the page.
- **The page needs no token.** It holds no data; every API call it makes still carries one.
  The DNS-rebinding `Host` check of ADR-0031 applies to the page as well.
- **No public API docs.** `/docs` and `/redoc` are off. The OpenAPI schema is at
  `/api/v1/openapi.json`, behind the token like the rest of the API.
- **Headers on every non-API response**, not only `index.html`. Any page on this origin could
  read the token the console keeps, so none may be framed or run foreign code:
  - a Content Security Policy allowing only the origin's own scripts and connections, plus
    its fonts;
  - `frame-ancestors 'none'` and `X-Frame-Options: DENY`;
  - `nosniff` and `Referrer-Policy: no-referrer`.

  `index.html` is `no-store`. The content-hashed `assets/` are `immutable`.
- **The printed link** is `http://127.0.0.1:<port>/#token=…` when the console is served here,
  and the Vite dev URL otherwise.

The Vite dev server and its proxy remain the way to *work on* the console.

## Consequences

- From a source checkout, `npm run build` once and then `opseval serve` gives one link.
- The wheel does not include the console yet. Releasing it needs the build to run before
  packaging and the files to be included as package data (a hatch build hook). That is a
  release-pipeline change and waits for the first real release (Gate 0).
- *Update:* the typefaces ship with the console (Fontsource packages, all OFL-1.1), so a page
  view is reported to no font CDN, an air-gapped machine renders the same, and the CSP allows
  nothing from another origin (`font-src 'self'`). Vite never inlines assets
  (`assetsInlineLimit: 0`), since an inlined `data:` font would be refused by that policy. Variable
  fonts are used where Fontsource has them (woff2 only, every axis, so Bricolage keeps its optical
  size); the OFL texts ship in `licenses/` and the attributions are in `NOTICE`.
