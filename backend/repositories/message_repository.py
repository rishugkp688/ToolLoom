from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from backend.models import Message


class MessageTail(list):
    """A bounded chronological suffix, including what the read could not retain."""

    def __init__(self, rows=(), *, omitted_rows: int = 0):
        super().__init__(rows)
        self.omitted_rows = omitted_rows


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

    def list_from(self, conversation_id: str, offset: int = 0) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
            .offset(max(0, offset))
        )
        return list(self.session.execute(stmt).scalars().all())

    def page(self, conversation_id: str, *, limit: int = 100, before: str | None = None):
        """Keyset page in chronological order; never separate calls from results.

        Limits apply to complete exchanges. One oversized exchange can exceed
        them so the audit view stays truthful; ordinary pages target 1 MiB.
        """
        base = select(Message).where(Message.conversation_id == conversation_id)
        if before is not None:
            boundary = self.session.get(Message, before)
            if boundary is None or boundary.conversation_id != conversation_id:
                raise ValueError("invalid message cursor for this conversation")
            base = base.where(
                tuple_(Message.created_at, Message.id) < tuple_(boundary.created_at, boundary.id)
            )
        newest = base.order_by(Message.created_at.desc(), Message.id.desc())
        rows = list(self.session.scalars(newest.limit(limit + 1)))
        more = len(rows) > limit
        rows = rows[:limit]
        # Extend backwards to include the call for a leading group of results.
        while rows and rows[-1].role == "tool":
            row = self.session.scalars(newest.offset(len(rows)).limit(1)).first()
            if row is None:
                more = False
                break
            rows.append(row)
            more = True
        # Bound normal payloads without throwing away half an exchange.
        size = 0
        boundary_index = None
        for index, row in enumerate(rows):
            size += len(row.content or "") + len(row.content_blocks_json or "")
            if row.role != "tool":
                if size > 1_000_000:
                    boundary_index = index + 1
                    break
        if boundary_index is not None and boundary_index < len(rows):
            rows = rows[:boundary_index]
            more = True
        if rows:
            oldest = rows[-1]
            more = self.session.execute(
                select(Message.id).where(
                    Message.conversation_id == conversation_id,
                    tuple_(Message.created_at, Message.id) < tuple_(oldest.created_at, oldest.id),
                ).limit(1)
            ).first() is not None
        rows.reverse()
        return rows, more

    def list_tail(
        self, conversation_id: str, *, offset: int = 0, max_chars: int = 60_000
    ) -> list[Message]:
        """The newest rows that fit a character bound, in chronological order.

        Building a prompt does not need the whole audit log: only the active suffix
        can influence what is sent. Reading newest-first keeps the cost tied to the
        working set rather than to how long the conversation has lived.
        """
        offset = max(0, offset)
        base = select(Message).where(Message.conversation_id == conversation_id)
        # The summary cursor counts from the oldest end, never from the newest.
        if offset:
            boundary = self.session.execute(
                select(Message.created_at, Message.id)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at, Message.id)
                .offset(offset)
                .limit(1)
            ).first()
            if boundary is None:
                return MessageTail()
            base = base.where(tuple_(Message.created_at, Message.id) >= tuple_(*boundary))
        total_rows = max(0, self.count(conversation_id) - offset)
        kept: list[Message] = []
        total = 0
        position = 0
        batch_size = 64
        exhausted = False
        while not exhausted:
            stmt = (
                base.order_by(Message.created_at.desc(), Message.id.desc())
                .offset(position)
                .limit(batch_size)
            )
            batch = list(self.session.execute(stmt).scalars().all())
            if not batch:
                break
            for row in batch:
                size = len(row.content or "") + len(row.content_blocks_json or "")
                if kept and total + size > max_chars:
                    exhausted = True
                    break
                kept.append(row)
                total += size
                position += 1
            if len(batch) < batch_size:
                break

        # Never open the active suffix on a tool result whose call was cut off.
        while kept and kept[-1].role == "tool":
            stmt = (
                base.order_by(Message.created_at.desc(), Message.id.desc())
                .offset(position)
                .limit(1)
            )
            older = self.session.execute(stmt).scalars().first()
            if older is None:
                break
            kept.append(older)
            position += 1
        kept.reverse()
        return MessageTail(kept, omitted_rows=max(0, total_rows - len(kept)))

    def first_user(self, conversation_id: str) -> Message | None:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role == "user")
            .order_by(Message.created_at, Message.id)
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def first_assistant(
        self, conversation_id: str, *, end_turn_only: bool = False
    ) -> Message | None:
        stmt = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.role == "assistant",
            Message.content != "",
        )
        if end_turn_only:
            stmt = stmt.where(Message.stop_reason == "end_turn")
        stmt = stmt.order_by(Message.created_at, Message.id).limit(1)
        return self.session.execute(stmt).scalars().first()

    def latest_user(self, conversation_id: str, *, include_resume: bool = False) -> Message | None:
        stmt = select(Message).where(
            Message.conversation_id == conversation_id, Message.role == "user"
        )
        if not include_resume:
            stmt = stmt.where(Message.is_resume.is_(False))
        stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(1)
        return self.session.execute(stmt).scalars().first()

    def counts_by_conversation(self) -> dict[str, int]:
        stmt = select(Message.conversation_id, func.count()).group_by(Message.conversation_id)
        return {row[0]: int(row[1]) for row in self.session.execute(stmt).all()}

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
