# Opseval console

React + TypeScript + Vite + Tailwind (ADR-0004), themed with the Opsedia "Coral Field" design system
(`docs/Design system.pdf`). Tokens live in `src/index.css`.

```bash
# terminal 1: the API (reads ./reports; the bundled demo catalogue is read-only)
uv run opseval serve --catalogue ./my-catalogue --target ws://127.0.0.1:8765
# terminal 2: the console, proxying /api to :8000
cd packages/console && npm install && npm run dev       # http://127.0.0.1:5173
```

`src/lib/types.ts` mirrors `packages/opseval/src/opseval/server/schemas.py`. The browser never
recomputes aggregates, and `null` from the API is rendered as "not measured", never as 0.

| Page | Endpoint(s) |
|---|---|
| Runs, Run detail | `GET /runs`, `GET /runs/{id}` (polls while a run is in flight) |
| New run | `POST /runs`, `GET /readiness` (launches in the background; the ADR-0008 commit key is the server's, never this form's) |
| Compare, Trend, Usage | `GET /compare`, `/trend`, `/usage` |
| Simulator | `GET /scenarios/{id}/persona`, `POST /simulator/rehearse` |
| Test cases, Bulk import | `GET /scenarios`, `POST /scenarios/import` (all-or-nothing) |
| Suites | `GET /suites`, `PUT /suites` |
| Checks, Settings | `GET /checks`, `GET /config` (secrets redacted server-side) |

The API answers only to a loopback `Host` (the DNS-rebinding guard) and refuses to bind a real
interface without `--token`. Add `--allow-commit` to let runs reach the irreversible outcome; the
scenario must declare it too.
