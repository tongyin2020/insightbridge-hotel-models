from datetime import datetime, timezone
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    return {
        "status": "ok",
        "product": "Macau AI Revenue Engine v19.1 Product",
        "version": "19.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "features": [
            "rate_limiting",
            "audit_logging",
            "tenant_isolation",
            "monitoring_dashboard",
        ],
    }
