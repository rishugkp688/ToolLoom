"""Safe outbound HTTP fetch (disabled by default; high-risk).

Controls, per BUILD_SPEC §7:

* only ``http``/``https`` schemes;
* hostnames are resolved and every resolved address must be public — loopback,
  private, link-local, multicast and reserved ranges are refused;
* forbidden request headers (Authorization, Cookie, Host, Proxy-Authorization)
  are rejected unless the whole value is a single ``${secrets.NAME}`` reference;
* response bodies are read with a hard size cap during streaming;
* redirects are followed up to a small limit with per-hop re-validation, and
  credential-bearing headers are stripped when a redirect crosses origin.

Known limitation: the hostname is resolved before connecting (no connect-to-
validated-IP pinning), so a DNS-rebinding attacker could in principle race the
check. This is documented rather than claimed away.
"""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from backend.services.secrets import is_single_reference

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


class HttpFetchError(RuntimeError):
    pass


def _is_blocked_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_check(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise HttpFetchError(f"could not resolve host {hostname!r}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise HttpFetchError(f"could not resolve host {hostname!r}")
    for address in addresses:
        if _is_blocked_address(address):
            raise HttpFetchError(f"refusing to connect to non-public address {address}")


def _check_headers(headers: dict[str, str], resolver) -> dict[str, str]:
    clean: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if name.lower() in FORBIDDEN_REQUEST_HEADERS:
            if not (resolver is not None and is_single_reference(value)):
                raise HttpFetchError(f"header {name!r} is not allowed with a literal value")
        clean[name] = value
    return clean


def fetch(arguments: dict, ctx) -> str:
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
    for hop in range(MAX_REDIRECTS + 1):
        parsed = urlsplit(current)
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise HttpFetchError(f"scheme {parsed.scheme!r} is not allowed")
        if not parsed.hostname:
            raise HttpFetchError("url has no host")
        _resolve_and_check(parsed.hostname)

        with httpx.Client(follow_redirects=False, timeout=TIMEOUT_SECONDS) as client:
            with client.stream(method, current, headers=headers, content=body_bytes) as response:
                if response.is_redirect and hop < MAX_REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        break
                    nxt = urljoin(current, location)
                    if _cross_origin(current, nxt):
                        headers = {
                            name: value
                            for name, value in headers.items()
                            if name.lower() not in FORBIDDEN_REQUEST_HEADERS
                        }
                    current = nxt
                    continue
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
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
    return (pa.scheme, pa.hostname, pa.port) != (pb.scheme, pb.hostname, pb.port)


def _render(response: httpx.Response, raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    if "text/html" in response.headers.get("content-type", "").lower():
        parser = _ReadableHTML()
        parser.feed(text)
        text = "\n".join(
            line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
        )
        text = "[Readable HTML; scripts and styles omitted]\n" + text
    header_lines = "\n".join(f"{k}: {v}" for k, v in response.headers.items())
    return f"HTTP {response.status_code}\n{header_lines}\n\n{text}"


class _ReadableHTML(HTMLParser):
    """Cheap readable text for static pages; never executes scripts or fetches links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []
        self.links = []

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
            self.links.append(href if href.startswith(("http://", "https://", "/")) else "")

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
