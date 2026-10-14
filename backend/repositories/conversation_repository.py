from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Conversation
from backend.utils import utcnow


class ConversationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, title: str | None = None) -> Conversation:
        conv = Conversation(title=title, title_source="manual" if title else "pending")
        self.session.add(conv)
        self.session.flush()
        return conv

    def get(self, conversation_id: str) -> Conversation | None:
        return self.session.get(Conversation, conversation_id)

    def list(self) -> list[Conversation]:
        stmt = select(Conversation).order_by(Conversation.updated_at.desc())
        return list(self.session.execute(stmt).scalars().all())

    def delete(self, conversation_id: str) -> bool:
        conv = self.get(conversation_id)
        if conv is None:
            return False
        self.session.delete(conv)
        return True

    def touch(self, conversation: Conversation) -> None:
        conversation.updated_at = utcnow()
        self.session.flush()
