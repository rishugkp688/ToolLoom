from backend.services.providers.base import (
    BaseProvider,
    ModelInfo,
    ModelTurn,
    ProviderError,
    ProviderMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolSpec,
    Usage,
)
from backend.services.providers.fake import FakeProvider

__all__ = [
    "BaseProvider",
    "ModelInfo",
    "ModelTurn",
    "ProviderError",
    "ProviderMessage",
    "TextBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "ToolSpec",
    "Usage",
    "FakeProvider",
    "build_provider",
]


def build_provider(settings, *, script=None):
    """Construct the configured LLM provider."""
    if settings.llm_provider == "commandcode":
        from backend.services.providers.commandcode import CommandCodeProvider

        return CommandCodeProvider(settings)
    return FakeProvider(script=script)
