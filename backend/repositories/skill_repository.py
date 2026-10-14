from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Skill
from backend.utils import utcnow


class SkillRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[Skill]:
        return list(self.session.execute(select(Skill).order_by(Skill.name)).scalars().all())

    def list_enabled(self) -> list[Skill]:
        stmt = select(Skill).where(Skill.enabled.is_(True)).order_by(Skill.name)
        return list(self.session.execute(stmt).scalars().all())

    def get(self, skill_id: str) -> Skill | None:
        return self.session.get(Skill, skill_id)

    def get_by_name(self, name: str) -> Skill | None:
        return self.session.execute(select(Skill).where(Skill.name == name)).scalar_one_or_none()

    def set_enabled(self, skill_id: str, enabled: bool) -> Skill | None:
        skill = self.get(skill_id)
        if skill is None:
            return None
        skill.enabled = enabled
        skill.updated_at = utcnow()
        self.session.flush()
        return skill

    def upsert_from_disk(
        self, *, name: str, description: str, fs_path: str, auto_include: bool, hidden: bool
    ) -> Skill:
        skill = self.get_by_name(name)
        if skill is None:
            skill = Skill(
                name=name,
                description=description,
                fs_path=fs_path,
                auto_include=auto_include,
                hidden_from_model=hidden,
                enabled=False,
            )
            self.session.add(skill)
        else:
            skill.description = description
            skill.fs_path = fs_path
            skill.auto_include = auto_include
            skill.hidden_from_model = hidden
            skill.updated_at = utcnow()
        self.session.flush()
        return skill

    def prune_missing(self, names: set[str]) -> int:
        removed = 0
        for skill in self.list():
            if skill.name not in names:
                self.session.delete(skill)
                removed += 1
        return removed

    def delete(self, skill_id: str) -> bool:
        skill = self.get(skill_id)
        if skill is None:
            return False
        self.session.delete(skill)
        return True
