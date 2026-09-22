# ADR-0004: React + TypeScript web console

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

The console (from v0.5) needs a large contributor pool, RTL support for Arabic, and generated API types.

## Decision

Build the console with **React + TypeScript + Vite**, using TanStack Query/Router and shadcn/ui (Tailwind). The API client is generated from the server's OpenAPI schema. The browser never recomputes aggregates; it only renders server metrics.

## Consequences

- Largest front-end contributor pool.
- The console is served as static files by the API server (same origin).
