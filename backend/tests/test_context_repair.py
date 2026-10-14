from __future__ import annotations

from backend.services.context_builder import repair_orphan_tool_uses
from backend.services.providers.base import (
    ProviderMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def test_repair_merges_into_following_user_turn():
    messages = [
        ProviderMessage("user", [TextBlock("hi")]),
        ProviderMessage("assistant", [ToolCallBlock("call_1", "demo__add", {"a": 1, "b": 1})]),
        ProviderMessage("user", [TextBlock("are you there?")]),
    ]
    repaired = repair_orphan_tool_uses(messages)
    assert len(repaired) == 3  # merged, not appended
    first_block = repaired[2].blocks[0]
    assert isinstance(first_block, ToolResultBlock)
    assert first_block.tool_call_id == "call_1"
    assert first_block.is_error is True
    assert first_block.content == "[no result available]"
    # The original user text is preserved after the synthetic result.
    assert repaired[2].blocks[-1].text == "are you there?"


def test_repair_appends_when_no_following_turn():
    messages = [
        ProviderMessage("user", [TextBlock("hi")]),
        ProviderMessage("assistant", [ToolCallBlock("call_9", "demo__add", {})]),
    ]
    repaired = repair_orphan_tool_uses(messages)
    assert len(repaired) == 3
    assert repaired[2].role == "user"
    assert repaired[2].blocks[0].tool_call_id == "call_9"


def test_repair_leaves_complete_turns_untouched():
    messages = [
        ProviderMessage("assistant", [ToolCallBlock("call_1", "demo__add", {})]),
        ProviderMessage("user", [ToolResultBlock("call_1", "2")]),
    ]
    repaired = repair_orphan_tool_uses(messages)
    assert len(repaired) == 2
    assert repaired[1].blocks[0].content == "2"
