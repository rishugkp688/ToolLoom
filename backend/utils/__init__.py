"""Small pure helpers shared across the backend."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite stores naive datetimes consistently)."""
    return datetime.now(UTC).replace(tzinfo=None)
