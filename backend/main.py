"""FastAPI application factory.

Builds the engine/session holder, wires the provider, tools, MCP, secrets, skills
and agent runtime, applies local-only security middleware, and mounts the vanilla
frontend.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api import (
    conversations,
    health,
    mcp_servers,
    models_api,
)
from backend.api import (
    secrets as secrets_api,
)
from backend.api import (
    settings as settings_api,
)
from backend.api import (
    skills as skills_api,
)
from backend.config import Settings
from backend.db import Database
from backend.runtime import AgentRuntime
from backend.services.context_compactor import ContextCompactor
from backend.services.conversation_titles import ConversationTitles
from backend.services.mcp_connection_manager import MCPConnectionManager
from backend.services.model_catalogue import ModelCatalogue
from backend.services.providers import build_provider
from backend.services.secret_service import SecretService
from backend.services.secrets import DbResolver, Vault, load_or_generate_key
from backend.services.skill_service import SkillService
from backend.services.tool_executor import ToolExecutor
from backend.services.tool_registry import ToolRegistry
from backend.services.usage_service import UsageService

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _hostname(value: str) -> str:
    text = value.strip()
    if text.startswith("["):  # IPv6 literal, e.g. [::1]:8000
        return text[1:].split("]", 1)[0]
    return text.rsplit(":", 1)[0] if ":" in text else text


def _allowed_host(value: str) -> bool:
    return _hostname(value) in _LOCAL_HOSTNAMES


def _allowed_origin(origin: str, expected: str) -> bool:
    try:
        parts = urlsplit(origin)
        target = urlsplit(expected)

        def port(value):
            return value.port or (443 if value.scheme == "https" else 80)

        return (
            parts.scheme in {"http", "https"}
            and parts.hostname in _LOCAL_HOSTNAMES
            and not parts.username
            and not parts.password
            and not parts.path
            and not parts.query
            and not parts.fragment
            and (parts.scheme, parts.hostname, port(parts))
            == (target.scheme, target.hostname, port(target))
        )
    except ValueError:
        return False


def create_app(settings: Settings | None = None, provider=None) -> FastAPI:
    settings = settings or Settings()
    settings.validate_runtime()
    settings.ensure_dirs()

    db = Database(settings.database_url)
    db.create_all()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            db.dispose()

    app = FastAPI(title="ToolLoom", version=settings.version, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.active_runs = set()
    app.state.provider = provider or build_provider(settings)
    app.state.model_catalogue = ModelCatalogue(app.state.provider, settings)

    vault = Vault(load_or_generate_key(settings.secrets_key_path, settings.toolloom_secrets_key))
    secret_resolver = DbResolver(db, vault)

    connection_manager = MCPConnectionManager(settings, secret_resolver=secret_resolver)
    tool_registry = ToolRegistry(db, settings, connection_manager)
    tool_registry.seed()
    tool_executor = ToolExecutor(db, settings, connection_manager, secret_resolver=secret_resolver)

    skill_service = SkillService(db, settings)
    skill_service.seed()
    secret_service = SecretService(db, vault)
    compactor = ContextCompactor(db, settings, app.state.provider)

    app.state.vault = vault
    app.state.secret_resolver = secret_resolver
    app.state.secret_service = secret_service
    app.state.skill_service = skill_service
    app.state.usage_service = UsageService(db, settings)
    app.state.mcp_connection_manager = connection_manager
    app.state.tool_registry = tool_registry
    app.state.tool_executor = tool_executor
    app.state.conversation_titles = ConversationTitles(
        db, settings, app.state.provider, app.state.model_catalogue, tool_executor._redact
    )
    app.state.runtime = AgentRuntime(
        db,
        settings,
        app.state.provider,
        tool_executor=tool_executor,
        compactor=compactor,
        model_catalogue=app.state.model_catalogue,
    )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        host = request.headers.get("host")
        if not host or not _allowed_host(host):
            return JSONResponse(status_code=403, content={"detail": "host not allowed"})
        if request.method in _STATE_CHANGING:
            host = request.headers.get("host")
            if host and not _allowed_host(host):
                return JSONResponse(status_code=403, content={"detail": "host not allowed"})
            origin = request.headers.get("origin")
            if origin and not _allowed_origin(origin, str(request.base_url).rstrip("/")):
                return JSONResponse(status_code=403, content={"detail": "origin not allowed"})
        try:
            return await call_next(request)
        except Exception as exc:
            # Avoid logging arbitrary exception strings, which may contain credentials.
            logging.getLogger(__name__).error("Request failed (%s)", type(exc).__name__)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error. Check backend storage and retry."},
            )

    app.include_router(health.router)
    app.include_router(models_api.router)
    app.include_router(conversations.router)
    app.include_router(mcp_servers.router)
    app.include_router(secrets_api.router)
    app.include_router(skills_api.router)
    app.include_router(settings_api.router)

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app
