from __future__ import annotations

from backend.repositories import ConversationRepository, MessageRepository
from backend.services.context_builder import ContextBuilder


async def test_compaction_summarizes_and_prefixes_context(make_app):
    app = make_app(model_context_window=1200, model_max_output_tokens=128)
    with app.state.db.session() as session:
        conversation = ConversationRepository(session).create()
        conversation_id = conversation.id
        repo = MessageRepository(session)
        for index in range(6):
            repo.add(conversation_id=conversation_id, role="user", content=f"q{index} " + "x" * 200)
            repo.add(
                conversation_id=conversation_id, role="assistant", content=f"a{index} " + "y" * 200
            )

    changed = await app.state.runtime.compactor.compact_if_needed(conversation_id)
    assert changed is True

    with app.state.db.session() as session:
        stored = ConversationRepository(session).get(conversation_id)
        assert stored.summary
        assert stored.summary_message_count > 0
        assert stored.summary_message_count < repo_count(session, conversation_id)

    built = ContextBuilder(app.state.db, app.state.settings).build(conversation_id)
    assert built.messages[0].text == "Summary of earlier conversation:"
    assert built.messages[1].role == "assistant"


async def test_compaction_is_a_noop_on_short_conversations(make_app):
    app = make_app()
    with app.state.db.session() as session:
        conversation = ConversationRepository(session).create()
        MessageRepository(session).add(
            conversation_id=conversation.id, role="user", content="hi"
        )
        conversation_id = conversation.id

    changed = await app.state.runtime.compactor.compact_if_needed(conversation_id)
    assert changed is False


def repo_count(session, conversation_id: str) -> int:
    return MessageRepository(session).count(conversation_id)
