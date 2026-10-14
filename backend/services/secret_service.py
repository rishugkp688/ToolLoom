"""Secrets CRUD that never returns plaintext values."""

from __future__ import annotations

from backend.repositories import SecretRepository
from backend.services.secrets import SecretsError, Vault, preview, validate_secret_name


class SecretService:
    def __init__(self, db, vault: Vault) -> None:
        self.db = db
        self.vault = vault

    def list(self) -> list[dict]:
        with self.db.session() as session:
            rows = SecretRepository(session).list()
            out = []
            for row in rows:
                try:
                    hint = preview(self.vault.decrypt(row.encrypted_value))
                except SecretsError:
                    hint = "(undecryptable)"
                out.append(
                    {
                        "id": row.id,
                        "name": row.name,
                        "description": row.description,
                        "preview": hint,
                        "updated_at": row.updated_at,
                    }
                )
            return out

    def upsert(self, name: str, value: str, description: str = "") -> dict:
        validate_secret_name(name)
        if not value:
            raise SecretsError("secret value must not be empty")
        encrypted = self.vault.encrypt(value)
        with self.db.session() as session:
            row = SecretRepository(session).upsert(
                name=name, encrypted_value=encrypted, description=description
            )
            return {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "preview": preview(value),
                "updated_at": row.updated_at,
            }

    def delete(self, name: str) -> bool:
        with self.db.session() as session:
            return SecretRepository(session).delete_by_name(name)
