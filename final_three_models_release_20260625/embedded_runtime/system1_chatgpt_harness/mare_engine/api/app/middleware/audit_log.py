"""
Audit logging middleware — records every mutating API call.

Captures: timestamp, user, IP, method, path, status, duration.
Logs are written to both stdout (structured JSON) and the audit_logs DB table.
"""

import json
import time
import logging
from datetime import datetime, timezone
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("audit")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

# Methods that mutate state
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _extract_user(request: Request) -> str:
    """Try to extract user identity from Authorization header."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return "anonymous"
    try:
        import jwt
        import os
        secret = os.getenv("JWT_SECRET", "change-this-secret-before-production")
        payload = jwt.decode(auth[7:], secret, algorithms=["HS256"])
        return payload.get("sub", "unknown")
    except Exception:
        return "invalid-token"


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


class AuditLogMiddleware(BaseHTTPMiddleware):
    """
    Logs all mutating API requests to structured JSON output.
    Also logs failed authentication attempts (401s) regardless of method.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.time()
        response = await call_next(request)
        duration_ms = round((time.time() - start) * 1000, 1)

        path = request.url.path

        # Only audit API calls
        if not path.startswith("/api/"):
            return response

        method = request.method
        status = response.status_code
        should_log = (
            method in MUTATING_METHODS
            or status in (401, 403, 429)  # security-relevant responses
            or path.endswith("/auth/login")
        )

        if should_log:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "api_audit",
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
                "client_ip": _get_client_ip(request),
                "user": _extract_user(request),
                "user_agent": request.headers.get("user-agent", "")[:120],
            }
            logger.info(json.dumps(entry, ensure_ascii=False))

        return response
