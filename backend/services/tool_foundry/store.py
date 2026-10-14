"""Where authoring happens and where installed capabilities live.

Two locations, on purpose:

* ``<workspace>/foundry/<name>/`` is where the agent writes and tests a package.
  The agent can freely rewrite it, which is why nothing runs from here.
* ``<data_dir>/tools/<name>/<version>/`` is where an approved package is copied.
  It sits outside the conversation workspace, so a later run cannot edit a tool
  the operator already reviewed. The integrity hash only means something because
  of that separation.

One version per name is active at a time. Installing again replaces the previous
version; the version string is recorded so a reinstall is visible in the UI.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backend.services.tool_foundry.manifest import (
    MANIFEST_FILENAME,
    FoundryError,
    FoundryManifest,
    load_manifest,
)

AUTHORING_DIRNAME = "foundry"
INSTALLED_DIRNAME = "tools"
INTEGRITY_FILENAME = ".integrity.json"

_SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git", ".integrity.json")


def authoring_root(workspace) -> Path:
    return Path(workspace.root) / AUTHORING_DIRNAME


def authoring_package(workspace, name: str) -> Path:
    return authoring_root(workspace) / name


def installed_root(settings) -> Path:
    root = Path(settings.data_dir) / INSTALLED_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def installed_package(settings, name: str, version: str) -> Path:
    return installed_root(settings) / name / version


@dataclass
class InstalledPackage:
    name: str
    version: str
    directory: Path
    manifest: FoundryManifest
    entrypoint: Path
    # The hash recorded at install time. This is the approved hash.
    sha256: str
    # The hash of the file as it is right now.
    actual_sha256: str
    installed_at: str | None
    source_conversation: str | None
    tests_passed: int | None
    integrity_ok: bool

    @property
    def tool_name(self) -> str:
        return f"generated__{self.name}"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_integrity(directory: Path) -> dict:
    path = directory / INTEGRITY_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _load(directory: Path) -> InstalledPackage:
    manifest = load_manifest(directory)
    entrypoint = (directory / manifest.entrypoint).resolve()
    integrity = _read_integrity(directory)
    recorded = str(integrity.get("sha256") or "")
    actual = _hash_file(entrypoint) if entrypoint.is_file() else ""
    return InstalledPackage(
        name=manifest.name,
        version=manifest.version,
        directory=directory,
        manifest=manifest,
        entrypoint=entrypoint,
        sha256=recorded,
        actual_sha256=actual,
        installed_at=integrity.get("installed_at"),
        source_conversation=integrity.get("source_conversation"),
        tests_passed=integrity.get("tests_passed"),
        integrity_ok=bool(recorded) and recorded == actual,
    )


def install_package(
    settings,
    source_dir: Path,
    manifest: FoundryManifest,
    *,
    source_conversation: str | None = None,
    tests_passed: int | None = None,
) -> InstalledPackage:
    """Copy a validated package into the installed store and hash its entrypoint."""
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise FoundryError(f"package directory not found: {source_dir}")

    root = installed_root(settings)
    target = installed_package(settings, manifest.name, manifest.version)
    # Staging sits beside the name directory, not inside it, because the swap
    # below removes the name directory and would take staging with it.
    staging = root / f".{manifest.name}-{manifest.version}.staging"

    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source_dir, staging, ignore=_SKIP)

    entrypoint = staging / manifest.entrypoint
    if not entrypoint.is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise FoundryError(f"entrypoint {manifest.entrypoint!r} is missing from the package")

    integrity = {
        "sha256": _hash_file(entrypoint),
        "manifest_sha256": _hash_file(staging / MANIFEST_FILENAME),
        "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_conversation": source_conversation,
        "tests_passed": tests_passed,
    }
    (staging / INTEGRITY_FILENAME).write_text(
        json.dumps(integrity, indent=2) + "\n", encoding="utf-8"
    )

    # Replace the whole name directory so only one version is ever active, and
    # swap via rename so a failure cannot leave a half-installed version behind.
    name_dir = (root / manifest.name).resolve()
    if root.resolve() not in name_dir.parents:
        shutil.rmtree(staging, ignore_errors=True)
        raise FoundryError("refusing to install outside the tools directory")
    if name_dir.exists():
        shutil.rmtree(name_dir, ignore_errors=True)
    name_dir.mkdir(parents=True, exist_ok=True)
    staging.rename(target)

    return _load(target)


def list_installed(settings) -> list[InstalledPackage]:
    root = installed_root(settings)
    packages: list[InstalledPackage] = []
    for name_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if name_dir.name.startswith("."):
            continue  # staging leftovers
        for version_dir in sorted(p for p in name_dir.iterdir() if p.is_dir()):
            try:
                packages.append(_load(version_dir))
            except FoundryError:
                # A broken package stays visible in the directory listing but is
                # not registered as a tool. Surfacing it in the UI is the point.
                continue
    return packages


def find_installed(settings, name: str) -> InstalledPackage | None:
    for package in list_installed(settings):
        if package.name == name:
            return package
    return None


def verify_integrity(package: InstalledPackage) -> bool:
    """Compare the file against the hash recorded when it was approved."""
    if not package.entrypoint.is_file() or not package.sha256:
        return False
    return _hash_file(package.entrypoint) == package.sha256


def remove_installed(settings, name: str) -> bool:
    root = installed_root(settings).resolve()
    target = (root / name).resolve()
    if root not in target.parents:
        raise FoundryError("refusing to remove outside the tools directory")
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def read_package_files(package: InstalledPackage, *, limit: int = 100_000) -> dict[str, str]:
    """Read-only view of a package, for reviewing it before enabling it."""
    files: dict[str, str] = {}
    candidates: list[tuple[str, Path]] = [
        ("manifest.json", package.directory / MANIFEST_FILENAME),
        (package.manifest.entrypoint, package.entrypoint),
    ]
    tests = package.directory / package.manifest.tests
    if tests.is_dir():
        candidates.extend(
            (f"{package.manifest.tests}/{path.name}", path)
            for path in sorted(tests.glob("test_*.py"))
        )
    for label, path in candidates:
        if not path.is_file():
            continue
        try:
            files[label] = path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            continue
    return files
