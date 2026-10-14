"""Error helpers.

anyio task groups wrap failures in ``ExceptionGroup``; the useful information is
in the innermost cause. ``flatten_error`` unwraps that chain so users see
``ConnectError: ...`` instead of "unhandled errors in a TaskGroup".
"""

from __future__ import annotations


def flatten_error(exc: BaseException) -> BaseException:
    node: BaseException = exc
    seen: set[int] = set()
    while id(node) not in seen:
        seen.add(id(node))
        subs = getattr(node, "exceptions", None)
        if subs:
            node = subs[0]
            continue
        if node.__cause__ is not None:
            node = node.__cause__
            continue
        break
    return node


def is_pre_send_error(exc: BaseException) -> bool:
    """True only when the request provably never reached the server.

    Such failures are safe to retry (a retry cannot double-write). Read timeouts
    and protocol errors are deliberately excluded — the request may have been
    processed.
    """
    import httpx

    flat = flatten_error(exc)
    return isinstance(
        flat, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
    )
