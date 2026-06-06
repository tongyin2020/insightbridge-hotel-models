"""Routes for the unified policy engine configuration and violation review.

Endpoints
---------
GET  /api/v1/policies/{hotel_id}             – current policy config
PUT  /api/v1/policies/{hotel_id}             – update rule parameters
GET  /api/v1/policies/{hotel_id}/violations  – recent violations
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db.session import get_db
from app.services.policy_engine import PolicyEngine

router = APIRouter()

# Module-level engine instance (rules are hotel-agnostic defaults for now;
# per-hotel overrides can be stored in DB and merged at request time).
_engine = PolicyEngine()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RuleUpdate(BaseModel):
    """Body for PUT to update one rule."""
    rule_name: str
    updates: dict  # e.g. {"enabled": false, "max_deviation_pct": 0.25}


class PolicyConfigResponse(BaseModel):
    hotel_id: str
    rules: list[dict]


class ViolationEntry(BaseModel):
    rule: str
    severity: str
    message: str
    suggested_action: str | None = None


class ViolationHistoryResponse(BaseModel):
    hotel_id: str
    violations: list[ViolationEntry]
    note: str = "Violations are computed on-demand from the latest recommendation."


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
# GET /policies/{hotel_id}
# ---------------------------------------------------------------------------

@router.get("/{hotel_id}", response_model=PolicyConfigResponse)
def get_policies(
    hotel_id: str,
    authorization: str | None = Header(default=None),
):
    """Return current policy rule configuration."""
    claims = require_auth(authorization)
    _enforce_access(claims, hotel_id)
    return PolicyConfigResponse(
        hotel_id=hotel_id,
        rules=_engine.get_rule_configs(),
    )


# ---------------------------------------------------------------------------
# PUT /policies/{hotel_id}
# ---------------------------------------------------------------------------

@router.put("/{hotel_id}", response_model=PolicyConfigResponse)
def update_policies(
    hotel_id: str,
    body: RuleUpdate,
    authorization: str | None = Header(default=None),
):
    """Update a policy rule's parameters (admin only)."""
    claims = require_auth(authorization)
    _enforce_admin(claims)

    found = _engine.update_rule_config(body.rule_name, body.updates)
    if not found:
        raise HTTPException(status_code=404, detail=f"Rule '{body.rule_name}' not found")

    return PolicyConfigResponse(
        hotel_id=hotel_id,
        rules=_engine.get_rule_configs(),
    )


# ---------------------------------------------------------------------------
# GET /policies/{hotel_id}/violations
# ---------------------------------------------------------------------------

@router.get("/{hotel_id}/violations", response_model=ViolationHistoryResponse)
def get_violations(
    hotel_id: str,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """Return recent guardrail violations for a hotel.

    For now this re-evaluates the latest recommendation through the policy
    engine.  A production system would store violations persistently.
    """
    from app.models.pricing_history import PricingHistory
    from app.models.hotel_setting import HotelSetting
    from app.services.policy_engine import PricingContext

    claims = require_auth(authorization)
    _enforce_access(claims, hotel_id)

    latest = (
        db.query(PricingHistory)
        .filter(PricingHistory.hotel_id == hotel_id)
        .order_by(PricingHistory.id.desc())
        .first()
    )
    if not latest:
        return ViolationHistoryResponse(hotel_id=hotel_id, violations=[])

    settings = (
        db.query(HotelSetting)
        .filter(HotelSetting.hotel_id == hotel_id)
        .first()
    )

    ctx = PricingContext(
        proposed_price=latest.recommended_price,
        base_price=settings.base_price if settings else 898,
        floor_price=settings.floor_price if settings else 750,
        ceiling_price=settings.ceiling_price if settings else 1015,
        demand_score=latest.demand_score or 0,
        season=latest.season or "shoulder",
    )
    report = _engine.evaluate(ctx)

    return ViolationHistoryResponse(
        hotel_id=hotel_id,
        violations=[
            ViolationEntry(
                rule=v.rule_name,
                severity=v.severity,
                message=v.message,
                suggested_action=v.suggested_action,
            )
            for v in report.violations
        ],
    )
