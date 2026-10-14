from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Message


class MessageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str = "",
        blocks: list[dict[str, Any]] | None = None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        is_resume: bool = False,
        usage_input: int = 0,
        usage_output: int = 0,
        stop_reason: str | None = None,
        user_text: str | None = None,
        attachments: list[dict] | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            user_text=user_text,
            attachments_json=json.dumps(attachments or []),
            content_blocks_json=json.dumps(blocks or []),
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            is_resume=is_resume,
            usage_input=usage_input,
            usage_output=usage_output,
            stop_reason=stop_reason,
        )
        self.session.add(message)
        self.session.flush()
        return message

    def list_by_conversation(self, conversation_id: str) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count(self, conversation_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
        return int(self.session.execute(stmt).scalar_one())

    def usage_by_conversation(self) -> list[tuple[str, int, int]]:
        stmt = select(
            Message.conversation_id,
            func.coalesce(func.sum(Message.usage_input), 0),
            func.coalesce(func.sum(Message.usage_output), 0),
        ).group_by(Message.conversation_id)
        return [(row[0], int(row[1]), int(row[2])) for row in self.session.execute(stmt).all()]

    def blocks(self, message: Message) -> list[dict[str, Any]]:
        try:
            data = json.loads(message.content_blocks_json or "[]")
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []
