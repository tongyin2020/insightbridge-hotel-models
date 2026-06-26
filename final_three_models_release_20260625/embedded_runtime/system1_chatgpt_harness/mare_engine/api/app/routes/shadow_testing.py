"""Routes for shadow parameter testing.

Endpoints
---------
GET  /api/v1/shadow/{hotel_id}/status   – current shadow status & performance
POST /api/v1/shadow/{hotel_id}/promote  – promote shadow weights to production
GET  /api/v1/shadow/{hotel_id}/history  – shadow comparison history
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from app.auth import require_auth
from app.services.shadow_testing import (
    _load_shadow_history,
    _load_shadow_weights,
    auto_evolve_shadow,
    evaluate_shadow_performance,
    promote_shadow_to_production,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class ShadowStatusResponse(BaseModel):
    hotel_id: str
    shadow_weights: dict
    performance: dict
    auto_evolve_available: bool = True


class ShadowPromoteResponse(BaseModel):
    status: str
    promoted_weights: dict


class ShadowHistoryEntry(BaseModel):
    hotel_id: str | None = None
    production_price: int | None = None
    shadow_price: int | None = None
    price_delta: int | None = None
    timestamp: str | None = None


class ShadowHistoryResponse(BaseModel):
    hotel_id: str
    entries: list[ShadowHistoryEntry]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enforce_admin(claims: dict) -> None:
    if claims["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


def _enforce_access(claims: dict, hotel_id: str) -> None:
    if claims["role"] != "admin" and claims.get("hotel_id") != hotel_id:
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# GET /shadow/{hotel_id}/status
# ---------------------------------------------------------------------------

@router.get("/{hotel_id}/status", response_model=ShadowStatusResponse)
def shadow_status(
    hotel_id: str,
    authorization: str | None = Header(default=None),
):
    """Return current shadow weights and win-rate performance."""
    claims = require_auth(authorization)
    _enforce_access(claims, hotel_id)

    shadow_weights = _load_shadow_weights()
    perf = evaluate_shadow_performance(hotel_id)

    return ShadowStatusResponse(
        hotel_id=hotel_id,
        shadow_weights=shadow_weights,
        performance={
            "window_size": perf.window_size,
            "shadow_win_rate": perf.shadow_win_rate,
            "recommendation": perf.recommendation,
        },
    )


# ---------------------------------------------------------------------------
# POST /shadow/{hotel_id}/promote
# ---------------------------------------------------------------------------

@router.post("/{hotel_id}/promote", response_model=ShadowPromoteResponse)
def promote_shadow(
    hotel_id: str,
    authorization: str | None = Header(default=None),
):
    """Promote shadow weights to production (admin only).

    After promotion a fresh shadow is created with a small perturbation.
    """
    claims = require_auth(authorization)
    _enforce_admin(claims)

    promoted = promote_shadow_to_production()
    # Also evolve for the next cycle
    auto_evolve_shadow(hotel_id, magnitude=0.03)

    return ShadowPromoteResponse(
        status="promoted",
        promoted_weights=promoted,
    )


# ---------------------------------------------------------------------------
# GET /shadow/{hotel_id}/history
# ---------------------------------------------------------------------------

@router.get("/{hotel_id}/history", response_model=ShadowHistoryResponse)
def shadow_history(
    hotel_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    authorization: str | None = Header(default=None),
):
    """Return shadow vs production comparison history."""
    claims = require_auth(authorization)
    _enforce_access(claims, hotel_id)

    all_entries = _load_shadow_history()
    hotel_entries = [e for e in all_entries if e.get("hotel_id") == hotel_id]
    # Most recent first
    hotel_entries = hotel_entries[-limit:][::-1]

    return ShadowHistoryResponse(
        hotel_id=hotel_id,
        entries=[
            ShadowHistoryEntry(
                hotel_id=e.get("hotel_id"),
                production_price=e.get("production_price"),
                shadow_price=e.get("shadow_price"),
                price_delta=e.get("price_delta"),
                timestamp=e.get("timestamp"),
            )
            for e in hotel_entries
        ],
        total=len(hotel_entries),
    )
