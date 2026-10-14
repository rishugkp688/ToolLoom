"""Keyless web search for the agent.

Providers, selected with ``TOOLLOOM_SEARCH_PROVIDER``:

* ``duckduckgo`` (default) — the public HTML endpoint, parsed directly. No
  account, no key. Scraped markup can change without notice, so parsing is
  isolated in pure functions and covered by fixture tests.
* ``wikipedia`` — the MediaWiki search API. Keyless and stable, but encyclopedic
  rather than general web search.
* ``searxng`` — an operator-run instance with the ``json`` output format
  enabled. Nothing is inferred or auto-discovered; the operator supplies the URL.

Egress policy: the model's query text leaves this machine, so the tool is
high-risk, ships disabled, and needs explicit operator approval. Only the query
is sent. No credentials, conversation history, secrets or workspace files are
attached, and nothing from the vault is read.

Known limitations, stated rather than papered over:

* DuckDuckGo exposes no documented search API; this reads its HTML endpoint, so
  it can break when the markup changes and its availability is not guaranteed.
* Results are whatever the provider returns. They are data for the model to
  read, never instructions to follow.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html/"
WIKIPEDIA_ENDPOINT = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "ToolLoom/0.1 (local agent; sandbox__web_search)"

PROVIDERS = ("duckduckgo", "wikipedia", "searxng")
DEFAULT_PROVIDER = "duckduckgo"
DEFAULT_MAX_RESULTS = 5
HARD_MAX_RESULTS = 10
MAX_QUERY_CHARS = 400
MAX_SNIPPET_CHARS = 300
MAX_RESPONSE_BYTES = 512 * 1024
MAX_REDIRECTS = 3

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_DDG_ANCHOR = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
_DDG_SNIPPET = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)


class WebSearchError(RuntimeError):
    """A search could not be performed. Surfaced to the model as a tool error."""


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""


# --- parsing (pure; covered by fixture tests) --------------------------------

def _clean(text: str) -> str:
    collapsed = _WS.sub(" ", html.unescape(_TAG.sub("", text or ""))).strip()
    if len(collapsed) > MAX_SNIPPET_CHARS:
        collapsed = collapsed[:MAX_SNIPPET_CHARS].rstrip() + "…"
    return collapsed


def _unwrap(href: str) -> str:
    """DuckDuckGo sometimes serves result links through its own redirector."""
    href = (href or "").strip()
    if "uddg=" in href:
        query = parse_qs(urlsplit(href).query)
        if query.get("uddg"):
            return query["uddg"][0]
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_duckduckgo(markup: str, limit: int) -> list[Result]:
    """Pair each result anchor with the snippet that follows it."""
    anchors = list(_DDG_ANCHOR.finditer(markup))
    results: list[Result] = []
    for index, anchor in enumerate(anchors):
        if len(results) >= limit:
            break
        url = _unwrap(anchor.group(1))
        title = _clean(anchor.group(2))
        if not url.startswith(("http://", "https://")):
            continue
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(markup)
        segment = markup[anchor.end() : end]
        snippet_match = _DDG_SNIPPET.search(segment)
        snippet = _clean(snippet_match.group(1)) if snippet_match else ""
        results.append(Result(title=title or url, url=url, snippet=snippet))
    return results


def parse_searxng(payload: Any, limit: int) -> list[Result]:
    if not isinstance(payload, dict):
        raise WebSearchError("search instance did not return a JSON object")
    raw = payload.get("results")
    if not isinstance(raw, list):
        raise WebSearchError("search instance response has no results list")
    results: list[Result] = []
    for item in raw:
        if len(results) >= limit:
            break
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        results.append(
            Result(
                title=_clean(str(item.get("title") or url)),
                url=url,
                snippet=_clean(str(item.get("content") or "")),
            )
        )
    return results


def parse_wikipedia(payload: Any, limit: int) -> list[Result]:
    if not isinstance(payload, dict):
        raise WebSearchError("wikipedia returned an unexpected payload")
    hits = (payload.get("query") or {}).get("search")
    if not isinstance(hits, list):
        raise WebSearchError("wikipedia response has no search results")
    results: list[Result] = []
    for hit in hits:
        if len(results) >= limit:
            break
        title = str(hit.get("title") or "").strip()
        if not title:
            continue
        results.append(
            Result(
                title=title,
                url=f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                snippet=_clean(str(hit.get("snippet") or "")),
            )
        )
    return results


# --- transport ---------------------------------------------------------------

def _read_capped(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise WebSearchError("search response exceeded the size limit")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def _request(
    method: str, url: str, *, timeout: float, params: dict | None = None, data: dict | None = None
) -> httpx.Response:
    try:
        with httpx.Client(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            with client.stream(method, url, params=params, data=data) as response:
                body = _read_capped(response)
                status = response.status_code
    except WebSearchError:
        raise
    except httpx.HTTPError as exc:
        raise WebSearchError(f"search request failed: {type(exc).__name__}") from exc
    if status >= 400:
        raise WebSearchError(f"search provider returned HTTP {status}")
    return httpx.Response(status_code=status, content=body.encode("utf-8"))


def _duckduckgo(query: str, limit: int, timeout: float) -> list[Result]:
    response = _request(
        "POST", DUCKDUCKGO_ENDPOINT, timeout=timeout, data={"q": query, "kl": "wt-wt"}
    )
    return parse_duckduckgo(response.text, limit)


def _wikipedia(query: str, limit: int, timeout: float) -> list[Result]:
    response = _request(
        "GET",
        WIKIPEDIA_ENDPOINT,
        timeout=timeout,
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "format": "json",
            "formatversion": "2",
        },
    )
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise WebSearchError("wikipedia returned non-JSON content") from exc
    return parse_wikipedia(payload, limit)


def _instance_url(settings) -> str:
    base = (getattr(settings, "toolloom_search_url", None) or "").strip()
    if not base:
        raise WebSearchError(
            "TOOLLOOM_SEARCH_PROVIDER=searxng requires TOOLLOOM_SEARCH_URL to point at an "
            "instance with the json output format enabled"
        )
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise WebSearchError("TOOLLOOM_SEARCH_URL must be an http(s) URL")
    return base.rstrip("/") + "/search"


def _searxng(query: str, limit: int, timeout: float, settings) -> list[Result]:
    response = _request(
        "GET", _instance_url(settings), timeout=timeout, params={"q": query, "format": "json"}
    )
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise WebSearchError(
            "search instance did not return JSON; enable the json format on the instance"
        ) from exc
    return parse_searxng(payload, limit)


# --- entry point -------------------------------------------------------------

def resolve_provider(settings) -> str:
    provider = str(getattr(settings, "toolloom_search_provider", DEFAULT_PROVIDER) or "").lower()
    if provider not in PROVIDERS:
        raise WebSearchError(
            f"unknown search provider {provider!r}; expected one of {', '.join(PROVIDERS)}"
        )
    return provider


def resolve_limit(requested: Any, settings) -> int:
    configured = int(getattr(settings, "toolloom_search_max_results", DEFAULT_MAX_RESULTS))
    try:
        limit = int(requested) if requested is not None else configured
    except (TypeError, ValueError):
        raise WebSearchError("`max_results` must be an integer") from None
    return max(1, min(limit, HARD_MAX_RESULTS, max(1, configured)))


def render(query: str, provider: str, results: list[Result]) -> str:
    header = f'{len(results)} result(s) for "{query}" via {provider}'
    if not results:
        return f"{header}: the provider returned nothing. Try different wording."
    blocks = [header, ""]
    for position, result in enumerate(results, start=1):
        blocks.append(f"{position}. {result.title}")
        blocks.append(f"   {result.url}")
        if result.snippet:
            blocks.append(f"   {result.snippet}")
        blocks.append("")
    return "\n".join(blocks).rstrip()


def search(arguments: dict, ctx) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise WebSearchError("`query` is required")
    if len(query) > MAX_QUERY_CHARS:
        raise WebSearchError(f"`query` must be at most {MAX_QUERY_CHARS} characters")

    settings = getattr(ctx, "settings", None)
    provider = resolve_provider(settings)
    limit = resolve_limit(arguments.get("max_results"), settings)
    timeout = float(getattr(settings, "toolloom_search_timeout_seconds", 15) or 15)

    if provider == "duckduckgo":
        results = _duckduckgo(query, limit, timeout)
    elif provider == "wikipedia":
        results = _wikipedia(query, limit, timeout)
    else:
        results = _searxng(query, limit, timeout, settings)
    return render(query, provider, results[:limit])
