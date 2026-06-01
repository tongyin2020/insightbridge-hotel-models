"""
In-memory sliding-window rate limiter middleware for FastAPI.

Limits requests per IP (or per authenticated user) to prevent abuse.
Configuration via environment variables:
  RATE_LIMIT_RPM        – max requests per minute (default 60)
  RATE_LIMIT_LOGIN_RPM  – stricter limit for /auth/login (default 10)
  RATE_LIMIT_ENABLED    – set to "0" to disable entirely (default "1")
"""

import os
import time
from collections import defaultdict
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENABLED = os.getenv("RATE_LIMIT_ENABLED", "1") == "1"
DEFAULT_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))
LOGIN_RPM = int(os.getenv("RATE_LIMIT_LOGIN_RPM", "10"))
WINDOW_SECONDS = 60

# ---------------------------------------------------------------------------
# In-memory store  (replace with Redis for multi-process deployments)
# ---------------------------------------------------------------------------
_buckets: dict[str, list[float]] = defaultdict(list)


def _clean_bucket(key: str, now: float) -> list[float]:
    """Remove timestamps older than the sliding window."""
    cutoff = now - WINDOW_SECONDS
    _buckets[key] = [t for t in _buckets[key] if t > cutoff]
    return _buckets[key]


def _get_client_key(request: Request) -> str:
    """Best-effort client identifier: forwarded IP > direct IP."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter.
    Returns 429 Too Many Requests when the client exceeds the limit.
    Adds X-RateLimit-* headers to every response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if not ENABLED:
            return await call_next(request)

        # Skip health checks
        if request.url.path in ("/api/v1/health", "/healthz"):
            return await call_next(request)

        client_ip = _get_client_key(request)
        path = request.url.path

        # Stricter limit for login endpoint
        is_login = path.endswith("/auth/login") and request.method == "POST"
        limit = LOGIN_RPM if is_login else DEFAULT_RPM
        bucket_key = f"login:{client_ip}" if is_login else f"api:{client_ip}"

        now = time.time()
        bucket = _clean_bucket(bucket_key, now)

        # Check limit
        remaining = max(0, limit - len(bucket))
        if len(bucket) >= limit:
            retry_after = int(WINDOW_SECONDS - (now - bucket[0])) + 1
            return Response(
                content='{"detail":"Too many requests. Please try again later."}',
                status_code=429,
                media_type="application/json",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(bucket[0] + WINDOW_SECONDS)),
                },
            )

        # Record this request
        bucket.append(now)
        _buckets[bucket_key] = bucket

        # Process request
        response = await call_next(request)

        # Add rate-limit headers
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining - 1 if remaining > 0 else 0)
        response.headers["X-RateLimit-Reset"] = str(int(now + WINDOW_SECONDS))

        return response
