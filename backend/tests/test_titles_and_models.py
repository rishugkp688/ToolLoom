from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.repositories import ConversationRepository, MessageRepository
from backend.services.context_builder import ContextBuilder
from backend.services.conversation_titles import TITLE_PROMPT
from backend.services.model_catalogue import ModelBudget, ModelCatalogue, budget_for
from backend.services.providers.base import ModelInfo, ModelTurn, TextBlock, ToolCallBlock, Usage
from backend.services.providers.commandcode import CommandCodeProvider
from backend.services.providers.fake import FakeProvider
from backend.tests.conftest import LOCAL_BASE_URL
from backend.tests.helpers import parse_sse


class RecordingProvider(FakeProvider):
    def __init__(self, title_result=None):
        super().__init__()
        self.title_result = title_result or ModelTurn(
            [TextBlock('"Planning a Photography Walk"')], "end_turn", Usage(12, 5), "small"
        )
        self.calls = []
        self.lookups = 0

    async def list_models(self):
        self.lookups += 1
        return [ModelInfo("small", context_window=2048), ModelInfo("large", context_window=65536)]

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("system") == TITLE_PROMPT:
            if isinstance(self.title_result, Exception):
                raise self.title_result
            return self.title_result
        return ModelTurn(
            [TextBlock("Earlier discussion summary.")], "end_turn", Usage(2, 2), "small"
        )

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        async for event in super().stream(**kwargs):
            yield event


def test_generated_title_persists_once_and_accounts_for_usage(make_app):
    provider = RecordingProvider()
    app = make_app(provider, toolloom_auto_titles=True)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        response = client.post(
            f"/conversations/{cid}/messages/stream",
            json={
                "message": "Help me plan a photography walk",
                "model_id": "small",
            },
        )
        events = parse_sse(response.text)
        assert events[-1][0] == "done"
        assert not any(kind == "error" for kind, _ in events)
        detail = client.get(f"/conversations/{cid}").json()
        assert detail["title"] == "Planning a Photography Walk"
        assert len(detail["messages"]) == 2
        call = next(c for c in provider.calls if c.get("system") == TITLE_PROMPT)
        assert call["model_id"] == "small"
        assert call["tools"] == []
        assert call["max_tokens"] == 512  # selected small model still bounds the helper
        with app.state.db.session() as session:
            conv = ConversationRepository(session).get(cid)
            assert conv.title_source == "generated"
            assert (conv.title_usage_input, conv.title_usage_output) == (12, 5)
        total = app.state.usage_service.summary()["per_conversation"][cid]
        chat_usage = events[-1][1]["usage"]
        assert total["total_tokens"] == chat_usage["total_tokens"] + 17
        assert (
            client.post(f"/conversations/{cid}/messages", json={"message": "Thanks"}).status_code
            == 200
        )
        assert sum(c.get("system") == TITLE_PROMPT for c in provider.calls) == 1
        assert client.get("/conversations").json()[0]["title"] == detail["title"]


@pytest.mark.parametrize(
    "result",
    [
        RuntimeError("provider unavailable"),
        ModelTurn([], "end_turn", Usage(), "small"),
        ModelTurn([TextBlock("unfinished")], "max_tokens", Usage(), "small"),
        ModelTurn([ToolCallBlock("x", "unsafe", {})], "tool_use", Usage(), "small"),
    ],
)
def test_title_failure_keeps_successful_chat_and_readable_fallback(make_app, result):
    app = make_app(RecordingProvider(result), toolloom_auto_titles=True)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        response = client.post(f"/conversations/{cid}/messages", json={"message": "My walk"})
        assert response.status_code == 200
        assert response.json()["reply"] == "Echo: My walk"
        with app.state.db.session() as session:
            conv = ConversationRepository(session).get(cid)
            assert conv.title == "My walk"
            assert conv.title_source == "fallback"
        assert cid not in app.state.active_runs


def test_title_timeout_does_not_discard_reply(make_app, monkeypatch):
    provider = RecordingProvider()

    async def slow_title(**kwargs):
        await asyncio.sleep(1)

    provider.complete = slow_title
    monkeypatch.setattr("backend.services.conversation_titles.TITLE_TIMEOUT_SECONDS", 0.01)
    with TestClient(
        make_app(provider, toolloom_auto_titles=True), base_url=LOCAL_BASE_URL
    ) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        response = client.post(f"/conversations/{cid}/messages/stream", json={"message": "hello"})
        assert parse_sse(response.text)[-1][0] == "done"
        assert client.get(f"/conversations/{cid}").json()["title"] == "hello"


