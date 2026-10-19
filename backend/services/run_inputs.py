"""Apply queued steering at model-request boundaries, never during tool dispatch."""

from sqlalchemy import select

from backend.models import RunInput
from backend.repositories import MessageRepository, TaskRepository
from backend.services.providers.base import TextBlock, blocks_to_dicts
from backend.utils import utcnow


def pending(db, conversation_id):
    with db.session() as session:
        return session.scalar(select(RunInput.id).where(
            RunInput.conversation_id == conversation_id, RunInput.delivered_at.is_(None),
            RunInput.superseded_at.is_(None),
        ).limit(1)) is not None


def deliver(db, conversation_id):
    with db.session() as session:
        rows = list(session.scalars(select(RunInput).where(
            RunInput.conversation_id == conversation_id, RunInput.delivered_at.is_(None),
            RunInput.superseded_at.is_(None),
        ).order_by(RunInput.created_at, RunInput.id)))
        for row in rows:
            text = "Operator steering (new direction takes precedence):\n" + row.message
            MessageRepository(session).add(
                conversation_id=conversation_id, role="user", content=text,
                user_text=row.message, blocks=blocks_to_dicts([TextBlock(text)]),
            )
            row.delivered_at = utcnow()
        if rows:
            repository = TaskRepository(session)
            task = repository.active_for_conversation(conversation_id)
            if task is not None:
                repository.revise(task, objective=rows[-1].message)
        return len(rows)
