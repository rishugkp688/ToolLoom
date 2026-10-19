"""The manifest that describes a generated tool.

It is a proposal, not a contract the runtime enforces. ``permissions`` is
recorded so the operator can review what the tool claims to need; the executor
does not act on it. See the package docstring for why.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MANIFEST_FILENAME = "manifest.json"

# Lowercase start, then lowercase, digits and underscores. Long enough to be
# descriptive, short enough to read in a tool list.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
MAX_DESCRIPTION = 500


class FoundryError(ValueError):
    """A package or manifest problem, reported to the model with the reason."""


class Permissions(BaseModel):
    """What the tool claims it needs. Displayed for review; not enforced."""

    model_config = ConfigDict(extra="forbid")

    filesystem: Literal["none", "workspace"] = "workspace"
    network: bool = False


class FoundryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    version: str = "1.0.0"
    description: str
    runtime: Literal["python"] = "python"
    # Existing installed CLIs keep their flag contract. New scaffolds use an
    # exact JSON envelope so empty collections/null/booleans are not lossy.
    argument_encoding: Literal["flags", "json"] = "flags"
    # Relative to the package root, must stay inside it, must be a .py file.
    entrypoint: str
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    permissions: Permissions = Field(default_factory=Permissions)
    # Directory (relative to the package root) holding unittest files.
    tests: str = "tests"
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not NAME_PATTERN.match(value or ""):
            raise ValueError(
                "name must start with a letter and use lowercase letters, digits and "
                "underscores, 2 to 48 characters"
            )
        return value

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not VERSION_PATTERN.match(value or ""):
            raise ValueError("version must look like 1.0.0")
        return value

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("description is required")
        if len(text) > MAX_DESCRIPTION:
            raise ValueError(f"description must be at most {MAX_DESCRIPTION} characters")
        return text

    @field_validator("entrypoint", "tests")
    @classmethod
    def _check_relative(cls, value: str, info) -> str:
        raw = (value or "").strip()
        if not raw:
            raise ValueError(f"{info.field_name} is required")
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"{info.field_name} must be a relative path inside the package")
        if info.field_name == "entrypoint" and candidate.suffix != ".py":
            raise ValueError("entrypoint must be a .py file")
        return raw

    @field_validator("input_schema")
    @classmethod
    def _check_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("inputSchema must be an object")
        if value.get("type") != "object":
            raise ValueError('inputSchema must have "type": "object"')
        properties = value.get("properties")
        if not isinstance(properties, dict):
            raise ValueError('inputSchema must define a "properties" object')
        required = value.get("required", [])
        if not isinstance(required, list) or any(not isinstance(k, str) for k in required):
            raise ValueError('inputSchema "required" must be a list of strings')
        unknown = [key for key in required if key not in properties]
        if unknown:
            raise ValueError(f"required name(s) missing from properties: {', '.join(unknown)}")
        return value


def _explain(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(manifest)"
        problems.append(f"{location}: {error['msg']}")
    return "; ".join(problems)


def load_manifest(package_dir: Path) -> FoundryManifest:
    path = Path(package_dir) / MANIFEST_FILENAME
    if not path.is_file():
        raise FoundryError(f"{MANIFEST_FILENAME} not found in {package_dir}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FoundryError(f"{MANIFEST_FILENAME} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise FoundryError(f"{MANIFEST_FILENAME} must contain a JSON object")
    try:
        return FoundryManifest.model_validate(raw)
    except ValidationError as exc:
        raise FoundryError(f"invalid manifest: {_explain(exc)}") from exc


def save_manifest(package_dir: Path, manifest: FoundryManifest) -> Path:
    path = Path(package_dir) / MANIFEST_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.model_dump(by_alias=True, exclude_none=True), indent=2) + "\n",
        encoding="utf-8",
    )
    return path