@pytest.mark.parametrize("legacy,custom", [(False, True), (True, True), (True, False)])
def test_manual_titles_preserved_and_legacy_provisional_titles_upgraded(make_app, legacy, custom):
    provider = RecordingProvider()
    app = make_app(provider, toolloom_auto_titles=True)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post(
            "/conversations", json={"title": "My custom name"} if custom else {}
        ).json()["id"]
        if legacy:
            with app.state.db.session() as session:
                conv = ConversationRepository(session).get(cid)
                conv.title_source = "legacy"
                if not custom:
                    conv.title = "My walk"
        client.post(f"/conversations/{cid}/messages", json={"message": "My walk"})
        assert client.get(f"/conversations/{cid}").json()["title"] == (
            "My custom name" if custom else "Planning a Photography Walk"
        )
        assert sum(c.get("system") == TITLE_PROMPT for c in provider.calls) == (0 if custom else 1)


@pytest.mark.parametrize(
    "raw,expected", [(1000000, 1000000), (None, None), (True, None), (-1, None), ("8192", None)]
)
def test_provider_normalizes_reported_context(raw, expected):
    models = CommandCodeProvider._normalize_models(
        None,
        {
            "data": [
                {"id": "model", "name": "Model", "context_length": raw},
            ]
        },
    )
    assert models[0].context_window == expected


async def test_catalogue_exact_selected_model_cache_and_unknown_fallback():
    provider = RecordingProvider()
    catalogue = ModelCatalogue(provider, Settings(_env_file=None))
    small, large, unknown = await asyncio.gather(
        catalogue.budget("small"),
        catalogue.budget("large"),
        catalogue.budget("missing"),
    )
    assert (small.context_window, small.max_output_tokens) == (2048, 512)
    assert large.context_window == 65536
    assert (unknown.context_window, unknown.source) == (32768, "fallback")
    assert provider.lookups == 1


async def test_catalogue_failure_has_bounded_retries_and_explicit_fallback():
    provider = RecordingProvider()

    async def fail():
        provider.lookups += 1
        raise RuntimeError("offline")

    provider.list_models = fail
    catalogue = ModelCatalogue(provider, Settings(_env_file=None))
    assert (await catalogue.budget("small")).source == "fallback"
    assert (await catalogue.budget("small")).source == "fallback"
    assert provider.lookups == 1


def test_configured_cap_never_exceeds_reported_window():
    settings = Settings(_env_file=None, model_context_window=100000)
    assert budget_for(settings, ModelInfo("small", context_window=2048)).context_window == 2048
    settings.model_context_window = 1024
    assert budget_for(settings, ModelInfo("small", context_window=2048)).source == "override"


def test_api_and_runtime_use_selected_model_budget_and_reject_oversized_turn(make_app):
    provider = RecordingProvider()
    app = make_app(provider)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        models = client.get("/models").json()
        assert models[0]["effective_context_window"] == 2048
        for model, output in [("small", 512), ("large", 4096)]:
            cid = client.post("/conversations", json={}).json()["id"]
            response = client.post(
                f"/conversations/{cid}/messages", json={"message": "Hi", "model_id": model}
            )
            assert response.status_code == 200
            assert provider.calls[-1]["model_id"] == model
            assert provider.calls[-1]["max_tokens"] == output
        before = len(provider.calls)
        cid = client.post("/conversations", json={}).json()["id"]
        response = client.post(
            f"/conversations/{cid}/messages/stream",
            json={"message": "x" * 20000, "model_id": "small"},
        )
        assert any(kind == "error" for kind, _ in parse_sse(response.text))
        assert len(provider.calls) == before


async def test_context_trimming_and_compaction_follow_model_size(make_app):
    provider = RecordingProvider()
    app = make_app(provider)
    with app.state.db.session() as session:
        cid = ConversationRepository(session).create().id
        repo = MessageRepository(session)
        for index in range(6):
            repo.add(conversation_id=cid, role="user", content=f"q{index} " + "x" * 800)
            repo.add(conversation_id=cid, role="assistant", content="a" * 800)
        repo.add(conversation_id=cid, role="user", content="latest")
    small = ModelBudget(2048, 512, "provider")
    large = ModelBudget(65536, 4096, "provider")
    short = ContextBuilder(app.state.db, app.state.settings, small).build(cid)
    long = ContextBuilder(app.state.db, app.state.settings, large).build(cid)
    assert len(short.messages) < len(long.messages)
    assert short.messages[-1].text == "latest"
    assert not await app.state.runtime.compactor.compact_if_needed(
        cid, model_id="large", budget=large
    )
    assert await app.state.runtime.compactor.compact_if_needed(cid, model_id="small", budget=small)
    assert provider.calls[-1]["model_id"] == "small"
    assert provider.calls[-1]["max_tokens"] == 512
    with app.state.db.session() as session:
        assert repo.count(cid) == 13  # context trimming never deletes the audit log


