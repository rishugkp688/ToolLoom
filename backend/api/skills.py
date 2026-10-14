from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, UploadFile

from backend.schemas.extra import SkillToggle
from backend.services.skills import SkillError

router = APIRouter(prefix="/skills", tags=["skills"])


def _service(request: Request):
    return request.app.state.skill_service


def _out(skill) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "fs_path": skill.fs_path,
        "enabled": skill.enabled,
        "auto_include": skill.auto_include,
        "hidden_from_model": skill.hidden_from_model,
    }


@router.get("")
def list_skills(request: Request) -> list[dict]:
    return [_out(skill) for skill in _service(request).list()]


@router.post("/rescan")
def rescan(request: Request) -> dict:
    return _service(request).rescan()


@router.post("/upload", status_code=201)
async def upload_skill(request: Request, file: UploadFile) -> dict:
    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="upload exceeds the 25 MB limit")
    try:
        result = _service(request).install(file.filename or "skill", data)
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"installed": result.get("installed", 0), "warnings": result.get("warnings", [])}


@router.patch("/{skill_id}")
def toggle_skill(skill_id: str, body: SkillToggle, request: Request) -> dict:
    skill = _service(request).set_enabled(skill_id, body.enabled)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return _out(skill)


@router.delete("/{skill_id}", status_code=204)
def delete_skill(skill_id: str, request: Request) -> None:
    try:
        deleted = _service(request).delete(skill_id)
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="skill not found")
