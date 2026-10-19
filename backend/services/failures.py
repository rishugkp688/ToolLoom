"""Public error contract shared by streamed and ordinary API responses."""

from backend.services.context_builder import ContextBudgetError
from backend.services.providers.base import ProviderError
from backend.utils.sqlite_retry import is_sqlite_busy


def failure_payload(exc: Exception, redact=lambda value: value) -> dict:
    if is_sqlite_busy(exc):
        return {
            "message": "Local storage is busy. "
                       "Wait and inspect saved run state before retrying.",
            "status": 503,
            "category": "storage_busy",
        }
    if isinstance(exc, ContextBudgetError):
        return {"message": redact(str(exc)), "status": 413, "category": "context_budget"}
    if isinstance(exc, ProviderError):
        return {"message": redact(str(exc)), "status": exc.status, "category": exc.category}
    return {
        "message": "Run failed unexpectedly. Check backend storage and retry.",
        "status": 500,
        "category": "internal",
    }

