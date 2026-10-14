"""SKILL.md discovery, validation, and installation.

A directory containing a ``SKILL.md`` is a skill root. Frontmatter is a small
YAML-ish block; we parse it by hand to avoid a PyYAML dependency.
"""

from __future__ import annotations

import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DRIVE = re.compile(r"^[A-Za-z]:")
MAX_SKILL_NAME = 64
MAX_DESCRIPTION = 1024
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024


class SkillError(ValueError):
    """Invalid skill definition or archive."""


@dataclass
class LoadedSkill:
    name: str
    description: str
    fs_path: str
    auto_include: bool = False
    hidden_from_model: bool = False
    warnings: list[str] = field(default_factory=list)


def _as_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"true", "yes", "1", "on"}


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return meta, "\n".join(lines[index + 1 :])
        line = lines[index]
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta, text


def validate_name(name: str) -> str:
    if not name or len(name) > MAX_SKILL_NAME or not _NAME_RE.match(name):
        raise SkillError(
            "skill name must be 1-64 chars of lowercase letters, digits and single hyphens"
        )
    return name


def parse_skill_md(path: Path) -> LoadedSkill:
    if path.stat().st_size > 512 * 1024:
        raise SkillError("SKILL.md is too large")
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = split_frontmatter(text)
    name = validate_name(meta.get("name") or path.parent.name)
    description = (meta.get("description") or "").strip()
    if not description:
        raise SkillError("SKILL.md requires a `description` in frontmatter")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"description must be <= {MAX_DESCRIPTION} chars")
    return LoadedSkill(
        name=name,
        description=description,
        fs_path=str(path.parent.resolve()),
        auto_include=_as_bool(meta.get("auto-include")),
        hidden_from_model=_as_bool(meta.get("disable-model-invocation")),
    )


def scan_skills(root: Path) -> tuple[list[LoadedSkill], list[str]]:
    root = Path(root)
    skills: list[LoadedSkill] = []
    warnings: list[str] = []
    seen: set[str] = set()
    if not root.is_dir():
        return skills, warnings
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file() or skill_md.is_symlink():
            continue
        try:
            skill = parse_skill_md(skill_md)
        except SkillError as exc:
            warnings.append(f"{child.name}: {exc}")
            continue
        if skill.name in seen:
            warnings.append(f"{child.name}: duplicate skill name {skill.name!r} ignored")
            continue
        seen.add(skill.name)
        skills.append(skill)
    return skills, warnings


def sync_skills_to_db(session, root: Path) -> dict:
    from backend.repositories import SkillRepository

    skills, warnings = scan_skills(root)
    repo = SkillRepository(session)
    for skill in skills:
        repo.upsert_from_disk(
            name=skill.name,
            description=skill.description,
            fs_path=skill.fs_path,
            auto_include=skill.auto_include,
            hidden=skill.hidden_from_model,
        )
    removed = repo.prune_missing({skill.name for skill in skills})
    return {"scanned": len(skills), "removed": removed, "warnings": warnings}


def read_skill_body(skill_path: str, file: str | None = None) -> str:
    root = Path(skill_path).resolve()
    target = (root / (file or "SKILL.md")).resolve()
    if target != root and root not in target.parents:
        raise SkillError("file escapes the skill directory")
    if not target.is_file():
        raise SkillError(f"no such file in skill: {file}")
    if target.stat().st_size > 512 * 1024:
        raise SkillError("skill file is too large to read")
    return target.read_text(encoding="utf-8", errors="replace")


# --- installation ------------------------------------------------------------

def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    total = 0
    members: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        if len(members) >= 1000:
            raise SkillError("archive exceeds 1000 entries")
        name = info.filename
        if name.startswith(("/", "\\")) or _DRIVE.match(name):
            raise SkillError(f"unsafe archive entry: {name}")
        parts = Path(name.replace("\\", "/")).parts
        if ".." in parts:
            raise SkillError(f"unsafe archive entry: {name}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise SkillError(f"archive contains a symlink: {name}")
        total += info.file_size
        if total > MAX_ARCHIVE_BYTES:
            raise SkillError("archive expands beyond the 25 MB limit")
        members.append(info)
    return members


def _clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def install_archive(skills_root: Path, filename: str, data: bytes) -> list[str]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise SkillError("archive is larger than the 25 MB limit")
    skills_root.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem or "skill"
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = _safe_members(archive)
        names = [info.filename for info in members if info.filename.strip("/")]
        top_skill = any(Path(name).as_posix().rstrip("/") == "SKILL.md" for name in names)
        installed: list[str] = []
        if top_skill:
            folder = _sanitize_folder(stem)
            _extract_into(archive, members, skills_root / folder, strip_prefix="")
            _validate_installed(skills_root / folder)
            installed.append(folder)
        else:
            folders: dict[str, list[zipfile.ZipInfo]] = {}
            for info in members:
                parts = Path(info.filename.replace("\\", "/")).parts
                if not parts:
                    continue
                folders.setdefault(parts[0], []).append(info)
            for top, infos in folders.items():
                if not any(
                    Path(i.filename.replace("\\", "/")).as_posix().endswith("/SKILL.md")
                    or Path(i.filename.replace("\\", "/")).as_posix() == "SKILL.md"
                    for i in infos
                ):
                    continue
                folder = _sanitize_folder(top)
                _extract_into(archive, infos, skills_root / folder, strip_prefix=top)
                _validate_installed(skills_root / folder)
                installed.append(folder)
        if not installed:
            raise SkillError("archive did not contain a SKILL.md")
        return installed


def install_markdown(skills_root: Path, filename: str, data: bytes) -> list[str]:
    text = data.decode("utf-8", "replace")
    meta, _ = split_frontmatter(text)
    stem = Path(filename).stem or "skill"
    name = validate_name((meta.get("name") or stem).strip())
    if not (meta.get("description") or "").strip():
        raise SkillError("SKILL.md requires a `description` in frontmatter")
    folder = skills_root / _sanitize_folder(name)
    _clean_dir(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return [folder.name]


def _sanitize_folder(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name or "skill").strip("._") or "skill"
    return cleaned[:64]


def _extract_into(
    archive: zipfile.ZipFile, members: list[zipfile.ZipInfo], target: Path, *, strip_prefix: str
) -> None:
    _clean_dir(target)
    target.mkdir(parents=True, exist_ok=True)
    for info in members:
        if info.is_dir():
            continue
        rel = Path(info.filename.replace("\\", "/"))
        parts = rel.parts[1:] if strip_prefix else rel.parts
        if not parts:
            continue
        destination = (target / Path(*parts)).resolve()
        if target.resolve() not in destination.parents and destination != target.resolve():
            raise SkillError(f"unsafe archive entry: {info.filename}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(info))


def _validate_installed(folder: Path) -> None:
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        raise SkillError("installed skill is missing SKILL.md")
    parse_skill_md(skill_md)