def test_title_migration_is_idempotent_and_preserves_old_names(tmp_path):
    import sqlite3

    from backend.db import Database

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO conversations VALUES ('old', 'Existing title')")
    db = Database(f"sqlite:///{path}")
    try:
        db._apply_lightweight_migrations()
        db._apply_lightweight_migrations()
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT title, title_source, title_usage_input, title_usage_output, title_attempts, title_last_attempt, title_error FROM conversations"
            ).fetchone()
        assert row == ("Existing title", "legacy", 0, 0, 0, None, None)
    finally:
        db.dispose()


def test_title_uses_typed_text_and_file_names_without_sending_file_body(make_app):
    provider = RecordingProvider()
    app = make_app(provider, toolloom_auto_titles=True)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        upload = client.post(
            f"/conversations/{cid}/files",
            files={
                "files": ("notes.txt", b"private file body", "text/plain"),
            },
        ).json()["files"][0]
        # A fixed assistant response ensures the title helper receives no echoed file preview.
        provider._script = [ModelTurn([TextBlock("Received.")], "end_turn", Usage(2, 2), "small")]
        client.post(
            f"/conversations/{cid}/messages",
            json={
                "message": "Discuss my notes",
                "attachments": [upload["path"]],
            },
        )
        prompt = next(
            c["messages"][0].text for c in provider.calls if c.get("system") == TITLE_PROMPT
        )
        assert "Discuss my notes" in prompt and "notes.txt" in prompt
        assert "<attachment" not in prompt and "private file body" not in prompt


def test_failed_title_retries_after_cooldown_then_stays_generated(make_app, monkeypatch):
    from datetime import timedelta

    from backend.utils import utcnow

    now = utcnow()
    monkeypatch.setattr("backend.services.conversation_titles.utcnow", lambda: now)
    provider = RecordingProvider(ModelTurn([], "max_tokens", Usage(331, 128), "large"))
    app = make_app(provider, toolloom_auto_titles=True)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        for text in ["Tell me about inference engineering", "Please explain more"]:
            assert (
                client.post(
                    f"/conversations/{cid}/messages", json={"message": text, "model_id": "large"}
                ).status_code
                == 200
            )
        title_calls = [c for c in provider.calls if c.get("system") == TITLE_PROMPT]
        assert len(title_calls) == 1  # no rapid retry or retry during page reads
        assert title_calls[0]["max_tokens"] == 1024
        with app.state.db.session() as session:
            conv = ConversationRepository(session).get(cid)
            assert (conv.title_source, conv.title_error, conv.title_attempts) == (
                "fallback",
                "output_limit",
                1,
            )
        now += timedelta(seconds=61)
        provider.title_result = ModelTurn(
            [TextBlock("Understanding Inference Engineering")], "end_turn", Usage(20, 10), "large"
        )
        client.post(
            f"/conversations/{cid}/messages", json={"message": "Thanks", "model_id": "large"}
        )
        with app.state.db.session() as session:
            conv = ConversationRepository(session).get(cid)
            assert conv.title == "Understanding Inference Engineering"
            assert conv.title_source == "generated" and conv.title_error is None
            assert conv.title_attempts == 2
            assert (conv.title_usage_input, conv.title_usage_output) == (351, 138)
        now += timedelta(seconds=61)
        client.post(f"/conversations/{cid}/messages", json={"message": "Another question"})
        assert sum(c.get("system") == TITLE_PROMPT for c in provider.calls) == 2


def test_title_retries_are_bounded_even_when_provider_stays_unavailable(make_app, monkeypatch):
    from datetime import timedelta

    from backend.utils import utcnow

    now = utcnow()
    monkeypatch.setattr("backend.services.conversation_titles.utcnow", lambda: now)
    provider = RecordingProvider(RuntimeError("offline"))
    app = make_app(provider, toolloom_auto_titles=True)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        for _ in range(5):
            assert (
                client.post(f"/conversations/{cid}/messages", json={"message": "Hi"}).status_code
                == 200
            )
            now += timedelta(seconds=61)
        assert sum(c.get("system") == TITLE_PROMPT for c in provider.calls) == 3
        with app.state.db.session() as session:
            conv = ConversationRepository(session).get(cid)
            assert conv.title_attempts == 3 and conv.title_error == "provider_error"


def test_old_fallback_titles_are_eligible_for_repair(make_app):
    provider = RecordingProvider()
    app = make_app(provider, toolloom_auto_titles=True)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        with app.state.db.session() as session:
            conv = ConversationRepository(session).get(cid)
            conv.title = "An old preview"
            conv.title_source = "fallback"
        client.post(f"/conversations/{cid}/messages", json={"message": "Help plan a walk"})
        assert client.get(f"/conversations/{cid}").json()["title"] == "Planning a Photography Walk"
