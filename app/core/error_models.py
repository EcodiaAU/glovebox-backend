"""Shared response shapes for routes that don't have a domain-specific model.

ErrorResponse keeps the legacy `{"error": "..."}` payload shape that v1 clients
already parse, so the strict-Pydantic pass does not break the running app.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    error: str = Field(description="Human-readable error message")


class OkResponse(BaseModel):
    ok: bool


class UnlockedResponse(BaseModel):
    unlocked: bool
    source: str | None = None
    payment_status: str | None = None


class ReceivedResponse(BaseModel):
    received: bool


class CheckoutSessionResponse(BaseModel):
    url: str | None
