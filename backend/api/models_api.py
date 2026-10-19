from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.schemas import ModelInfo
from backend.services.failures import failure_payload
from backend.services.model_catalogue import budget_for

router = APIRouter(tags=["meta"])


@router.get("/models", response_model=list[ModelInfo])
async def list_models(request: Request) -> list[ModelInfo]:
    try:
        infos = await request.app.state.model_catalogue.list()
    except Exception as exc:  # surface provider failures precisely
        failure = failure_payload(exc, request.app.state.tool_executor._redact)
        raise HTTPException(
            status_code=failure["status"], detail=failure["message"],
            headers={"X-ToolLoom-Error": failure["category"]},
        ) from exc
    result = []
    for info in infos:
        budget = budget_for(request.app.state.settings, info)
        result.append(
            ModelInfo(
                id=info.id,
                label=info.label,
                context_window=info.context_window,
                effective_context_window=budget.context_window,
                context_source=budget.source,
                max_output_tokens=budget.max_output_tokens,
            )
        )
    return result
