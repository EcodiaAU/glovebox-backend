# glovebox-backend

FastAPI (Python 3.11) + Pydantic 2 backend. `fly.toml` at the repo root
configures the Fly.io (Sydney) deployment target. Source of truth for the
OpenAPI spec consumed by `glovebox-ios`, `glovebox-android`, and `glovebox-web`
via generated clients.

The wider glovebox v1 architecture (Cap-wrapped Next.js frontend, frontend-
carplay, OSRM, edges DBs) lives in the sibling `D:/.code/glovebox/CLAUDE.md`.
This file is the doctrine that travels with the backend repo on a standalone
clone.

## Strict-Pydantic rule (v2 contract, enforced by `tests/test_openapi.py` + CI)

The backend is the canonical OpenAPI source for the three native client repos.
Untyped routes leak as `unknown` / `Any` into generated Swift, Kotlin, and
TypeScript clients, which then ship the gap downstream. The rule below keeps
the spec faithful enough that the generators emit usable types. The current
codebase satisfies it; every PR should keep it satisfied on every route in
`app/api/`:

- Do NOT use `Any` or untyped `dict` in route signatures. Type request bodies
  as Pydantic models; type query/path params as scalars.
- For JSON routes, set `response_model=<PydanticModel>` in the decorator.
- For binary / streaming routes, set `response_class=FileResponse` (or
  `StreamingResponse`) and add a `responses=` block naming the content type
  served (`application/octet-stream`, `application/zip`, etc).
- For routes that return error payloads via `JSONResponse`, declare the error
  shape via `responses={4xx: {"model": ErrorResponse}, ...}`. The shared
  `ErrorResponse` model in `app/core/error_models.py` preserves the legacy
  `{"error": "..."}` wire shape so existing v1 Cap clients keep parsing.
- Keep the OpenAPI version at `3.1.0` (FastAPI default). The baseline at
  `docs/openapi-3.1.0-locked.json` is the comparison target; `ci.yml` regenerates
  the spec on pushes to `main` (and PRs) that touch `app/`, `tests/`, or
  `requirements.txt`, diffs it against the baseline, and fails on drift.

## Tests

```
pip install pytest
pytest tests/ -q
```

`tests/test_openapi.py` is the baseline that enforces the strict-Pydantic rule
above by failing if any route is missing a 2xx response in the emitted
`openapi.json`, or if the spec drifts off `3.1.0`. Add real route-level tests
alongside it as the codebase grows.

## CI bot

`.github/workflows/bump-clients.yml` fires `repository_dispatch` events at the
three native repos when `openapi.json` drifts from the locked baseline. Auth is
a fine-grained PAT scoped to the three client repos, stored as the
`GLOVEBOX_BACKEND_BOT_TOKEN` repo secret (sourced from
`kv_store.creds.github_glovebox_backend_bot`). The workflow degrades gracefully
when the secret is absent: it still uploads `openapi.json` as an artifact and
logs a warning naming the cred to provision.
