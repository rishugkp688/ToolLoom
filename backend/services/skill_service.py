"""Skill installation, rescan and enable/disable."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from threading import RLock

from backend.repositories import SkillRepository
from backend.services.skills import (
    SkillError,
    install_archive,
    install_markdown,
    sync_skills_to_db,
)
from backend.services.skills.loader import parse_skill_md


class SkillService:
    def __init__(self, db, settings) -> None:
        self.db = db
        self.settings = settings
        self.root = Path(settings.data_dir) / "skills"
        self._writes = RLock()

    def seed(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db.session() as session:
            sync_skills_to_db(session, self.root)

    def list(self) -> list:
        with self.db.session() as session:
            return SkillRepository(session).list()

    def rescan(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._writes, self.db.session() as session:
            return sync_skills_to_db(session, self.root)

    def set_enabled(self, skill_id: str, enabled: bool):
        with self._writes, self.db.session() as session:
            return SkillRepository(session).set_enabled(skill_id, enabled)

    def delete(self, skill_id: str) -> bool:
        with self._writes, self.db.session() as session:
            repo = SkillRepository(session)
            skill = repo.get(skill_id)
            if skill is None:
                return False
            folder = Path(skill.fs_path).resolve()
            root = self.root.resolve()
            if folder == root or root not in folder.parents:
                raise SkillError("refusing to delete a folder outside the skills root")
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
            repo.delete(skill_id)
            return True

    def install(self, filename: str, data: bytes) -> dict:
        with self._writes:
            return self._install(filename, data)

    def _install(self, filename: str, data: bytes) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        lower = (filename or "").lower()
        with tempfile.TemporaryDirectory(dir=self.root.parent, prefix="skill-stage-") as temp:
            stage = Path(temp) / "new"
            stage.mkdir()
            try:
                if lower.endswith(".zip"):
                    folders = install_archive(stage, filename, data)
                elif lower.endswith((".md", ".markdown")):
                    folders = install_markdown(stage, filename, data)
                else:
                    raise SkillError("upload must be a .zip, .md or .markdown file")
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                raise SkillError("invalid or unreadable skill bundle") from exc
            names = [parse_skill_md(stage / folder / "SKILL.md").name for folder in folders]
            if len(set(names)) != len(names):
                raise SkillError("duplicate skill names in bundle")
            for folder in folders:
                if (self.root / folder).is_symlink():
                    raise SkillError("skill destination must not be a symlink")
            replaced = []
            try:
                for folder in folders:
                    target = self.root / folder
                    backup = Path(temp) / (folder + ".backup")
                    if target.exists():
                        target.rename(backup)
                    replaced.append((target, backup))
                    (stage / folder).rename(target)
                result = self.rescan()
                with self.db.session() as session:
                    for skill in SkillRepository(session).list():
                        if skill.name in names:
                            skill.enabled = False
            except Exception:
                for target, backup in reversed(replaced):
                    if target.exists():
                        shutil.rmtree(target)
                    if backup.exists():
                        backup.rename(target)
                raise
        return {**result, "installed": len(names)}
