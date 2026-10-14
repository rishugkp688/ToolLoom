from __future__ import annotations

import json
import sys

# A real MCP server run as a fixed child process (command + args only).
STDIO_SERVER_CODE = (
    "from mcp.server.fastmcp import FastMCP\n"
    "server = FastMCP('stdio-demo')\n"
    "@server.tool()\n"
    "def triple(n: int) -> int:\n"
    "    '''Triple an integer.'''\n"
    "    return n * 3\n"
    "server.run(transport='stdio')\n"
)


def register_stdio_demo(client, name: str = "stdio-demo") -> dict:
    response = client.post(
        "/mcp/servers",
        json={
            "name": name,
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-c", STDIO_SERVER_CODE],
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()



def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into a list of ``(event, data)`` tuples."""
    events: list[tuple[str, dict]] = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        event = "message"
        data = ""
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data += line[len("data:") :].strip()
        try:
            payload = json.loads(data) if data else {}
        except json.JSONDecodeError:
            payload = {"_raw": data}
        events.append((event, payload))
    return events


def demo_server(client) -> dict:
    """The opt-in synthetic fixture server (seeded only with TOOLLOOM_SEED_DEMO)."""
    servers = client.get("/mcp/servers").json()
    return next(s for s in servers if s["name"] == "demo")


def sandbox_server(client) -> dict:
    """The one local server a normal installation ships."""
    servers = client.get("/mcp/servers").json()
    return next(s for s in servers if s["name"] == "sandbox")


def enable_tool(client, server_name: str, tool_name: str) -> dict:
    """Enable a discovered tool (all tools start disabled)."""
    server = next(s for s in client.get("/mcp/servers").json() if s["name"] == server_name)
    response = client.patch(
        f"/mcp/servers/{server['id']}/tools/{tool_name}", json={"enabled": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    return server


def enable_demo_add(client) -> dict:
    """Enable the synthetic demo__add fixture tool (disabled by default)."""
    return enable_tool(client, "demo", "add")

