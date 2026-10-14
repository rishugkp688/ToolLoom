from __future__ import annotations

from fastapi import APIRouter, Request

from backend.schemas import HealthResponse

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    provider = request.app.state.provider
    return HealthResponse(
        status="ok",
        provider=provider.name,
        model_id=getattr(provider, "model_id", None),
        version=settings.version,
    )


@router.get("/config", tags=["meta"])
def config_status(request: Request) -> dict:
    """Non-sensitive runtime status (never returns key material)."""
    settings = request.app.state.settings
    provider = request.app.state.provider
    return {
        "provider": provider.name,
        "provider_configured": settings.provider_configured,
        "default_model": settings.commandcode_model,
        "max_iterations": settings.toolloom_max_iterations,
        "max_tool_calls": settings.toolloom_max_tool_calls,
        "allow_stdio": settings.toolloom_allow_stdio,
        "allow_python": settings.toolloom_allow_python,
        "pricing_configured": settings.toolloom_price_input_per_1k is not None
        or settings.toolloom_price_output_per_1k is not None,
    }


@router.get("/usage", tags=["meta"])
def usage(request: Request) -> dict:
    return request.app.state.usage_service.summary()
