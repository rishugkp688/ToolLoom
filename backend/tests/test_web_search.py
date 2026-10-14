"""Offline tests for the keyless web search tool.

Network is never used here. Parsers are pure and tested against a captured
DuckDuckGo response; the transport is stubbed for the dispatch paths. A live
check exists at the bottom and is skipped unless TOOLLOOM_LIVE_SEARCH=1.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest

from backend.services.sandbox import search_tool
from backend.services.sandbox.search_tool import WebSearchError

# Captured verbatim from https://html.duckduckgo.com/html/ (q=python pathlib),
# whitespace collapsed. The `result__extras` block sits between each title anchor
# and its snippet, which is exactly what the pairing logic must skip.
DDG_HTML = (
    '<div class="result results_links results_links_deep web-result "><div class="links_main '
    'links_deep result__body"><!-- This is the visible part --><h2 class="result__title"><a '
    'rel="nofollow" class="result__a" href="https://docs.python.org/3/library/pathlib.html">'
    'pathlib — Object-oriented filesystem paths — Python 3.15.0 documentation</a></h2>'
    '<div class="result__extras"><div class="result__extras__url"><span class="result__icon">'
    '<a rel="nofollow" href="https://docs.python.org/3/library/pathlib.html"><img '
    'class="result__icon__img" width="16" height="16" alt="" '
    'src="//external-content.duckduckgo.com/ip3/docs.python.org.ico" /></a></span>'
    '<a class="result__url" href="https://docs.python.org/3/library/pathlib.html"> '
    'docs.python.org/3/library/pathlib.html </a><span>&nbsp; 2026-10-14</span></div></div>'
    '<a class="result__snippet" href="https://docs.python.org/3/library/pathlib.html">'
    'Source code: Lib/<b>pathlib</b>/ This module offers classes representing filesystem '
    'paths with semantics appropriate for different operating systems.</a></div></div>'
    '<div class="result results_links results_links_deep web-result "><div class="links_main '
    'links_deep result__body"><h2 class="result__title"><a rel="nofollow" class="result__a" '
    'href="https://www.geeksforgeeks.org/python/pathlib-module-in-python/">Pathlib Module in '
    'Python - GeeksforGeeks</a></h2><a class="result__snippet" '
    'href="https://www.geeksforgeeks.org/python/pathlib-module-in-python/"><b>Pathlib</b> '
    'provides an object-oriented way to work with filesystem paths &amp; more.</a></div></div>'
)


def _ctx(**overrides) -> SimpleNamespace:
    values = {
        "toolloom_search_provider": "duckduckgo",
        "toolloom_search_url": None,
        "toolloom_search_max_results": 5,
        "toolloom_search_timeout_seconds": 15,
    }
    values.update(overrides)
    return SimpleNamespace(settings=SimpleNamespace(**values))


def _stub(monkeypatch, body: str, status: int = 200) -> dict:
    seen: dict = {}

    def fake_request(method, url, *, timeout, params=None, data=None):
        seen.update(method=method, url=url, params=params, data=data, timeout=timeout)
        return httpx.Response(status_code=status, text=body)

    monkeypatch.setattr(search_tool, "_request", fake_request)
    return seen


# --- parsing -----------------------------------------------------------------

def test_parses_duckduckgo_results_in_order():
    results = search_tool.parse_duckduckgo(DDG_HTML, 5)
    assert [r.url for r in results] == [
        "https://docs.python.org/3/library/pathlib.html",
        "https://www.geeksforgeeks.org/python/pathlib-module-in-python/",
    ]
    assert results[0].title.startswith("pathlib — Object-oriented")
    assert results[0].snippet.startswith("Source code: Lib/pathlib/")
    # Inline highlight tags are stripped, entities decoded.
    assert "<b>" not in results[1].snippet
    assert "&amp;" not in results[1].snippet
    assert "paths & more" in results[1].snippet


def test_duckduckgo_respects_the_limit():
    assert len(search_tool.parse_duckduckgo(DDG_HTML, 1)) == 1


def test_duckduckgo_skips_unusable_links():
    markup = (
        '<a class="result__a" href="/relative/only">Relative</a>'
        '<a class="result__a" href="https://ok.example/x">Fine</a>'
    )
    results = search_tool.parse_duckduckgo(markup, 5)
    assert [r.url for r in results] == ["https://ok.example/x"]


@pytest.mark.parametrize(
    "href,expected",
    [
        ("https://example.com/a", "https://example.com/a"),
        ("//example.com/a", "https://example.com/a"),
        (
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb%3Fq%3D1&rut=abc",
            "https://example.com/b?q=1",
        ),
    ],
)
def test_link_unwrapping(href, expected):
    assert search_tool._unwrap(href) == expected


def test_parses_wikipedia_payload():
    payload = {
        "query": {
            "search": [
                {"title": "Asyncio", "snippet": "Asyncio is a <b>Python</b> library."},
                {"title": "Coroutine", "snippet": "Coroutines generalize subroutines."},
            ]
        }
    }
    results = search_tool.parse_wikipedia(payload, 5)
    assert results[0].title == "Asyncio"
    assert results[0].url == "https://en.wikipedia.org/wiki/Asyncio"
    assert results[0].snippet == "Asyncio is a Python library."


def test_parses_searxng_payload():
    payload = {
        "results": [
            {"title": "One", "url": "https://one.example", "content": "first"},
            {"title": "Bad", "url": "ftp://bad.example"},
            {"title": "Two", "url": "https://two.example", "content": "second"},
        ]
    }
    results = search_tool.parse_searxng(payload, 5)
    assert [r.url for r in results] == ["https://one.example", "https://two.example"]


def test_searxng_rejects_non_json_shape():
    with pytest.raises(WebSearchError):
        search_tool.parse_searxng({"unexpected": True}, 5)


def test_snippets_are_truncated():
    long_snippet = "x" * (search_tool.MAX_SNIPPET_CHARS + 50)
    results = search_tool.parse_searxng(
        {"results": [{"title": "T", "url": "https://e.example", "content": long_snippet}]}, 5
    )
    assert len(results[0].snippet) <= search_tool.MAX_SNIPPET_CHARS + 1
    assert results[0].snippet.endswith("…")


# --- dispatch ----------------------------------------------------------------

def test_duckduckgo_search_posts_the_query(monkeypatch):
    seen = _stub(monkeypatch, DDG_HTML)
    output = search_tool.search({"query": "python pathlib"}, _ctx())
    assert seen["method"] == "POST"
    assert seen["url"] == search_tool.DUCKDUCKGO_ENDPOINT
    assert seen["data"] == {"q": "python pathlib", "kl": "wt-wt"}
    assert "2 result(s)" in output
    assert "https://docs.python.org/3/library/pathlib.html" in output


def test_wikipedia_search_uses_the_api(monkeypatch):
    body = json.dumps({"query": {"search": [{"title": "Asyncio", "snippet": "async io"}]}})
    seen = _stub(monkeypatch, body)
    output = search_tool.search({"query": "asyncio"}, _ctx(toolloom_search_provider="wikipedia"))
    assert seen["url"] == search_tool.WIKIPEDIA_ENDPOINT
    assert seen["params"]["srsearch"] == "asyncio"
    assert "via wikipedia" in output


def test_searxng_uses_the_configured_instance(monkeypatch):
    body = json.dumps({"results": [{"title": "T", "url": "https://e.example", "content": "c"}]})
    seen = _stub(monkeypatch, body)
    output = search_tool.search(
        {"query": "hello"},
        _ctx(toolloom_search_provider="searxng", toolloom_search_url="http://127.0.0.1:8080"),
    )
    assert seen["url"] == "http://127.0.0.1:8080/search"
    assert seen["params"] == {"q": "hello", "format": "json"}
    assert "via searxng" in output


def test_empty_results_are_reported_honestly(monkeypatch):
    _stub(monkeypatch, "<html>nothing</html>")
    output = search_tool.search({"query": "zzzz"}, _ctx())
    assert "returned nothing" in output


# --- validation and limits ---------------------------------------------------

def test_query_is_required():
    with pytest.raises(WebSearchError, match="`query` is required"):
        search_tool.search({"query": "   "}, _ctx())


def test_overlong_query_is_refused():
    with pytest.raises(WebSearchError, match="at most"):
        search_tool.search({"query": "x" * 500}, _ctx())


def test_unknown_provider_is_refused():
    with pytest.raises(WebSearchError, match="unknown search provider"):
        search_tool.search({"query": "hi"}, _ctx(toolloom_search_provider="bing"))


def test_searxng_without_url_explains_itself():
    with pytest.raises(WebSearchError, match="TOOLLOOM_SEARCH_URL"):
        search_tool.search({"query": "hi"}, _ctx(toolloom_search_provider="searxng"))


def test_invalid_instance_url_is_refused():
    with pytest.raises(WebSearchError, match="http\\(s\\) URL"):
        search_tool.search(
            {"query": "hi"},
            _ctx(toolloom_search_provider="searxng", toolloom_search_url="file:///etc"),
        )


def test_result_count_is_clamped_to_the_hard_maximum():
    assert search_tool.resolve_limit(999, _ctx(toolloom_search_max_results=10).settings) == 10
    assert search_tool.resolve_limit(0, _ctx().settings) == 1
    assert search_tool.resolve_limit(None, _ctx().settings) == 5


def test_result_count_never_exceeds_the_configured_ceiling():
    assert search_tool.resolve_limit(10, _ctx(toolloom_search_max_results=3).settings) == 3


def test_non_integer_max_results_is_refused():
    with pytest.raises(WebSearchError, match="must be an integer"):
        search_tool.resolve_limit("many", _ctx().settings)


def test_transport_failure_becomes_a_tool_error(monkeypatch):
    class FailingClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(search_tool.httpx, "Client", FailingClient)
    with pytest.raises(WebSearchError, match="search request failed"):
        search_tool._duckduckgo("q", 5, 1.0)


def test_http_error_status_is_reported(monkeypatch):
    """The status check lives in _request, so drive it through a fake transport."""

    class FakeResponse:
        status_code = 429

        def iter_bytes(self):
            yield b"<html>blocked</html>"

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return FakeStream()

    monkeypatch.setattr(search_tool.httpx, "Client", FakeClient)
    with pytest.raises(WebSearchError, match="HTTP 429"):
        search_tool._duckduckgo("q", 5, 1.0)


def test_oversized_response_is_refused(monkeypatch):
    class FakeResponse:
        status_code = 200

        def iter_bytes(self):
            for _ in range(40):
                yield b"x" * 32_000

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return FakeStream()

    monkeypatch.setattr(search_tool.httpx, "Client", FakeClient)
    with pytest.raises(WebSearchError, match="size limit"):
        search_tool._duckduckgo("q", 5, 1.0)


# --- registration ------------------------------------------------------------

def test_web_search_is_registered_and_high_risk(client):
    server = next(s for s in client.get("/mcp/servers").json() if s["name"] == "sandbox")
    tools = client.get(f"/mcp/servers/{server['id']}/tools").json()
    tool = next(t for t in tools if t["name"] == "web_search")
    assert tool["namespaced_name"] == "sandbox__web_search"
    assert tool["risk"] == "high"
    assert tool["enabled"] is False and tool["approved"] is False
    assert tool["input_schema"]["required"] == ["query"]


def test_web_search_refuses_until_enabled_and_approved(client):
    import asyncio

    from backend.services.providers.base import ToolCallBlock

    enable = client.patch(
        f"/mcp/servers/{next(s for s in client.get('/mcp/servers').json() if s['name'] == 'sandbox')['id']}"
        "/tools/web_search",
        json={"enabled": True},
    )
    assert enable.status_code == 200
    result = asyncio.run(
        client.app.state.tool_executor.execute(
            ToolCallBlock("s1", "sandbox__web_search", {"query": "hello"}), "conv"
        )
    )
    assert result.is_error is True
    assert "requires explicit operator approval" in result.model_text


@pytest.mark.skipif(
    os.environ.get("TOOLLOOM_LIVE_SEARCH") != "1",
    reason="set TOOLLOOM_LIVE_SEARCH=1 to run a real keyless search",
)
def test_live_duckduckgo_returns_results():
    output = search_tool.search({"query": "python pathlib", "max_results": 3}, _ctx())
    assert "result(s)" in output
    assert "http" in output
