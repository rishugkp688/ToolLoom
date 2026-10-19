from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert
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
            # Another connection may have read the same empty database. Do not
            # rollback the caller's transaction to recover a singleton collision.
            self.session.execute(
                insert(Setting).values(id=GLOBAL_SETTING_ID, system_prompt="")
                .on_conflict_do_nothing(index_elements=[Setting.id])
            )
            setting = self.session.get(Setting, GLOBAL_SETTING_ID)
            assert setting is not None
        return setting

    def get_system_prompt(self) -> str:
        return self.get().system_prompt or ""

    def set_system_prompt(self, prompt: str) -> Setting:
        setting = self.get()
        setting.system_prompt = prompt or ""
        setting.updated_at = utcnow()
        self.session.flush()
        return setting
