from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Secret
from backend.utils import utcnow


class SecretRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[Secret]:
        return list(self.session.execute(select(Secret).order_by(Secret.name)).scalars().all())

    def get_by_name(self, name: str) -> Secret | None:
        stmt = select(Secret).where(Secret.name == name.lower())
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert(self, *, name: str, encrypted_value: str, description: str = "") -> Secret:
        secret = self.get_by_name(name)
        if secret is None:
            secret = Secret(
                name=name.lower(), encrypted_value=encrypted_value, description=description
            )
            self.session.add(secret)
        else:
            secret.encrypted_value = encrypted_value
            if description:
                secret.description = description
            secret.updated_at = utcnow()
        self.session.flush()
        return secret

    def delete_by_name(self, name: str) -> bool:
        secret = self.get_by_name(name)
        if secret is None:
            return False
        self.session.delete(secret)
        return True
