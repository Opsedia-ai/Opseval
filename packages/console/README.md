# Opseval console

React + TypeScript + Vite + Tailwind (ADR-0004), themed with the Opsedia "Coral Field" design system
(`docs/Design system.pdf`). Tokens live in `src/index.css`.

```bash
# terminal 1: the API (reads ./reports, serves the demo catalogue read-only by default)
uv run opseval serve --target ws://127.0.0.1:8765      # needs the `serve` extra (in the dev group)
#   prints:  Console:  http://localhost:5173/#token=…   <- open this link
# terminal 2: the console, proxying /api to :8000 (OPSEVAL_API=http://127.0.0.1:<port> for another)
cd packages/console && npm ci && npm run dev            # http://localhost:5173
```

Every API request needs the bearer token `opseval serve` printed (ADR-0031). The link carries it
in the URL fragment; `src/lib/api.ts` keeps it in `localStorage` and drops it from the address bar.

`src/lib/types.ts` mirrors `packages/opseval/src/opseval/server/schemas.py`. The browser never
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
