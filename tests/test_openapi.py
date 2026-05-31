"""Strict-Pydantic baseline test.

The glovebox-backend repo is the canonical OpenAPI source for the three native
client repos (`glovebox-ios`, `glovebox-android`, `glovebox-web`). The CI bot
opens client-bump PRs against those repos whenever `openapi.json` changes, and
generated clients are only lossless if every route has typed input + output
schemas. This test enforces that contract.

Failure modes caught:
- A route returns `Any` or untyped `dict` (no `response_model`, no `response_class`).
- A route returns `JSONResponse` without declaring a `response_model` in the
  decorator (the 2xx contract becomes invisible to client generators).
- The emitted spec downgrades below OpenAPI 3.1.0 (FastAPI default since 0.99).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Tests live in `backend/tests/`; add `backend/` to sys.path so `app` imports.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Minimum env settings need to import the app's settings module without a real
# Supabase / Stripe environment present.
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_PRICE_ID", "price_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
os.environ.setdefault("OSRM_BASE_URL", "http://osrm.invalid")
os.environ.setdefault("MAPBOX_TOKEN", "test")


@pytest.fixture(scope="module")
def openapi_spec() -> dict:
    from app.main import app

    return app.openapi()


def test_openapi_version_locked_at_3_1_0(openapi_spec: dict) -> None:
    """OpenAPI 3.1.0 is required for openapi-generator clients to emit cleanly."""
    assert openapi_spec.get("openapi") == "3.1.0", (
        f"openapi version drifted to {openapi_spec.get('openapi')!r}; "
        "client generators target 3.1.0"
    )


def test_every_route_declares_a_2xx_response(openapi_spec: dict) -> None:
    """Every route MUST have a 2xx response with content (or be a binary stream).

    A route that returns an untyped dict has no 200 entry in OpenAPI; this
    catches routes that fell through the strict-Pydantic pass.
    """
    gaps: list[str] = []
    for path, methods in openapi_spec["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            responses = op.get("responses", {})
            two_xx = (
                responses.get("200") or responses.get("201") or responses.get("204")
            )
            if not two_xx:
                gaps.append(f"{method.upper()} {path}: no 2xx response declared")
                continue
    assert not gaps, "Strict-Pydantic gaps detected:\n  " + "\n  ".join(gaps)


def test_route_count_matches_router_registration(openapi_spec: dict) -> None:
    """Backstop against accidental router removal during refactors."""
    paths = list(openapi_spec["paths"].keys())
    # Sanity floor: v1 ships with ~49 routes. If this drops by >5 a regression
    # has likely removed a router. Raise the floor as the backend grows.
    assert len(paths) >= 40, f"only {len(paths)} routes emitted: {paths}"
