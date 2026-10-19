"""The conversation coordinator: CRUD plus user-message persistence."""

from __future__ import annotations

import json

from backend.repositories import ConversationRepository, MessageRepository
from backend.services.providers.base import TextBlock, blocks_to_dicts
from backend.services.uploads import legacy_attachment_presentation


def title_from_message(text: str, limit: int = 60) -> str:
    cleaned = " ".join(text.strip().split())
    if len(cleaned) <= limit:
        return cleaned or "New conversation"
    return cleaned[: limit - 1].rstrip() + "…"


def _summary(session, conversation, message_count: int | None = None) -> dict:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "title_source": conversation.title_source,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "message_count": (
            MessageRepository(session).count(conversation.id)
            if message_count is None
            else message_count
        ),
    }


def _presentation(row) -> dict:
    if row.role != "user":
        return {}
    if row.user_text is not None:
        return {"user_text": row.user_text, "attachments": json.loads(row.attachments_json or "[]")}
    text, files = legacy_attachment_presentation(row.content)
    return {"user_text": text, "attachments": files}


class ConversationService:
    def __init__(self, db, settings) -> None:
        self.db = db
        self.settings = settings

    def create(self, title: str | None = None) -> dict:
        with self.db.session() as session:
            conversation = ConversationRepository(session).create(title)
            return _summary(session, conversation)

    def list(self) -> list[dict]:
        with self.db.session() as session:
            conversations = ConversationRepository(session).list()
            counts = MessageRepository(session).counts_by_conversation()
            return [_summary(session, c, counts.get(c.id, 0)) for c in conversations]

    def meta(self, conversation_id: str) -> dict | None:
        """Header metadata only: no transcript, so polling it stays cheap."""
        with self.db.session() as session:
            conversation = ConversationRepository(session).get(conversation_id)
            if conversation is None:
                return None
            return _summary(session, conversation)

    def get(self, conversation_id: str) -> dict | None:
        with self.db.session() as session:
            conversation = ConversationRepository(session).get(conversation_id)
            if conversation is None:
                return None
            message_repo = MessageRepository(session)
            detail = _summary(session, conversation)
            detail["messages"] = [
                {
                    "id": row.id,
                    "role": row.role,
                    "content": row.content,
                    "blocks": message_repo.blocks(row),
                    "tool_name": row.tool_name,
                    "tool_call_id": row.tool_call_id,
                    "created_at": row.created_at,
                    "stop_reason": row.stop_reason,
                    **_presentation(row),
                }
                for row in message_repo.list_by_conversation(conversation_id)
            ]
            return detail

    def delete(self, conversation_id: str) -> bool:
        with self.db.session() as session:
            return ConversationRepository(session).delete(conversation_id)

    def add_user_message(
        self,
        conversation_id: str,
        text: str,
        *,
        is_resume: bool = False,
        user_text: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict | None:
        with self.db.session() as session:
            repo = ConversationRepository(session)
            conversation = repo.get(conversation_id)
            if conversation is None:
                return None
            title_text = user_text if user_text is not None else text
            if not title_text.strip() and attachments:
                title_text = attachments[0]["name"]
            if not conversation.title and title_text:
                conversation.title = title_from_message(title_text)
            MessageRepository(session).add(
                conversation_id=conversation_id,
                role="user",
                content=text,
                blocks=blocks_to_dicts([TextBlock(text=text)]),
                is_resume=is_resume,
                user_text=user_text,
                attachments=attachments,
            )
            repo.touch(conversation)
            return {"id": conversation.id, "title": conversation.title}
