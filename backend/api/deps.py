"""FastAPI dependency providers. All state lives on ``request.app.state``."""

from __future__ import annotations

from fastapi import Request

from backend.config import Settings
from backend.db import Database


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_runtime(request: Request):
    return request.app.state.runtime
