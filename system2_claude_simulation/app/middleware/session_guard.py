"""
Session binding middleware — prevents password sharing across hotels/locations.

How it works:
  1. On login, a unique session_token is stored in the users table
  2. The session_token is embedded in the JWT as the "sid" claim
  3. This middleware extracts "sid" from the JWT and checks it against the DB
  4. If someone logs in again (same credentials, different device/location),
     the old session_token is overwritten → the old JWT's "sid" no longer matches
     → the old session gets a 401 "Session expired" response
  5. This ensures: one credential = one active session at a time

Why this prevents sharing:
  - Hotel A operator shares password with Hotel B operator
  - Hotel B operator logs in → new session_token generated
  - Hotel A operator's next API call fails because their JWT's "sid" is stale
  - Hotel A operator must log in again → invalidates Hotel B's session
  - Net effect: only one person can use the account at any given time
"""

import os
import json
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

try:
    import jwt as pyjwt
except ImportError:
    pyjwt = None

JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-before-production")

# Skip session check for these paths
EXEMPT_PATHS = {"/api/v1/health", "/api/v1/auth/login", "/healthz"}


class SessionGuardMiddleware(BaseHTTPMiddleware):
    """
    Validates that the JWT's session_token (sid) matches the current
    session_token stored in the database for that user.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Only check API routes, skip exemptions
        if not path.startswith("/api/") or path in EXEMPT_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer ") or pyjwt is None:
            return await call_next(request)

        # Decode JWT
        try:
            claims = pyjwt.decode(auth_header[7:], JWT_SECRET, algorithms=["HS256"])
        except Exception:
            return await call_next(request)  # let route handler deal with invalid JWTs

        sid = claims.get("sid")
        user_email = claims.get("sub")

        # If no sid in token (legacy tokens before v19.1), allow through
        if not sid or not user_email:
            return await call_next(request)

        # Check session_token against database
        from app.db.session import SessionLocal
        from app.models.user import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == user_email).first()
            if user and user.session_token and user.session_token != sid:
                return Response(
                    content=json.dumps({
                        "detail": "Session expired. Your account has been logged in from another location.",
                        "code": "SESSION_REPLACED"
                    }),
                    status_code=401,
                    media_type="application/json",
                )
        finally:
            db.close()

        return await call_next(request)
