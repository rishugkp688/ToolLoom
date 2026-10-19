"""Bounded retries for explicitly selected, self-contained DB transactions.

Never decorate a function that dispatches tools, performs filesystem mutations,
or retains a Session across calls. Each failed attempt must rollback/close before
the next attempt begins. SQLite BUSY during COMMIT is rolled back by Database.session.
"""

import sqlite3
import time
from functools import wraps

from sqlalchemy.exc import OperationalError


def is_sqlite_busy(exc):
    return (isinstance(exc, OperationalError)
            and isinstance(exc.orig, sqlite3.OperationalError)
            and getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
            in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def retry_sqlite_transaction(function):
    @wraps(function)
    def call(*args, **kwargs):
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                if not is_sqlite_busy(exc) or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
    return call
