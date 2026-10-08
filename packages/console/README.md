# Opseval console

React + TypeScript + Vite + Tailwind (ADR-0004), themed with the Opsedia "Coral Field" design system
(`docs/Design system.pdf`). Tokens live in `src/index.css`. The typefaces (Bricolage Grotesque,
Alexandria, Figtree, IBM Plex Mono and IBM Plex Sans Arabic, all OFL-1.1) are bundled from
Fontsource packages, so the console makes no request to a font CDN and works air-gapped.

```bash
# terminal 1: the API (reads ./reports, serves the demo catalogue read-only by default)
uv run opseval serve --target ws://127.0.0.1:8765      # needs the `serve` extra (in the dev group)
#   prints:  Console:  http://localhost:5173/#token=…   <- open this link
# terminal 2: the console, proxying /api to :8000 (OPSEVAL_API=http://127.0.0.1:<port> for another)
cd packages/console && npm ci && npm run dev            # http://localhost:5173
```

To *use* the console, `npm run build` once: `opseval serve` then serves `dist/` itself on
the API's port and prints that link (ADR-0034). The dev server above is for working on it.

Every API request needs the bearer token `opseval serve` printed (ADR-0031).

`npm test` (Vitest, in CI) pins the console's share of the one rule: only a safe run is green, a
harness error never looks like an agent failure, an agent error is never drawn as a normal turn,
an unmeasured count is never 0, progress is never a verdict, and the token is taken from the
link's fragment, dropped from the address bar and sent on every call. The link carries it
in the URL fragment; `src/lib/api.ts` keeps it in `localStorage` and drops it from the address bar.

`src/lib/api.gen.ts` is **generated** from `packages/opseval/src/opseval/server/schemas.py` by `scripts/console_types.py` (`make check` fails if it drifts); `src/lib/types.ts` only gives those types the console's names. Change a Python model, then run `uv run python scripts/console_types.py`. The browser never
recomputes aggregates, and `null` from the API is rendered as "not measured", never as 0.

| Page | Endpoint(s) |
|---|---|
| Runs, Run detail | `GET /runs`, `GET /runs/{id}` (polls while a run is in flight) |
| New run | `POST /runs` (launches in the background; honours ADR-0008) |
| Compare, Trend, Usage | `GET /compare`, `/trend`, `/usage` |
| Simulator | `GET /scenarios/{id}/persona`, `POST /simulator/rehearse` |
| Test cases, Bulk import | `GET /scenarios`, `POST /scenarios/import` (all-or-nothing) |
| Suites | `GET /suites`, `PUT /suites` |
| Checks, Settings | `GET /checks`, `GET /config` (secrets redacted server-side) |
