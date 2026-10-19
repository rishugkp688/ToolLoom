"""Explicit execution phases; an exception's Python class is not effect evidence."""


class ToolPreflightError(ValueError):
    """Rejected before execution began; no side effect was attempted."""
