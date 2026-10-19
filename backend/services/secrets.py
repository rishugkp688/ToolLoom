"""Local secrets vault (Fernet) with ``${secrets.NAME}`` interpolation.

The plaintext value is never returned by any API, serialized into a response, or
logged. Decryption failures surface as an actionable message rather than
gibberish.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

_REF_RE = re.compile(r"\$\{secrets\.([A-Za-z0-9_]{1,64})\}")
_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


class SecretsError(RuntimeError):
    """Raised for vault/reference failures."""


def validate_secret_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise SecretsError("secret name must match [a-z0-9_]{1,64}")
    return name


def load_or_generate_key(key_path: Path, env_key: str | None) -> bytes:
    """Resolve the Fernet key from the env or a git-ignored key file."""
    if env_key:
        return env_key.strip().encode("utf-8")
    key_path = Path(key_path)
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return key


class Vault:
    def __init__(self, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise SecretsError(
                "invalid vault key: set TOOLLOOM_SECRETS_KEY to a valid Fernet key, "
                "or delete the key file so a new one is generated"
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretsError(
                "cannot decrypt secret — the vault key has changed since it was stored"
            ) from exc


def preview(value: str) -> str:
    if len(value) < 12:
        return "•••"
    return f"{value[:4]}…{value[-3:]}"


def find_refs(text: str) -> set[str]:
    return {match.group(1).lower() for match in _REF_RE.finditer(text or "")}


class SecretResolver(Protocol):
    def lookup(self, name: str) -> str | None: ...
    def resolve_string(self, text: str) -> str: ...
    def resolve_mapping(self, mapping: dict[str, str]) -> dict[str, str]: ...


class DictResolver:
    """Test helper / static resolver."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = {k.lower(): v for k, v in values.items()}

    def lookup(self, name: str) -> str | None:
        return self.values.get(name.lower())

    def resolve_string(self, text: str) -> str:
        return _substitute(text, self.lookup)

    def resolve_mapping(self, mapping: dict[str, str]) -> dict[str, str]:
        return {key: _substitute(value, self.lookup) for key, value in (mapping or {}).items()}


class DbResolver:
    def __init__(self, db, vault: Vault) -> None:
        self.db = db
        self.vault = vault

    def lookup(self, name: str) -> str | None:
        from backend.repositories import SecretRepository

        with self.db.session() as session:
            secret = SecretRepository(session).get_by_name(name)
            if secret is None:
                return None
            try:
                return self.vault.decrypt(secret.encrypted_value)
            except SecretsError:
                return None

    def resolve_string(self, text: str) -> str:
        return _substitute(text, self.lookup)

    def redaction_values(self) -> tuple[str, ...]:
        """Backend-only snapshot for streaming redaction; never a tool argument."""
        from backend.repositories import SecretRepository

        with self.db.session() as session:
            values = []
            for secret in SecretRepository(session).list():
                try:
                    values.append(self.vault.decrypt(secret.encrypted_value))
                except SecretsError:
                    continue
        return tuple(values)

    def redact(self, text: str) -> str:
        for value in sorted(self.redaction_values(), key=len, reverse=True):
            if value:
                text = text.replace(value, "[REDACTED]")
        return text

    def resolve_mapping(self, mapping: dict[str, str]) -> dict[str, str]:
        return {key: _substitute(value, self.lookup) for key, value in (mapping or {}).items()}


def _substitute(text: str, lookup) -> str:
    def replace(match: re.Match[str]) -> str:
        value = lookup(match.group(1))
        if value is None:
            raise SecretsError(f"secret {match.group(1)!r} is not defined")
        return value

    return _REF_RE.sub(replace, text or "")


def is_single_reference(value: str) -> bool:
    """True when a header value is exactly one ``${secrets.NAME}`` reference."""
    return bool(_REF_RE.fullmatch((value or "").strip()))


__all__ = [
    "SecretsError",
    "Vault",
    "DictResolver",
    "DbResolver",
    "SecretResolver",
    "load_or_generate_key",
    "validate_secret_name",
    "preview",
    "find_refs",
    "is_single_reference",
]
