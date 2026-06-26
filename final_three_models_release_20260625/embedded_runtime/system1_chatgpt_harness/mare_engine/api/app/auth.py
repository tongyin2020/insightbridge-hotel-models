import os
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import HTTPException
from passlib.context import CryptContext

JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-before-production")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(subject: str, role: str, hotel_id: str | None, session_token: str | None = None):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "hotel_id": hotel_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXPIRE_MINUTES)).timestamp()),
    }
    # Include session_token in JWT for server-side validation
    if session_token:
        payload["sid"] = session_token
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def require_auth(authorization: str | None):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid auth scheme")
    token = authorization.replace("Bearer ", "", 1)
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
