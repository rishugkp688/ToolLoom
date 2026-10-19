"""Safe outbound HTTP fetch (disabled by default; high-risk).

Controls, per BUILD_SPEC §7:

* only ``http``/``https`` schemes;
* hostnames are resolved and every resolved address must be public — loopback,
  private, link-local, multicast and reserved ranges are refused;
* forbidden request headers (Authorization, Cookie, Host, Proxy-Authorization)
  are rejected unless the whole value is a single ``${secrets.NAME}`` reference;
* response bodies are read with a hard size cap during streaming;
* redirects are bounded and revalidated; cross-origin mutations/credentials stop;
* connections use a validated literal IP with the original TLS hostname and Host.

Socket phases and the redirect/body loop are bounded. DNS waits have a deadline
and at most four outstanding OS lookups; timed-out lookups cannot be forcibly
interrupted and retain their slot until they finish. A socket phase may finish
after the overall budget, at which point its result is rejected.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from threading import BoundedSemaphore
from urllib.parse import urljoin, urlsplit

import httpx

from backend.services.secrets import SecretsError, is_single_reference
from backend.services.tool_errors import ToolPreflightError

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
FORBIDDEN_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "proxy-authorization",
}
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 15
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="toolloom-dns")
_DNS_SLOTS = BoundedSemaphore(4)
SAFE_RESPONSE_HEADERS = {"content-type", "content-length", "last-modified", "etag", "cache-control"}


class _PinnedTransport(httpx.HTTPTransport):
    def __init__(self, address: str | tuple[str, ...], hostname: str, deadline=None):
        super().__init__(trust_env=False, retries=0)
        self.addresses = (address,) if isinstance(address, str) else address
        self.hostname = hostname
        self.deadline = deadline if deadline is not None else time.monotonic() + TIMEOUT_SECONDS

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # httpcore's sni_hostname extension retains certificate hostname validation
        # while its TCP target becomes the already validated literal address.
        for index, address in enumerate(self.addresses):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.ConnectTimeout("HTTP operation deadline exceeded", request=request)
            timeout = {
                key: min(value, remaining) if value is not None else remaining
                for key, value in request.extensions.get("timeout", {}).items()
            }
            pinned = httpx.Request(
                request.method,
                request.url.copy_with(host=address),
                headers={**dict(request.headers), "host": request.url.netloc.decode("ascii")},
                stream=request.stream,
                extensions={
                    **request.extensions,
                    "sni_hostname": self.hostname,
                    "timeout": timeout,
                },
            )
            try:
                return super().handle_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Only connection establishment can move to another validated IP.
                # Never replay after a write/read failure or an HTTP response.
                if index == len(self.addresses) - 1:
                    raise
        raise httpx.ConnectError("no validated addresses", request=request)


class HttpFetchError(RuntimeError):
    pass


class HttpFetchPreflightError(HttpFetchError, ToolPreflightError):
    """A refused fetch that provably did not dispatch any request."""


def _is_blocked_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_blocked_address(str(ip.ipv4_mapped))
        # Translation/tunnel routing is operator-dependent; do not permit these
        # to bypass a public-address check with an embedded private IPv4 target.
        if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network(
            "64:ff9b:1::/48"
        ):
            return True
        if ip.teredo is not None or ip.is_site_local:
            return True
        if ip.sixtofour is not None and _is_blocked_address(str(ip.sixtofour)):
            return True
    return (
        not ip.is_global
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_check(hostname: str, *, timeout=TIMEOUT_SECONDS) -> tuple[str, ...]:
    if not _DNS_SLOTS.acquire(blocking=False):
        raise HttpFetchError("DNS resolver busy; retry later")
    try:
        future = _DNS_POOL.submit(socket.getaddrinfo, hostname, None)
    except BaseException:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _future: _DNS_SLOTS.release())
    try:
        infos = future.result(timeout=max(0, timeout))
    except TimeoutError as exc:
        future.cancel()
        raise HttpFetchError("DNS resolution deadline exceeded") from exc
    except socket.gaierror as exc:
        raise HttpFetchError(f"could not resolve host {hostname!r}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise HttpFetchError(f"could not resolve host {hostname!r}")
    for address in addresses:
        if _is_blocked_address(address):
            raise HttpFetchError(f"refusing to connect to non-public address {address}")
    return tuple(sorted(addresses, key=lambda value: (":" in value, value)))


def _check_headers(headers: dict[str, str], resolver) -> dict[str, str]:
    clean: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise HttpFetchError("header names and values must be text")
        if name.lower() in FORBIDDEN_REQUEST_HEADERS:
            if not (resolver is not None and is_single_reference(value)):
                raise HttpFetchError(f"header {name!r} is not allowed with a literal value")
        clean[name] = value
    return clean


def fetch(arguments: dict, ctx) -> str:
    state = {"dispatched": False}
    try:
        return _fetch(arguments, ctx, state)
    except (HttpFetchError, ValueError, httpx.InvalidURL, SecretsError) as exc:
        if not state["dispatched"]:
            raise HttpFetchPreflightError(str(exc)) from exc
        raise


def _fetch(arguments: dict, ctx, state: dict) -> str:
    url = str(arguments.get("url") or "").strip()
    if not url:
        raise HttpFetchError("`url` is required")
    method = str(arguments.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise HttpFetchError(f"method {method!r} is not allowed")
    body = arguments.get("body")
    body_bytes = body.encode("utf-8") if isinstance(body, str) else None
    if body_bytes is not None and len(body_bytes) > MAX_BODY_BYTES:
        raise HttpFetchError("request body exceeds the 5 MB limit")

    resolver = getattr(ctx, "secrets", None)
    headers = _check_headers(arguments.get("headers") or {}, resolver)
    if resolver is not None:
        headers = {name: _resolve(value, resolver) for name, value in headers.items()}

    current = url
    deadline = time.monotonic() + TIMEOUT_SECONDS
    for hop in range(MAX_REDIRECTS + 1):
        if time.monotonic() >= deadline:
            raise HttpFetchError("HTTP operation deadline exceeded")
        parsed = urlsplit(current)
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise HttpFetchError(f"scheme {parsed.scheme!r} is not allowed")
        if not parsed.hostname:
            raise HttpFetchError("url has no host")
        if len(current) > 8192 or parsed.username is not None or parsed.password is not None:
            raise HttpFetchError("URL userinfo is forbidden; URL must be at most 8192 characters")
        try:
            _port = parsed.port
        except ValueError as exc:
            raise HttpFetchError("invalid URL port") from exc
        addresses = _resolve_and_check(parsed.hostname, timeout=deadline - time.monotonic())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HttpFetchError("HTTP operation deadline exceeded during resolution")

        with httpx.Client(
            follow_redirects=False,
            timeout=remaining,
            trust_env=False,
            transport=_PinnedTransport(addresses, parsed.hostname, deadline),
        ) as client:
            state["dispatched"] = True
            with client.stream(method, current, headers=headers, content=body_bytes) as response:
                if response.is_redirect:
                    if hop >= MAX_REDIRECTS:
                        raise HttpFetchError("too many redirects")
                    location = response.headers.get("location")
                    if not location:
                        raise HttpFetchError("redirect has no Location header")
                    nxt = urljoin(current, location)
                    if _cross_origin(current, nxt):
                        if method not in {"GET", "HEAD"} or body_bytes or headers:
                            raise HttpFetchError(
                                "refusing cross-origin redirect with caller headers or mutation"
                            )
                        if parsed.scheme == "https" and urlsplit(nxt).scheme != "https":
                            raise HttpFetchError("refusing HTTPS downgrade redirect")
                    if response.status_code == 303 and method != "HEAD":
                        method, body_bytes = "GET", None
                        headers = {
                            name: value
                            for name, value in headers.items()
                            if name.lower()
                            not in {"content-type", "content-length", "transfer-encoding"}
                        }
                    current = nxt
                    continue
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise HttpFetchError("HTTP operation deadline exceeded while reading body")
                    total += len(chunk)
                    if total > MAX_BODY_BYTES:
                        raise HttpFetchError("response body exceeds the 5 MB limit")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                rendered = _render(response, raw)
                if response.status_code >= 400:
                    raise HttpFetchError(rendered)
                return rendered
    raise HttpFetchError("too many redirects")


def _resolve(value: str, resolver) -> str:
    from backend.services.secrets import _substitute

    return _substitute(value, resolver.lookup)


def _cross_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)

    def origin(parsed):
        return (
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )

    return origin(pa) != origin(pb)


def _render(response: httpx.Response, raw: bytes) -> str:
    try:
        final_url_text = str(response.request.url)
    except RuntimeError:
        final_url_text = ""
    content_type = response.headers.get("content-type", "").lower().split(";", 1)[0].strip()
    if content_type and not (
        content_type.startswith("text/")
        or content_type in {"application/json", "application/xml", "application/xhtml+xml"}
        or content_type.endswith(("+json", "+xml"))
    ):
        raise HttpFetchError(f"unsupported binary/content type: {content_type}")
    try:
        text = raw.decode(response.encoding or "utf-8", "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    if "text/html" in response.headers.get("content-type", "").lower():
        parser = _ReadableHTML(final_url_text)
        parser.feed(text)
        text = "\n".join(
            line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
        )
        text = "[Readable HTML; scripts and styles omitted]\n" + text
    header_lines = "\n".join(
        f"{k}: {v}" for k, v in response.headers.items() if k.lower() in SAFE_RESPONSE_HEADERS
    )
    final_url = f"\nFinal URL: {final_url_text}" if final_url_text else ""
    return f"HTTP {response.status_code}{final_url}\n{header_lines}\n\n[External content]\n{text}"


class _ReadableHTML(HTMLParser):
    """Cheap readable text for static pages; never executes scripts or fetches links."""

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []
        self.links = []
        self.base_url = base_url

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.append(tag)
            return
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden.append(tag)
            return
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "section", "article"}:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href", "")
            link = urljoin(self.base_url, href)
            self.links.append(link if urlsplit(link).scheme in ALLOWED_SCHEMES else "")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == "a" and self.links:
            link = self.links.pop()
            if link:
                self.parts.append(f" ({link})")
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr", "section", "article"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
