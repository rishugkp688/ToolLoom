"""Reject values that cannot be stored or serialized safely at the API boundary."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, model_validator

_MAX_DEPTH = 20


def _check(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise ValueError("input is nested too deeply")
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("text contains an invalid Unicode surrogate")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("numbers must be finite")
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                _check(key, depth + 1)
            _check(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check(item, depth + 1)
    return value


def safe_text(value: Any) -> str:
    """A response-safe string, with unencodable surrogates replaced."""
    return "".join(
        "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in str(value)
    )


class SafeModel(BaseModel):
    """A request model that refuses values the database or JSON encoder cannot hold."""

    @model_validator(mode="before")
    @classmethod
    def _validate_boundary_values(cls, data: Any) -> Any:
        return _check(data)
