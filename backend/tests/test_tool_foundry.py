"""Tool Foundry: authoring, validation, installation and generated execution.

Everything here runs offline. The tests drive the real tools through
``ToolExecutor``, so what they cover is the path a conversation actually takes.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.services.providers.base import ToolCallBlock
from backend.services.tool_foundry.manifest import FoundryError, FoundryManifest, load_manifest
from backend.services.tool_foundry.runner import build_argv
from backend.services.tool_foundry.store import list_installed

SCHEMA = {
    "type": "object",
    "properties": {
        "input": {"type": "string", "description": "JSONL file to read"},
        "group_by": {"type": "string", "description": "field to group by"},
        "limit": {"type": "integer", "description": "maximum rows"},
        "verbose": {"type": "boolean", "description": "extra detail"},
    },
    "required": ["input"],
    "additionalProperties": False,
}


def _call(client, name: str, arguments: dict, conversation_id: str = "conv-foundry"):
    return asyncio.run(
        client.app.state.tool_executor.execute(ToolCallBlock("call_1", name, arguments), conversation_id)
    )


def _json(result):
    return json.loads(result.output_text)


def _enable_foundry_tools(client) -> None:
    """Foundry tools obey default-deny like everything else, so enable them first."""
    server = next(s for s in client.get("/mcp/servers").json() if s["name"] == "sandbox")
    for tool in ("foundry_scaffold", "foundry_validate", "foundry_install"):
        body = {"enabled": True}
        if tool != "foundry_scaffold":
            body["approved"] = True  # high risk
        response = client.patch(f"/mcp/servers/{server['id']}/tools/{tool}", json=body)
        assert response.status_code == 200, response.text


@contextmanager
def _foundry_client(make_app, **overrides):
    with TestClient(
        make_app(toolloom_allow_foundry=True, **overrides), base_url="http://127.0.0.1:8000"
    ) as client:
        _enable_foundry_tools(client)
        yield client


def _scaffold(client, name: str = "analyze_logs", schema: dict | None = None):
    return _call(
        client,
        "sandbox__foundry_scaffold",
        {
            "name": name,
            "description": "Analyze security event logs",
            "input_schema": schema or SCHEMA,
        },
    )


def _approve_tool(client, tool_name: str) -> None:
    server = next(s for s in client.get("/mcp/servers").json() if s["name"] == "generated")
    response = client.patch(
        f"/mcp/servers/{server['id']}/tools/{tool_name}",
        json={"enabled": True, "approved": True},
    )
    assert response.status_code == 200, response.text


# --- manifest ----------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"name": "Bad-Name"}, "name must start with a letter"),
        ({"version": "one"}, "version must look like"),
        ({"description": ""}, "description is required"),
        ({"entrypoint": "/etc/passwd.py"}, "must be a relative path"),
        ({"entrypoint": "../escape.py"}, "must be a relative path"),
        ({"entrypoint": "scripts/tool.txt"}, "must be a .py file"),
        ({"input_schema": {"type": "array"}}, 'must have "type": "object"'),
        ({"input_schema": {"type": "object"}}, 'must define a "properties" object'),
        (
            {"input_schema": {"type": "object", "properties": {}, "required": ["nope"]}},
            "required name(s) missing",
        ),
    ],
)
def test_manifest_rejects_bad_input(overrides, message):
    payload = {
        "name": "valid_name",
        "version": "1.0.0",
        "description": "does something",
        "entrypoint": "scripts/tool.py",
        "inputSchema": SCHEMA,
    }
    if "input_schema" in overrides:
        # The manifest accepts either the alias or the field name, so replace
        # rather than supplying both.
        payload.pop("inputSchema")
    payload.update(overrides)
    with pytest.raises(Exception) as excinfo:
        FoundryManifest.model_validate(payload)
    assert message in str(excinfo.value)


def test_manifest_roundtrips_through_disk(tmp_path):
    from backend.services.tool_foundry.manifest import save_manifest

    manifest = FoundryManifest(
        name="round_trip", description="round trip", entrypoint="scripts/tool.py", input_schema=SCHEMA
    )
    save_manifest(tmp_path, manifest)
    assert load_manifest(tmp_path).input_schema == SCHEMA
    assert json.loads((tmp_path / "manifest.json").read_text())["inputSchema"] == SCHEMA


def test_load_manifest_reports_a_missing_file(tmp_path):
    with pytest.raises(FoundryError, match="not found"):
        load_manifest(tmp_path)


# --- scaffolding -------------------------------------------------------------

def test_scaffold_writes_the_expected_layout(make_app):
    with _foundry_client(make_app) as client:
        result = _json(_scaffold(client))
        assert result["package_directory"] == "foundry/analyze_logs"
        assert result["entrypoint_path"] == "foundry/analyze_logs/scripts/analyze_logs.py"
        assert result["tests_path"] == "foundry/analyze_logs/tests/test_analyze_logs.py"
        workspace = client.app.state.settings.workspaces_dir / "conv-foundry"
        assert (workspace / result["manifest_path"]).is_file()
        assert (workspace / result["entrypoint_path"]).is_file()
        assert (workspace / result["tests_path"]).is_file()


def test_scaffold_generates_flags_matching_the_schema(make_app):
    with _foundry_client(make_app) as client:
        script = Path(client.app.state.settings.workspaces_dir) / "conv-foundry"
        _scaffold(client)
        text = (script / "foundry/analyze_logs/scripts/analyze_logs.py").read_text()
        assert '"--group-by"' in text
        assert '"--input", required=True' in text
        assert "type=int" in text
        assert 'action="append"' not in text  # no array in this schema


def test_scaffold_leaves_existing_files_alone(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        workspace = Path(client.app.state.settings.workspaces_dir) / "conv-foundry"
        entry = workspace / "foundry/analyze_logs/scripts/analyze_logs.py"
        entry.write_text("# my own edit\n", encoding="utf-8")
        result = _json(_scaffold(client))
        assert result["unchanged"] is not None
        assert entry.read_text(encoding="utf-8") == "# my own edit\n"


def test_scaffold_rejects_a_bad_schema(make_app):
    with _foundry_client(make_app) as client:
        result = _call(
            client,
            "sandbox__foundry_scaffold",
            {
                "name": "broken",
                "description": "bad schema",
                "input_schema": {"type": "array", "items": {}},
            },
        )
        assert result.is_error is True
        assert "type" in result.model_text


def test_scaffold_requires_the_foundry_flag(make_app):
    """The tools can be enabled, but the feature flag still refuses to work."""
    with TestClient(make_app(), base_url="http://127.0.0.1:8000") as client:
        _enable_foundry_tools(client)
        result = _call(
            client,
            "sandbox__foundry_scaffold",
            {"name": "nope", "description": "d", "input_schema": SCHEMA},
        )
        assert result.is_error is True
        assert "TOOLLOOM_ALLOW_FOUNDRY" in result.model_text


# --- validation --------------------------------------------------------------

def test_validate_passes_on_a_fresh_scaffold(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        report = _json(_call(client, "sandbox__foundry_validate", {"name": "analyze_logs"}))
        assert report["ok"] is True
        assert report["stage"] == "tests"
        assert report["tests_run"] >= 1


def test_validate_reports_failing_tests(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        workspace = Path(client.app.state.settings.workspaces_dir) / "conv-foundry"
        test_file = workspace / "foundry/analyze_logs/tests/test_analyze_logs.py"
        test_file.write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_fails(self):\n        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )
        result = _call(client, "sandbox__foundry_validate", {"name": "analyze_logs"})
        assert result.is_error is True
        report = _json(result)
        assert report["ok"] is False
        assert report["tests_run"] >= 1


def test_validate_rejects_a_package_with_no_tests(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        workspace = Path(client.app.state.settings.workspaces_dir) / "conv-foundry"
        (workspace / "foundry/analyze_logs/tests/test_analyze_logs.py").unlink()
        result = _call(client, "sandbox__foundry_validate", {"name": "analyze_logs"})
        assert result.is_error is True
        assert "no test_*.py" in result.model_text


def test_validate_reports_an_unknown_package(make_app):
    with _foundry_client(make_app) as client:
        result = _call(client, "sandbox__foundry_validate", {"name": "ghost"})
        assert result.is_error is True
        assert "foundry_scaffold" in result.model_text


def test_validate_blocks_a_tampered_manifest(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        workspace = Path(client.app.state.settings.workspaces_dir) / "conv-foundry"
        (workspace / "foundry/analyze_logs/manifest.json").write_text("{", encoding="utf-8")
        result = _call(client, "sandbox__foundry_validate", {"name": "analyze_logs"})
        assert result.is_error is True
        assert "valid JSON" in result.model_text


# --- installation ------------------------------------------------------------

def test_install_registers_a_disabled_unapproved_tool(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        report = _json(_call(client, "sandbox__foundry_install", {"name": "analyze_logs"}))
        assert report["installed"] is True
        assert report["tool"] == "generated__analyze_logs"
        assert report["tests_run"] >= 1
        assert "DISABLED and NOT APPROVED" in report["activation"]

        server = next(s for s in client.get("/mcp/servers").json() if s["name"] == "generated")
        tool = next(
            t
            for t in client.get(f"/mcp/servers/{server['id']}/tools").json()
            if t["name"] == "analyze_logs"
        )
        assert tool["enabled"] is False
        assert tool["approved"] is False
        assert tool["risk"] == "high"
        assert tool["input_schema"]["required"] == ["input"]


def test_install_lands_outside_the_agent_workspace(make_app):
    """A later run must not be able to edit a package the operator approved."""
    with _foundry_client(make_app) as client:
        _scaffold(client)
        report = _json(_call(client, "sandbox__foundry_install", {"name": "analyze_logs"}))
        settings = client.app.state.settings
        installed = Path(report["directory"]).resolve()
        workspace = (Path(settings.workspaces_dir) / "conv-foundry").resolve()
        assert workspace not in installed.parents
        assert (Path(settings.data_dir) / "tools").resolve() in installed.parents
        assert installed.is_dir()


def test_install_refuses_when_tests_fail(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        workspace = Path(client.app.state.settings.workspaces_dir) / "conv-foundry"
        (workspace / "foundry/analyze_logs/tests/test_analyze_logs.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_fails(self):\n        assert False\n",
            encoding="utf-8",
        )
        result = _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        assert result.is_error is True
        assert _json(result)["installed"] is False
        assert list_installed(client.app.state.settings) == []


# --- generated execution -----------------------------------------------------

def test_installed_tool_runs_through_the_executor(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        _approve_tool(client, "analyze_logs")

        result = _call(
            client,
            "generated__analyze_logs",
            {"input": "security-events.jsonl", "group_by": "source_ip", "limit": 5},
        )
        assert result.is_error is False, result.model_text
        assert _json(result) == {
            "input": "security-events.jsonl",
            "group_by": "source_ip",
            "limit": 5,
            # argparse store_true defaults to False; the flag was not passed.
            "verbose": False,
        }


def test_generated_tool_refuses_until_enabled_and_approved(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        result = _call(client, "generated__analyze_logs", {"input": "x.jsonl"})
        assert result.is_error is True
        assert "disabled" in result.model_text


def test_generated_tool_refuses_after_tampering(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        report = _json(_call(client, "sandbox__foundry_install", {"name": "analyze_logs"}))
        _approve_tool(client, "analyze_logs")
        entrypoint = Path(report["directory"]) / "scripts/analyze_logs.py"
        entrypoint.write_text("# tampered\n", encoding="utf-8")

        result = _call(client, "generated__analyze_logs", {"input": "x.jsonl"})
        assert result.is_error is True
        assert "no longer matches the hash" in result.model_text


def test_generated_tool_reports_a_non_zero_exit(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        workspace = Path(client.app.state.settings.workspaces_dir) / "conv-foundry"
        package = workspace / "foundry/analyze_logs"
        # A CLI that fails, with a test that expects the failure, so the
        # package is still installable and the error path is what gets tested.
        (package / "scripts/analyze_logs.py").write_text(
            "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n", encoding="utf-8"
        )
        (package / "tests/test_analyze_logs.py").write_text(
            "import subprocess\nimport sys\nimport unittest\nfrom pathlib import Path\n\n"
            "ENTRY = Path(__file__).resolve().parents[1] / 'scripts/analyze_logs.py'\n\n\n"
            "class TestExit(unittest.TestCase):\n"
            "    def test_exit_code_is_three(self):\n"
            "        done = subprocess.run([sys.executable, str(ENTRY)], capture_output=True, text=True)\n"
            "        self.assertEqual(done.returncode, 3)\n",
            encoding="utf-8",
        )
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        _approve_tool(client, "analyze_logs")

        result = _call(client, "generated__analyze_logs", {"input": "x.jsonl"})
        assert result.is_error is True
        assert "exited with code 3" in result.model_text
        assert "boom" in result.model_text


def test_generated_tool_validates_arguments_before_running(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        _approve_tool(client, "analyze_logs")
        result = _call(client, "generated__analyze_logs", {"limit": "many"})
        assert result.is_error is True
        assert "invalid tool arguments" in result.model_text


# --- argument mapping --------------------------------------------------------

def test_build_argv_maps_arguments_in_a_stable_order(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client, schema={
            "type": "object",
            "properties": {
                "tags": {"type": "array"},
                "flag": {"type": "boolean"},
                "count": {"type": "integer"},
            },
            "additionalProperties": False,
        })
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        package = list_installed(client.app.state.settings)[0]
        argv = build_argv(package, {"tags": ["a", "b"], "flag": True, "count": 2})
        # argv starts with: interpreter, -s, -u, -X, utf8, entrypoint.
        assert argv[6:] == ["--count", "2", "--flag", "--tags", "a", "--tags", "b"]


# --- capabilities API --------------------------------------------------------

def test_capabilities_api_lists_with_evidence(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        payload = client.get("/capabilities").json()
        assert payload["foundry_enabled"] is True
        assert payload["server_id"]
        entry = payload["capabilities"][0]
        assert entry["name"] == "analyze_logs"
        assert entry["tool"] == "generated__analyze_logs"
        assert entry["version"] == "1.0.0"
        assert entry["integrity_ok"] is True
        assert entry["tests_passed"] >= 1
        assert entry["source_conversation"] == "conv-foundry"
        assert entry["permissions"] == {"filesystem": "workspace", "network": False}
        assert entry["enabled"] is False and entry["approved"] is False


def test_capabilities_api_serves_source_for_review(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        payload = client.get("/capabilities/analyze_logs/source").json()
        assert "manifest.json" in payload["files"]
        assert "scripts/analyze_logs.py" in payload["files"]
        assert any(key.startswith("tests/") for key in payload["files"])
        assert "argparse" in payload["files"]["scripts/analyze_logs.py"]


def test_capabilities_api_removes_the_package_and_its_tool(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
        assert client.delete("/capabilities/analyze_logs").status_code == 204
        assert client.get("/capabilities").json()["capabilities"] == []
        server = next(s for s in client.get("/mcp/servers").json() if s["name"] == "generated")
        assert client.get(f"/mcp/servers/{server['id']}/tools").json() == []
        assert client.get("/capabilities/analyze_logs/source").status_code == 404


def test_capabilities_api_reports_a_missing_capability(make_app):
    with _foundry_client(make_app) as client:
        assert client.delete("/capabilities/ghost").status_code == 404


# --- registry integration ----------------------------------------------------

def test_generated_is_runtime_owned(make_app):
    with _foundry_client(make_app) as client:
        created = client.post(
            "/mcp/servers", json={"name": "generated", "transport": "http", "url": "http://x/mcp"}
        )
        assert created.status_code == 400
        assert "reserved" in created.json()["detail"]

        server = next(s for s in client.get("/mcp/servers").json() if s["name"] == "generated")
        assert client.delete(f"/mcp/servers/{server['id']}").status_code == 400
        assert client.patch(f"/mcp/servers/{server['id']}", json={"url": "http://y"}).status_code == 400
        assert client.patch(f"/mcp/servers/{server['id']}", json={"enabled": False}).status_code == 200


def test_installed_capability_survives_a_restart(make_app):
    with _foundry_client(make_app) as client:
        _scaffold(client)
        _call(client, "sandbox__foundry_install", {"name": "analyze_logs"})
    # A fresh app against the same data dir must re-register what is on disk.
    with _foundry_client(make_app) as second:
        names = [s["name"] for s in second.get("/capabilities").json()["capabilities"]]
        assert names == ["analyze_logs"]
        server = next(s for s in second.get("/mcp/servers").json() if s["name"] == "generated")
        tools = [t["name"] for t in second.get(f"/mcp/servers/{server['id']}/tools").json()]
        assert tools == ["analyze_logs"]
