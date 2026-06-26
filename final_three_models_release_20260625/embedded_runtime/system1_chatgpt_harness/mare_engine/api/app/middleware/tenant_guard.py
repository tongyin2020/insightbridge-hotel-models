"""
Tenant authorization guard — enforces strict data isolation.

Every request body and URL that contains a hotel_id is checked:
  - admin role: allowed to access any hotel
  - hotel_operator role: allowed ONLY if hotel_id matches their JWT claim

This middleware catches authorization violations that individual routes
might miss, providing defense-in-depth for multi-tenant security.
"""

import json
import os
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

try:
    import jwt as pyjwt
except ImportError:
    pyjwt = None

JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-before-production")


def _decode_token(request: Request) -> dict | None:
    """Decode JWT from Authorization header. Returns None if absent/invalid."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or pyjwt is None:
        return None
    try:
        return pyjwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None


def _extract_hotel_id_from_path(path: str) -> str | None:
    """Extract hotel_id from URL patterns like /api/v1/hotels/{hotel_id}/..."""
    parts = path.rstrip("/").split("/")
    # Pattern: /api/v1/hotels/{hotel_id}/settings
    try:
        idx = parts.index("hotels")
        if idx + 1 < len(parts):
            candidate = parts[idx + 1]
            if candidate and not candidate.startswith("{"):
                return candidate
    except ValueError:
        pass
    return None


class TenantGuardMiddleware(BaseHTTPMiddleware):
    """
    Defense-in-depth tenant isolation.
    Blocks hotel_operators from accessing hotels they don't belong to.
    Works for both URL-based and body-based hotel_id references.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Only guard API routes
        if not path.startswith("/api/"):
            return await call_next(request)

        # Skip public endpoints
        if path.endswith("/health") or path.endswith("/auth/login"):
            return await call_next(request)

        claims = _decode_token(request)
        if not claims:
            # Let the route handler deal with missing/invalid auth
            return await call_next(request)

        role = claims.get("role", "")
        user_hotel_id = claims.get("hotel_id")

        # Admins can access everything
        if role == "admin":
            return await call_next(request)

        # For non-admin users, enforce hotel_id match

        # 1. Check URL path
        path_hotel_id = _extract_hotel_id_from_path(path)
        if path_hotel_id and path_hotel_id != user_hotel_id:
            return Response(
                content='{"detail":"Tenant violation: you cannot access another hotel\'s data."}',
                status_code=403,
                media_type="application/json",
            )

        # 2. Check request body for POST/PUT/PATCH
        if request.method in ("POST", "PUT", "PATCH"):
            # We need to read the body, then put it back
            body_bytes = await request.body()
            if body_bytes:
                try:
                    body = json.loads(body_bytes)
                    body_hotel_id = body.get("hotel_id")
                    if body_hotel_id and body_hotel_id != user_hotel_id:
                        return Response(
                            content='{"detail":"Tenant violation: hotel_id in request body does not match your account."}',
                            status_code=403,
                            media_type="application/json",
                        )
                except (json.JSONDecodeError, AttributeError):
                    pass  # Not JSON body, skip

        # 3. Check query parameters
        query_hotel_id = request.query_params.get("hotel_id")
        if query_hotel_id and query_hotel_id != user_hotel_id:
            return Response(
                content='{"detail":"Tenant violation: hotel_id in query does not match your account."}',
                status_code=403,
                media_type="application/json",
            )

        return await call_next(request)
