"""
Auth routes with session binding + admin user management.

Session Binding:
  1. On login, a unique session_token (uuid4) is generated and stored
  2. The session_token is embedded in the JWT as "sid" claim
  3. If someone else logs in with the same credentials, the old session is killed
  4. Result: one account = one active session only (prevents password sharing)

Admin User Management:
  - Admin can create accounts with expiration dates
  - Admin can generate temporary passwords (trial accounts)
  - Admin can disable/enable accounts
  - Admin can list all users and their status
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import secrets
import string
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from typing import Optional
from app.db.session import get_db
from app.models.user import User
from app.auth import create_token, verify_password, hash_password, require_auth

router = APIRouter()


# ─── Request/Response Models ───

class LoginRequest(BaseModel):
    email: str
    password: str

class CreateUserRequest(BaseModel):
    email: str
    hotel_id: str
    role: str = "operator"
    valid_days: int = 365          # Account valid for N days (default 1 year)
    is_trial: bool = False         # Trial accounts can have shorter validity

class CreateUserResponse(BaseModel):
    email: str
    password: str                  # Generated password (shown once!)
    hotel_id: str
    role: str
    expires_at: str
    is_trial: bool
    message: str

class UpdateUserRequest(BaseModel):
    status: Optional[str] = None          # "active" / "suspended"
    valid_days: Optional[int] = None      # Extend or shorten validity
    new_password: Optional[bool] = False  # Generate a new password

class UserListItem(BaseModel):
    email: str
    hotel_id: Optional[str]
    role: str
    status: str
    is_trial: bool
    expires_at: Optional[str]
    last_login_at: Optional[str]
    last_login_ip: Optional[str]
    days_remaining: Optional[int]


# ─── Helpers ───

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"

def _generate_password(length: int = 12) -> str:
    """Generate a strong random password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    while True:
        pwd = ''.join(secrets.choice(alphabet) for _ in range(length))
        # Ensure at least 1 upper, 1 lower, 1 digit, 1 special
        if (any(c.isupper() for c in pwd) and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(c in "!@#$%" for c in pwd)):
            return pwd

def _require_admin(authorization: str = Header(None)):
    """Verify the caller is an admin."""
    claims = require_auth(authorization)
    if claims.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return claims


# ─── Login (with expiration check) ───

@router.post("/login")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email, User.status == "active").first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    # Check if account has expired
    if user.expires_at:
        now = datetime.now(timezone.utc)
        expires = user.expires_at.replace(tzinfo=timezone.utc) if user.expires_at.tzinfo is None else user.expires_at
        if now > expires:
            raise HTTPException(
                status_code=403,
                detail="Account expired. Please contact your administrator to renew."
            )

    # Generate new session token — invalidates any previous session
    new_session_token = uuid4().hex
    previous_session = user.session_token

    user.session_token = new_session_token
    user.last_login_ip = _get_client_ip(request)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    # Calculate days remaining
    days_remaining = None
    if user.expires_at:
        expires = user.expires_at.replace(tzinfo=timezone.utc) if user.expires_at.tzinfo is None else user.expires_at
        days_remaining = (expires - datetime.now(timezone.utc)).days

    response = {
        "access_token": create_token(user.email, user.role, user.hotel_id, new_session_token),
        "token_type": "bearer",
        "role": user.role,
        "hotel_id": user.hotel_id,
    }

    if days_remaining is not None and days_remaining <= 30:
        response["expiry_warning"] = f"Your account expires in {days_remaining} days. Please contact admin to renew."

    if previous_session:
        response["session_warning"] = "previous_session_terminated"

    return response


# ─── Admin: Create User (generate password + set expiration) ───

@router.post("/admin/create-user", response_model=CreateUserResponse)
def create_user(
    payload: CreateUserRequest,
    db: Session = Depends(get_db),
    admin: dict = Depends(_require_admin),
):
    """
    Admin creates a new hotel operator account.
    - Generates a random secure password
    - Sets account expiration date
    - Can mark as trial account
    """
    # Check if email already exists
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Generate password
    password = _generate_password()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=payload.valid_days)

    new_user = User(
        user_id=uuid4().hex,
        email=payload.email,
        password_hash=hash_password(password),
        hotel_id=payload.hotel_id,
        role=payload.role,
        status="active",
        expires_at=expires_at,
        created_at=now,
        created_by=admin.get("sub", "admin"),
        is_trial=payload.is_trial,
    )
    db.add(new_user)
    db.commit()

    return CreateUserResponse(
        email=payload.email,
        password=password,
        hotel_id=payload.hotel_id,
        role=payload.role,
        expires_at=expires_at.strftime("%Y-%m-%d %H:%M UTC"),
        is_trial=payload.is_trial,
        message=f"Account created. Valid for {payload.valid_days} days. Password shown only once!"
    )


# ─── Admin: Update User (extend, suspend, reset password) ───

@router.put("/admin/users/{email}")
def update_user(
    email: str,
    payload: UpdateUserRequest,
    db: Session = Depends(get_db),
    admin: dict = Depends(_require_admin),
):
    """
    Admin can:
    - Suspend or reactivate an account
    - Extend or shorten account validity
    - Generate a new password
    """
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    result = {"email": email, "changes": []}

    if payload.status and payload.status in ("active", "suspended"):
        user.status = payload.status
        # If suspended, kill their session immediately
        if payload.status == "suspended":
            user.session_token = None
        result["changes"].append(f"Status → {payload.status}")

    if payload.valid_days is not None:
        now = datetime.now(timezone.utc)
        user.expires_at = now + timedelta(days=payload.valid_days)
        result["changes"].append(f"Expires → {user.expires_at.strftime('%Y-%m-%d')} ({payload.valid_days} days)")

    new_password = None
    if payload.new_password:
        new_password = _generate_password()
        user.password_hash = hash_password(new_password)
        user.session_token = None  # Force re-login
        result["changes"].append("Password reset (new password generated)")
        result["new_password"] = new_password

    db.commit()
    return result


# ─── Admin: List All Users ───

@router.get("/admin/users")
def list_users(
    db: Session = Depends(get_db),
    admin: dict = Depends(_require_admin),
):
    """List all users with their status, expiration, and last login info."""
    users = db.query(User).order_by(User.created_at.desc()).all()
    now = datetime.now(timezone.utc)

    result = []
    for u in users:
        days_remaining = None
        if u.expires_at:
            exp = u.expires_at.replace(tzinfo=timezone.utc) if u.expires_at.tzinfo is None else u.expires_at
            days_remaining = (exp - now).days

        result.append(UserListItem(
            email=u.email,
            hotel_id=u.hotel_id,
            role=u.role,
            status=u.status if (days_remaining is None or days_remaining > 0) else "expired",
            is_trial=u.is_trial or False,
            expires_at=u.expires_at.strftime("%Y-%m-%d") if u.expires_at else None,
            last_login_at=u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else None,
            last_login_ip=u.last_login_ip,
            days_remaining=days_remaining,
        ))

    return {"total": len(result), "users": result}


# ─── Admin: Delete User ───

@router.delete("/admin/users/{email}")
def delete_user(
    email: str,
    db: Session = Depends(get_db),
    admin: dict = Depends(_require_admin),
):
    """Permanently delete a user account."""
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete admin accounts")

    db.delete(user)
    db.commit()
    return {"message": f"User {email} deleted successfully"}
