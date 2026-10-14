"""FastAPI routers."""

from backend.api import (
    capabilities,
    conversations,
    health,
    mcp_servers,
    models_api,
    secrets,
    settings,
    skills,
)

__all__ = [
    "capabilities",
    "conversations",
    "health",
    "mcp_servers",
    "models_api",
    "secrets",
    "settings",
    "skills",
]
