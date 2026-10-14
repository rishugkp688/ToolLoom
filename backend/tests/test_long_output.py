from __future__ import annotations

from backend.services.providers.base import ToolCallBlock
from backend.tests.helpers import enable_tool


async def test_long_tool_output_is_truncated_and_spilled(client):
    enable_tool(client, "sandbox", "fs_write")
    enable_tool(client, "sandbox", "fs_read")
    executor = client.app.state.tool_executor

    big = "x" * 50_000
    written = await executor.execute(
        ToolCallBlock("c1", "sandbox__fs_write", {"path": "big.txt", "content": big}),
        "conv-long",
    )
    assert written.is_error is False

    read = await executor.execute(
        ToolCallBlock("c2", "sandbox__fs_read", {"path": "big.txt"}), "conv-long"
    )
    assert read.is_error is False
    assert read.truncated is True
    assert "[TRUNCATED" in read.model_text
    assert ".transcripts/c2.txt" in read.model_text

    spill = client.app.state.settings.workspaces_dir / "conv-long" / ".transcripts" / "c2.txt"
    assert spill.exists()
    assert len(spill.read_text(encoding="utf-8")) == 50_000
