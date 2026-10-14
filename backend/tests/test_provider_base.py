from __future__ import annotations

from backend.runtime.observation import truncate_head_tail
from backend.services.providers.base import (
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    block_from_dict,
    block_to_dict,
)


def test_block_roundtrip():
    blocks = [
        TextBlock(text="hello"),
        ToolCallBlock(id="abc", name="demo__add", arguments={"a": 1, "b": 2}),
        ToolResultBlock(tool_call_id="abc", content="3", is_error=False, name="demo__add"),
    ]
    for block in blocks:
        restored = block_from_dict(block_to_dict(block))
        assert block_to_dict(restored) == block_to_dict(block)


def test_truncate_head_and_tail_preserves_both_ends():
    text = "HEAD" + ("x" * 5000) + "TAIL"
    trimmed, truncated = truncate_head_tail(text, max_chars=1000)
    assert truncated is True
    assert trimmed.startswith("HEAD")
    assert trimmed.endswith("TAIL")
    assert "truncated" in trimmed


def test_truncate_noop_when_small():
    trimmed, truncated = truncate_head_tail("short", max_chars=1000)
    assert trimmed == "short"
    assert truncated is False
