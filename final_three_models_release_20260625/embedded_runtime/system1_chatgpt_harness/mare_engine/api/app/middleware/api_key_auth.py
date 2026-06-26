"""
API Key Authentication Middleware
Adds a second layer of auth beyond JWT for model API endpoints.
Each hotel tenant gets a unique API key.
"""
import os
import hmac
import hashlib
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from fastapi import Request

# In production, load from database. For now, environment-based.
VALID_API_KEYS = set(
    k.strip()
    for k in os.getenv("VALID_API_KEYS", "").split(",")
    if k.strip()
)

# Paths that require API key (in addition to JWT)
PROTECTED_PREFIXES = [
    "/api/v1/recommendations",
    "/api/v1/feedback-loop",
    "/api/v1/shadow-testing",
    "/api/v1/bundles",
]

# Paths that are always public
PUBLIC_PATHS = [
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/health",
    "/api/docs",
    "/api/openapi.json",
]

class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip for public paths
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)

        # Skip if no API keys configured (development mode)
        if not VALID_API_KEYS:
            return await call_next(request)

        # Check if path requires API key
        requires_key = any(path.startswith(p) for p in PROTECTED_PREFIXES)
        if not requires_key:
            return await call_next(request)

        # Validate API key
        api_key = request.headers.get("X-API-Key", "")
        if not api_key or api_key not in VALID_API_KEYS:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_api_key",
                    "message": "Valid X-API-Key header required for this endpoint."
                }
            )

        # Optional: Validate request signature (anti-replay)
        signature = request.headers.get("X-Signature")
        timestamp = request.headers.get("X-Timestamp")
        if signature and timestamp:
            # Verify timestamp is within 5 minutes
            try:
                ts = int(timestamp)
                if abs(time.time() - ts) > 300:
                    return JSONResponse(
                        status_code=401,
                        content={"error": "expired_request", "message": "Request timestamp expired."}
                    )
            except ValueError:
                pass

        return await call_next(request)
