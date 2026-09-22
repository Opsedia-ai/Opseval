# ADR-0003: Python core, Pydantic models, OpenAI-compatible HTTP only

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

The eval and LLM tooling ecosystem is Python-first. Vendor SDKs change often and pull in heavy dependencies.

## Decision

- Core language: **Python ≥ 3.11**. Data models: **Pydantic v2**, with `extra="forbid"` and frozen run records. CLI: Typer. Server: FastAPI.
- Opseval talks to LLM providers only through the **OpenAI-compatible `POST /chat/completions`** API, using httpx. There are no vendor SDKs. Simulator and judge roles are configured independently.

## Consequences

- Works with any OpenAI-compatible provider or gateway, including self-hosted vLLM and Ollama.
- Provider-specific features beyond this API need an adapter plugin.
