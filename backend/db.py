"""SQLAlchemy engine, session factory and SQLite pragmas.

The :class:`Database` object is created once per application instance (see
``create_app``) and stored on ``app.state.db``. Streaming endpoints open their
own short-lived sessions from it, which keeps DB sessions independent of the
HTTP request lifetime.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        connect_args: dict[str, object] = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _set_sqlite_pragmas)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )

    def create_all(self) -> None:
        # Importing the models package registers all tables on Base.metadata.
        import backend.models  # noqa: F401

        Base.metadata.create_all(self.engine)
        self._apply_lightweight_migrations()

    def _apply_lightweight_migrations(self) -> None:
        """Add columns introduced after a database was first created.

        ``create_all`` only creates missing *tables*, not missing columns, so an
        existing ``.local/toolloom.db`` needs explicit ALTERs. Idempotent: each
        column is inspected before it is added.
        """
        from sqlalchemy import inspect, text

        additions: dict[str, dict[str, str]] = {
            "settings": {"unlimited_tools": "BOOLEAN NOT NULL DEFAULT 0"},
            "conversations": {
                "title_source": "VARCHAR(16) DEFAULT 'legacy'",
                "title_usage_input": "INTEGER DEFAULT 0",
                "title_usage_output": "INTEGER DEFAULT 0",
                "title_attempts": "INTEGER NOT NULL DEFAULT 0",
                "title_last_attempt": "DATETIME",
                "title_error": "VARCHAR(32)",
            },
            "mcp_servers": {
                "command": "VARCHAR(512)",
                "args_json": "TEXT DEFAULT '[]'",
                "env_json": "TEXT DEFAULT '{}'",
                "cwd": "VARCHAR(1024)",
            },
            "tools": {
                "risk": "VARCHAR(16) DEFAULT 'low'",
                "approved": "BOOLEAN DEFAULT 0",
            },
            "messages": {
                "user_text": "TEXT",
                "attachments_json": "TEXT DEFAULT '[]'",
                "usage_input": "INTEGER DEFAULT 0",
                "usage_output": "INTEGER DEFAULT 0",
                "stop_reason": "VARCHAR(32)",
            },
        }
        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names())
        with self.engine.begin() as connection:
            for table, columns in additions.items():
                if table not in tables:
                    continue
                present = {column["name"] for column in inspector.get_columns(table)}
                for name, ddl in columns.items():
                    if name not in present:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    def dispose(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Commit-on-exit session context manager."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
