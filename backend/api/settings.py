from __future__ import annotations

from fastapi import APIRouter, Request

from backend.repositories import SettingRepository
from backend.schemas.extra import SettingsIn

router = APIRouter(tags=["settings"])


def _settings_response(request: Request) -> dict:
    with request.app.state.db.session() as session:
        setting = SettingRepository(session).get()
        return {
            "system_prompt": setting.system_prompt,
            "unlimited_tools": setting.unlimited_tools,
            "run_timeout_seconds": request.app.state.settings.toolloom_run_timeout_seconds,
        }


@router.get("/settings")
def get_settings(request: Request) -> dict:
    return _settings_response(request)


@router.put("/settings")
def put_settings(body: SettingsIn, request: Request) -> dict:
    with request.app.state.db.session() as session:
        repo = SettingRepository(session)
        if body.system_prompt is not None:
            repo.set_system_prompt(body.system_prompt)
        if body.unlimited_tools is not None:
            repo.get().unlimited_tools = body.unlimited_tools
    return _settings_response(request)
