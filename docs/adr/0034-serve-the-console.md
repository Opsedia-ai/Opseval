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

- **Where it comes from.** It uses `OPSEVAL_CONSOLE_DIR` when that is set. Otherwise it looks
  for `packages/console/dist` next to the installed package, which is a source checkout after
  `npm run build`. If neither has an `index.html`, only the API answers, and `serve` says how
  to build the console.
- **Routing.**
  - A path that names a file inside the console folder gets that file.
  - Any other non-`/api` path gets `index.html`, because the console routes in the browser.
  - `/api/*` routes always win, and an unknown API route is a 404, never the page.
  - A file outside the console folder is never served, however the path is encoded.
- **The page needs no token.** It holds no data; every API call it makes still carries one.
  The DNS-rebinding `Host` check of ADR-0031 applies to the page as well.
- **Headers on the page.**
  - A Content Security Policy allows only the page's own scripts and connections, plus its
    fonts.
  - `frame-ancestors 'none'` and `X-Frame-Options: DENY` stop clickjacking. The console can
    start runs and edit suites, so it must never be framed by another site.
  - `nosniff`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` on
    `index.html`. Assets are content-hashed and may be cached.
- **The printed link** is `http://127.0.0.1:<port>/#token=…` when the console is served here,
  and the Vite dev URL otherwise.

The Vite dev server and its proxy remain the way to *work on* the console.

## Consequences

- From a source checkout, `npm run build` once and then `opseval serve` gives one link.
- The wheel does not include the console yet. Releasing it needs the build to run before
  packaging and the files to be included as package data (a hatch build hook). That is a
  release-pipeline change and waits for the first real release (Gate 0).
- The console fetches its typefaces from Google Fonts, so a page view tells Google the
  viewer's address, and an air-gapped machine falls back to system fonts. The privacy NFR
  asks for air-gapped operation. Self-hosting the fonts (all are under the OFL) is a
  follow-up, and the CSP will then drop the two font hosts.
