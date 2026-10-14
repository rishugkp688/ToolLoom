from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.schemas.extra import SecretIn
from backend.services.secrets import SecretsError

router = APIRouter(prefix="/secrets", tags=["secrets"])


def _service(request: Request):
    return request.app.state.secret_service


@router.get("")
def list_secrets(request: Request) -> list[dict]:
    return _service(request).list()


@router.post("", status_code=201)
def upsert_secret(body: SecretIn, request: Request) -> dict:
    try:
        return _service(request).upsert(body.name, body.value, body.description)
    except SecretsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{name}", status_code=204)
def delete_secret(name: str, request: Request) -> None:
    if not _service(request).delete(name):
        raise HTTPException(status_code=404, detail="secret not found")
