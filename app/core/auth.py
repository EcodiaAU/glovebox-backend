# app/core/auth.py
#
# Supabase JWT verification for protected endpoints.
# Validates the access_token from the Authorization header against
# the Supabase project's JWKS (JSON Web Key Set).
#
# Supabase now uses ECC P-256 (ES256) signing keys by default.
# The JWKS client fetches the public key and PyJWT handles the algorithm
# automatically from the key type - we just allowlist all Supabase-issued algs.

from __future__ import annotations

import logging
from typing import Optional

import jwt
from fastapi import HTTPException, Request
from pydantic import BaseModel

from app.core.settings import settings

logger = logging.getLogger(__name__)


class AuthUser(BaseModel):
    id: str
    email: Optional[str] = None
    # The person's canonical Ecodia Friend id, copied into app_metadata.friend_id
    # by the project's copy-forward trigger pair when they connect their Friend.
    # Present only for Friend-federated users; None for plain Glovebox logins.
    friend_id: Optional[str] = None


def _get_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return auth[7:]


def _get_jwks_url() -> str:
    """Build the JWKS URL from the Supabase project URL."""
    base = (settings.supa_url or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=500, detail="SUPA_URL not configured")
    return f"{base}/auth/v1/.well-known/jwks.json"


_jwks_client: Optional[jwt.PyJWKClient] = None


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(_get_jwks_url(), cache_keys=True)
    return _jwks_client


# Algorithms Supabase may use: ES256 (current P-256 keys), RS256 (legacy RSA),
# HS256 (legacy shared secret - still verifies old unexpired tokens).
_SUPABASE_ALGORITHMS = ["ES256", "RS256", "HS256"]


def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency - extracts and validates the Supabase JWT."""
    token = _get_token(request)

    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=_SUPABASE_ALGORITHMS,
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as exc:
        logger.warning("[auth] Invalid token: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub claim")

    # Supabase mints app_metadata into the access token, so the Friend id
    # rides along with every authenticated request - no admin lookup needed.
    app_meta = payload.get("app_metadata")
    friend_id = app_meta.get("friend_id") if isinstance(app_meta, dict) else None

    return AuthUser(
        id=user_id,
        email=payload.get("email"),
        friend_id=friend_id or None,
    )


def get_optional_user(request: Request) -> Optional[AuthUser]:
    """FastAPI dependency - returns AuthUser if a valid token is present, else None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        return get_current_user(request)
    except HTTPException:
        return None
