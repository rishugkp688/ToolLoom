from __future__ import annotations

from backend.services.context_builder import ContextBuilder, _logical_turns, estimate_tokens
from backend.services.providers.base import (
    ProviderMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


class _Settings:
    model_context_window = 2000
    model_max_output_tokens = 256


def _builder() -> ContextBuilder:
    return ContextBuilder(db=None, settings=_Settings())


def test_logical_turns_group_tool_results_with_their_turn():
    messages = [
        ProviderMessage("user", [TextBlock("hi")]),
        ProviderMessage("assistant", [ToolCallBlock("c1", "t", {})]),
        ProviderMessage("user", [ToolResultBlock("c1", "1")]),
        ProviderMessage("assistant", [TextBlock("done")]),
        ProviderMessage("user", [TextBlock("next")]),
    ]
    turns = _logical_turns(messages)
    assert [len(turn) for turn in turns] == [4, 1]


def test_budget_drops_oldest_turn_and_keeps_latest():
    messages = [
        ProviderMessage("user", [TextBlock("a" * 8000)]),
        ProviderMessage("assistant", [TextBlock("b" * 8000)]),
        ProviderMessage("user", [TextBlock("latest")]),
    ]
    trimmed = _builder()._fit_within_budget(messages, None, [])
    assert len(trimmed) == 1
    assert trimmed[0].text == "latest"


def test_budget_never_drops_the_only_turn():
    messages = [ProviderMessage("user", [TextBlock("x" * 100000)])]
    trimmed = _builder()._fit_within_budget(messages, None, [])
    assert len(trimmed) == 1


def test_estimate_tokens_grows_with_content():
    small = [ProviderMessage("user", [TextBlock("hi")])]
    large = [ProviderMessage("user", [TextBlock("x" * 4000)])]
    assert estimate_tokens(large, None, []) > estimate_tokens(small, None, [])
