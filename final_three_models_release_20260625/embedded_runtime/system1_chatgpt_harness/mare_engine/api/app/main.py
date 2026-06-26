import os
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from .routes import auth, health, hotels, recommendations, analytics, pricing_history
from .routes import monitoring, bundles
from .routes import feedback_loop, policies, shadow_testing
from .routes import model_value, benchmark
from .middleware.rate_limit import RateLimitMiddleware
from .middleware.audit_log import AuditLogMiddleware
from .middleware.tenant_guard import TenantGuardMiddleware
from .middleware.session_guard import SessionGuardMiddleware
from .middleware.api_key_auth import APIKeyMiddleware
from .core import model_loader

logger = logging.getLogger(__name__)

allowed = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1")
origins = [x.strip() for x in allowed.split(",") if x.strip()]
is_production = os.getenv("ENVIRONMENT", "").lower() == "production"

# Disable docs in production
docs_kwargs = {}
if is_production:
    docs_kwargs = {"docs_url": None, "redoc_url": None, "openapi_url": None}

app = FastAPI(
    title=os.getenv("APP_NAME", "Macau AI Revenue Engine Product"),
    version="19.1.0",
    description="Database-driven multi-tenant SaaS for hotel dynamic pricing with monitoring, rate limiting, and tenant isolation.",
    **docs_kwargs,
)

# ---------------------------------------------------------------------------
# Middleware stack (order matters: outermost runs first)
# ---------------------------------------------------------------------------
# 1. CORS — must be outermost
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
)

# 2. Rate limiter — reject abusive traffic early
app.add_middleware(RateLimitMiddleware)

# 3. Audit logger — record all mutating API calls
app.add_middleware(AuditLogMiddleware)

# 4. Tenant guard — enforce cross-hotel data isolation
app.add_middleware(TenantGuardMiddleware)

# 5. Session guard — one credential = one active session (prevents sharing)
app.add_middleware(SessionGuardMiddleware)

# 6. API Key auth — second layer of auth for model endpoints
app.add_middleware(APIKeyMiddleware)

# 7. Noindex — this is an internal login app, must not be crawled or indexed
class NoIndexMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

app.add_middleware(NoIndexMiddleware)

# ---------------------------------------------------------------------------
# robots.txt — block all crawlers at the protocol level
# ---------------------------------------------------------------------------
@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(hotels.router, prefix="/api/v1/hotels", tags=["hotels"])
app.include_router(recommendations.router, prefix="/api/v1/recommendations", tags=["recommendations"])
app.include_router(pricing_history.router, prefix="/api/v1/pricing-history", tags=["pricing-history"])
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["analytics"])
app.include_router(monitoring.router, prefix="/api/v1/monitoring", tags=["monitoring"])
app.include_router(bundles.router, prefix="/api/v1/bundles", tags=["bundles"])
app.include_router(feedback_loop.router, prefix="/api/v1/feedback-loop", tags=["feedback-loop"])
app.include_router(policies.router, prefix="/api/v1/policies", tags=["policies"])
app.include_router(shadow_testing.router, prefix="/api/v1/shadow", tags=["shadow-testing"])
app.include_router(model_value.router, prefix="/api/v1/model-value", tags=["model-value"])
app.include_router(benchmark.router, prefix="/api/v1/benchmark", tags=["benchmark"])

# ---------------------------------------------------------------------------
# Startup: load model weights securely
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def load_model_on_startup():
    try:
        weights = model_loader.load_model_weights()
        if model_loader.verify_model_integrity(weights):
            app.state.model_weights = weights
            logger.info("Model weights loaded and verified successfully.")
        else:
            logger.warning("Model weights loaded but integrity check found missing keys.")
            app.state.model_weights = weights
    except FileNotFoundError:
        logger.warning("No model weights file found — starting without pre-loaded weights.")
        app.state.model_weights = {}
    except RuntimeError as e:
        logger.error(f"Failed to load model weights: {e}")
        app.state.model_weights = {}
