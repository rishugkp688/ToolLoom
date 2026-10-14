from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models import Setting
from backend.models.setting import GLOBAL_SETTING_ID
from backend.utils import utcnow


class SettingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self) -> Setting:
        setting = self.session.get(Setting, GLOBAL_SETTING_ID)
        if setting is None:
            setting = Setting(id=GLOBAL_SETTING_ID, system_prompt="")
            self.session.add(setting)
            self.session.flush()
        return setting

    def get_system_prompt(self) -> str:
        try:
            return self.get().system_prompt or ""
        except Exception:  # fail-open: a bad settings row must not take chat offline
            return ""

    def set_system_prompt(self, prompt: str) -> Setting:
        setting = self.get()
        setting.system_prompt = prompt or ""
        setting.updated_at = utcnow()
        self.session.flush()
        return setting
