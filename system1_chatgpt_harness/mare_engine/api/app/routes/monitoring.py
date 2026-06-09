"""
Monitoring & system health routes — admin only.

Provides:
  /api/v1/monitoring/health      — deep health check (DB, model weights, disk)
  /api/v1/monitoring/metrics     — key system metrics
  /api/v1/monitoring/audit-log   — recent audit log entries
"""

import os
import time
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text, func

from app.db.session import get_db
from app.auth import require_auth
from app.models.hotel import Hotel
from app.models.user import User
from app.models.pricing_history import PricingHistory
from app.models.recommendation_feedback import RecommendationFeedback

router = APIRouter()

MODEL_WEIGHTS_PATH = Path(os.getenv("MODEL_WEIGHTS_PATH", "/app/data/model_weights.json"))

# ---------------------------------------------------------------------------
#  Track startup time and request count for uptime metrics
# ---------------------------------------------------------------------------
_startup_time = time.time()
_request_counts: dict[str, int] = {"total": 0, "errors": 0}


def _require_admin(authorization: str | None):
    """Only admins can access monitoring endpoints."""
    claims = require_auth(authorization)
    if claims.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required for monitoring")
    return claims


# ---------------------------------------------------------------------------
#  Deep health check
# ---------------------------------------------------------------------------
@router.get("/health")
def deep_health(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)

    checks = {}

    # 1. Database connectivity
    try:
        result = db.execute(text("SELECT 1"))
        result.fetchone()
        checks["database"] = {"status": "ok", "detail": "PostgreSQL reachable"}
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)[:200]}

    # 2. Database row counts
    try:
        counts = {
            "hotels": db.query(func.count(Hotel.id)).scalar(),
            "users": db.query(func.count(User.id)).scalar(),
            "pricing_history": db.query(func.count(PricingHistory.id)).scalar(),
            "feedback": db.query(func.count(RecommendationFeedback.id)).scalar(),
        }
        checks["data_counts"] = {"status": "ok", "counts": counts}
    except Exception as e:
        checks["data_counts"] = {"status": "error", "detail": str(e)[:200]}

    # 3. Model weights file
    try:
        if MODEL_WEIGHTS_PATH.exists():
            weights = json.loads(MODEL_WEIGHTS_PATH.read_text(encoding="utf-8"))
            factor_count = len(weights.get("demand_weights", {}))
            mod_time = datetime.fromtimestamp(
                MODEL_WEIGHTS_PATH.stat().st_mtime, tz=timezone.utc
            ).isoformat()
            checks["model_weights"] = {
                "status": "ok",
                "path": str(MODEL_WEIGHTS_PATH),
                "factors": factor_count,
                "last_modified": mod_time,
            }
        else:
            checks["model_weights"] = {
                "status": "warning",
                "detail": "model_weights.json not found, using defaults",
            }
    except Exception as e:
        checks["model_weights"] = {"status": "error", "detail": str(e)[:200]}

    # 4. Disk space
    try:
        stat = os.statvfs("/app/data")
        total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        used_pct = round((1 - free_gb / total_gb) * 100, 1)
        disk_status = "ok" if used_pct < 85 else "warning" if used_pct < 95 else "critical"
        checks["disk"] = {
            "status": disk_status,
            "total_gb": round(total_gb, 2),
            "free_gb": round(free_gb, 2),
            "used_percent": used_pct,
        }
    except Exception:
        checks["disk"] = {"status": "ok", "detail": "disk check not available on this platform"}

    # 5. Uptime
    uptime_seconds = int(time.time() - _startup_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    checks["uptime"] = {
        "status": "ok",
        "seconds": uptime_seconds,
        "human": f"{hours}h {minutes}m {seconds}s",
    }

    # Overall status
    statuses = [c.get("status", "ok") for c in checks.values()]
    overall = "error" if "error" in statuses else "warning" if "warning" in statuses else "ok"

    return {
        "overall": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
#  System metrics
# ---------------------------------------------------------------------------
@router.get("/metrics")
def system_metrics(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)

    # Recommendation stats
    total_recs = db.query(func.count(PricingHistory.id)).scalar() or 0
    applied_recs = (
        db.query(func.count(PricingHistory.id))
        .filter(PricingHistory.status == "applied")
        .scalar()
        or 0
    )
    total_feedback = db.query(func.count(RecommendationFeedback.id)).scalar() or 0

    # Revenue outcomes from feedback
    positive_outcomes = (
        db.query(func.count(RecommendationFeedback.id))
        .filter(RecommendationFeedback.revenue_outcome > 0)
        .scalar()
        or 0
    )

    # Average recommended price
    avg_rec_price = (
        db.query(func.avg(PricingHistory.recommended_price)).scalar() or 0
    )

    # Learning loop readiness
    training_ready = total_feedback >= 20

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "recommendations": {
            "total": total_recs,
            "applied": applied_recs,
            "application_rate": f"{(applied_recs / max(total_recs, 1) * 100):.1f}%",
            "avg_recommended_price": round(float(avg_rec_price), 2),
        },
        "feedback": {
            "total": total_feedback,
            "positive_revenue_outcomes": positive_outcomes,
            "positive_rate": f"{(positive_outcomes / max(total_feedback, 1) * 100):.1f}%",
        },
        "ml_feedback_status": {
            "feedback_collected": total_feedback,
            "min_for_stable_feedback": 20,
            "ready_for_stable_learning": training_ready,
        },
        "system": {
            "uptime_seconds": int(time.time() - _startup_time),
            "api_version": "19.0.0",
            "python_pid": os.getpid(),
        },
    }
