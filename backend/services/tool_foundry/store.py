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
    # The entrypoint hash, shown in the review view.
    sha256: str
    # The hash of the file as it is right now.
    actual_sha256: str
    # A digest over every regular file in the package. This is what approval is
    # bound to, because the entrypoint alone does not describe what a package does:
    # an imported helper, a resource file or the manifest can change behaviour
    # without touching the file the operator reviewed.
    digest: str
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


def package_digest(directory: Path) -> str:
    """A canonical digest over every regular file in a package.

    Covers helpers, resources and the manifest, not just the entrypoint, because
    those change what a tool does without changing the file that was reviewed.
    Symlinks are refused rather than followed so a link cannot point the digest at
    content outside the package, and the integrity record itself is excluded.
    """
    root = Path(directory)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == INTEGRITY_FILENAME or relative.startswith("__pycache__/"):
            continue
        if path.is_symlink():
            raise FoundryError(f"refusing to digest a symlink: {relative}")
        if not path.is_file() or relative.endswith(".pyc"):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_hash_file(path).encode("ascii"))
        digest.update(b"\n")
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
    recorded_entry = str(integrity.get("sha256") or "")
    recorded_digest = str(integrity.get("digest") or "")
    actual_entry = _hash_file(entrypoint) if entrypoint.is_file() else ""
    try:
        actual_digest = package_digest(directory)
    except FoundryError:
        actual_digest = ""
    if recorded_digest:
        integrity_ok = recorded_digest == actual_digest and bool(actual_digest)
    else:
        # Packages installed before package digests existed can only be checked
        # against their entrypoint. They are reported as they are, not re-blessed.
        integrity_ok = bool(recorded_entry) and recorded_entry == actual_entry
    return InstalledPackage(
        name=manifest.name,
        version=manifest.version,
        directory=directory,
        manifest=manifest,
        entrypoint=entrypoint,
        sha256=recorded_entry,
        actual_sha256=actual_entry,
        digest=recorded_digest,
        installed_at=integrity.get("installed_at"),
        source_conversation=integrity.get("source_conversation"),
        tests_passed=integrity.get("tests_passed"),
        integrity_ok=integrity_ok,
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
        # The manifest is inside this digest, so a separate manifest hash would be
        # a value nothing checks. One digest, checked in one place.
        "digest": package_digest(staging),
        "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_conversation": source_conversation,
        "tests_passed": tests_passed,
    }
    (staging / INTEGRITY_FILENAME).write_text(
        json.dumps(integrity, indent=2) + "\n", encoding="utf-8"
    )

    # Publish the new version first, then retire superseded ones. The previous
    # versions are parked rather than deleted, so a failure while publishing restores
    # what was working instead of leaving the capability gone.
    name_dir = (root / manifest.name).resolve()
    if root.resolve() not in name_dir.parents:
        shutil.rmtree(staging, ignore_errors=True)
        raise FoundryError("refusing to install outside the tools directory")
    name_dir.mkdir(parents=True, exist_ok=True)

    retired = name_dir / ".previous"
    if retired.exists():
        shutil.rmtree(retired, ignore_errors=True)
    superseded = [path for path in name_dir.iterdir() if path != retired]
    if superseded:
        retired.mkdir()
        for old in superseded:
            old.rename(retired / old.name)

    try:
        staging.rename(target)
        published = _load(target)
        if not published.integrity_ok:
            raise FoundryError("the published package failed its integrity check")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        if retired.exists():
            for old in retired.iterdir():
                old.rename(name_dir / old.name)
            retired.rmdir()
        shutil.rmtree(staging, ignore_errors=True)
        raise

    shutil.rmtree(retired, ignore_errors=True)
    return published


def list_installed(settings) -> list[InstalledPackage]:
    root = installed_root(settings)
    packages: list[InstalledPackage] = []
    for name_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if name_dir.name.startswith("."):
            continue  # staging leftovers
        for version_dir in sorted(p for p in name_dir.iterdir() if p.is_dir()):
            if version_dir.name.startswith("."):
                continue  # `.previous` while an install is in flight
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
    """Compare the package against the digest recorded when it was installed.

    Checks every file, so a changed helper or manifest fails as loudly as a changed
    entrypoint. Falls back to the entrypoint hash only for packages installed
    before package digests existed.
    """
    if package.digest:
        try:
            return package_digest(package.directory) == package.digest
        except FoundryError:
            return False
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
    """Read-only view of a package, for reviewing it before enabling it.

    Every readable file is returned, not just the entrypoint and tests: an
    imported helper is part of what the operator is approving.
    """
    root = package.directory
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == INTEGRITY_FILENAME or relative.startswith("__pycache__/"):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            files[relative] = path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            continue
    return files
