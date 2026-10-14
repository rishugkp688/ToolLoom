"""JSON-with-comments reader for `mcp.json`-style configs.

Strips ``//`` and ``/* */`` comments (string-literal aware) and removes a single
trailing comma before ``}``/``]``. Mirrors what editors like VS Code / Cursor
accept, so an operator can paste their existing config verbatim.
"""

from __future__ import annotations

import json
from typing import Any


def strip_jsonc(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escaped = False
    while i < n:
        char = text[i]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            if i + 1 >= n:
                raise ValueError("unterminated block comment")
            out.append(" ")
            i += 2
            continue
        out.append(char)
        i += 1
    # Remove trailing commas only outside quoted strings.
    clean = "".join(out)
    out = []
    in_string = escaped = False
    for index, char in enumerate(clean):
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif char == ",":
            lookahead = index + 1
            while lookahead < len(clean) and clean[lookahead].isspace():
                lookahead += 1
            if lookahead >= len(clean) or clean[lookahead] not in "}]":
                out.append(char)
        else:
            out.append(char)
    return "".join(out)


def loads(text: str) -> Any:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(strip_jsonc(text), object_pairs_hook=unique_pairs)
