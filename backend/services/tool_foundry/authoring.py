"""The three authoring operations behind the sandbox's foundry tools.

* ``scaffold`` writes a package skeleton whose CLI flags already match the
  declared schema, so the agent fills in logic rather than inventing an
  interface.
* ``validate`` runs the package's tests without installing anything.
* ``install`` validates again, then copies the package and registers it.

Nothing here runs automatically. The agent reaches these only when the operator
asked for a capability, and every installed tool lands disabled and unapproved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.services.tool_foundry.manifest import (
    MANIFEST_FILENAME,
    FoundryError,
    FoundryManifest,
    load_manifest,
    save_manifest,
)
from backend.services.tool_foundry.store import (
    authoring_package,
    install_package,
)
from backend.services.tool_foundry.validator import run_tests, validate_package

TEST_OUTPUT_CHARS = 3_000

_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
__DOCSTRING__

from __future__ import annotations

import argparse
import json
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=__NAME_REPR__,
        description=__DESCRIPTION_REPR__,
    )
__ARGUMENTS____FORMAT_ARG__    return parser


def run(args: argparse.Namespace) -> dict:
    """Compute the result. Replace this body with the real implementation."""
    return {
__RESULT_KEYS__    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args)
    if getattr(args, "format", "json") == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_FORMAT_ARGUMENT = '''    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="output format (default: json)",
    )
'''

_TEST_TEMPLATE = '''"""Tests for __NAME__.

Run them exactly as Tool Foundry does:
    python -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "__ENTRYPOINT__"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT), *args],
        capture_output=True,
        text=True,
    )


class Test__CLASS__(unittest.TestCase):
    def test_help_exits_zero(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_returns_json_object(self) -> None:
        result = run_cli(__SAMPLE_ARGS__)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)

    # Keep a case that reproduces the original script's output on a real
    # fixture. That comparison is what proves the conversion, so do not drop it.
'''


def _argparse_line(prop: str, spec: dict[str, Any], required: bool) -> str:
    flag = "--" + prop.replace("_", "-")
    kind = spec.get("type", "string")
    help_text = str(spec.get("description") or prop)
    # repr for both: a description containing a quote, backslash or newline used to
    # produce a CLI that did not compile.
    if kind == "boolean":
        return f"    parser.add_argument({flag!r}, action=\"store_true\", help={help_text!r})\n"
    if kind == "integer":
        signature = "type=int"
    elif kind == "number":
        signature = "type=float"
    elif kind == "array":
        signature = 'action="append"'
    else:
        signature = "type=str"
    required_text = "required=True, " if required else ""
    return f"    parser.add_argument({flag!r}, {required_text}{signature}, help={help_text!r})\n"


def _result_line(prop: str) -> str:
    return f'        "{prop}": getattr(args, "{prop.replace("-", "_")}"),\n'


def _sample_arg(prop: str, spec: dict[str, Any]) -> str:
    flag = '"--' + prop.replace("_", "-") + '"'
    kind = spec.get("type", "string")
    if kind == "boolean":
        return flag
    if kind == "integer":
        return f"{flag}, \"1\""
    if kind == "number":
        return f"{flag}, \"1.0\""
    if kind == "array":
        return f"{flag}, \"example\""
    return f'{flag}, "REPLACE_ME"'


def scaffold_package(workspace, name: str, description: str, input_schema: dict) -> dict:
    """Create a package skeleton with CLI flags matching the declared schema."""
    manifest = FoundryManifest(
        name=name,
        description=description,
        entrypoint=f"scripts/{name}.py",
        input_schema=input_schema,
        tests="tests",
    )
    package = authoring_package(workspace, manifest.name)
    scripts = package / "scripts"
    tests = package / "tests"
    scripts.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)

    properties: dict[str, Any] = manifest.input_schema.get("properties") or {}
    required = set(manifest.input_schema.get("required") or [])
    arguments = "".join(
        _argparse_line(prop, spec if isinstance(spec, dict) else {}, prop in required)
        for prop, spec in properties.items()
    )
    result_keys = "".join(_result_line(prop) for prop in properties)
    script = (
        _SCRIPT_TEMPLATE.replace(
            "__DOCSTRING__",
            repr(
                manifest.description
                + "\n\nGenerated by ToolLoom Tool Foundry. Command-line flags map one "
                "to one onto the manifest inputSchema, with underscores written as hyphens."
            ),
        )
        .replace("__NAME_REPR__", repr(manifest.name))
        .replace("__DESCRIPTION_REPR__", repr(manifest.description))
        .replace("__FORMAT_ARG__", "" if "format" in properties else _FORMAT_ARGUMENT)
        .replace("__ARGUMENTS__", arguments)
        .replace("__RESULT_KEYS__", result_keys or '        "result": "TODO",\n')
    )
    script_path = package / manifest.entrypoint
    if not script_path.exists():
        script_path.write_text(script, encoding="utf-8")

    sample = ", ".join(
        _sample_arg(prop, spec if isinstance(spec, dict) else {})
        for prop, spec in properties.items()
    )
    test_body = (
        _TEST_TEMPLATE.replace("__NAME__", manifest.name)
        .replace("__ENTRYPOINT__", manifest.entrypoint)
        .replace("__CLASS__", manifest.name.title().replace("_", ""))
        .replace("__SAMPLE_ARGS__", sample)
    )
    test_path = tests / f"test_{manifest.name}.py"
    if not test_path.exists():
        test_path.write_text(test_body, encoding="utf-8")

    manifest_path = package / MANIFEST_FILENAME
    existed = manifest_path.exists()
    if not existed:
        save_manifest(package, manifest)

    return {
        "package_directory": package.relative_to(Path(workspace.root)).as_posix(),
        "manifest_path": manifest_path.relative_to(Path(workspace.root)).as_posix(),
        "entrypoint_path": script_path.relative_to(Path(workspace.root)).as_posix(),
        "tests_path": test_path.relative_to(Path(workspace.root)).as_posix(),
        "flag_convention": "underscores become hyphens: input_schema -> --input-schema",
        "unchanged": (
            f"{manifest.name} already existed, so existing files were left alone. "
            "Delete the directory first to start over."
            if existed
            else None
        ),
    }


def _package_dir(workspace, name: str) -> Path:
    package = authoring_package(workspace, name)
    if not package.is_dir():
        raise FoundryError(
            f"no package named {name!r} in the workspace. Call foundry_scaffold first."
        )
    return package


async def validate_package_async(workspace, name: str, settings) -> dict:
    package = _package_dir(workspace, name)
    report = validate_package(package)
    if not report.ok or report.manifest is None:
        return {"ok": False, "stage": "structure", "problems": report.problems}
    tests = await run_tests(package, report.manifest, settings)
    return {
        "ok": tests.ok,
        "stage": "tests",
        "manifest": report.manifest.name,
        "version": report.manifest.version,
        "tests_run": tests.tests_run,
        "exit_code": tests.exit_code,
        "stop_reason": tests.stop_reason,
        "summary": tests.summary(),
        "output": tests.output[-TEST_OUTPUT_CHARS:],
    }


async def install_from_workspace(
    workspace, name: str, settings, conversation_id: str
) -> dict:
    package = _package_dir(workspace, name)
    report = validate_package(package)
    if not report.ok or report.manifest is None:
        return {"ok": False, "stage": "structure", "problems": report.problems}
    tests = await run_tests(package, report.manifest, settings)
    if not tests.ok:
        return {
            "ok": False,
            "stage": "tests",
            "summary": tests.summary(),
            "output": tests.output[-TEST_OUTPUT_CHARS:],
            "installed": False,
        }
    installed = install_package(
        settings,
        package,
        report.manifest,
        source_conversation=conversation_id,
        tests_passed=tests.tests_run,
    )
    return {
        "ok": True,
        "installed": True,
        "name": installed.name,
        "version": installed.version,
        "tool": installed.tool_name,
        "entrypoint": report.manifest.entrypoint,
        "sha256": installed.sha256,
        # Approval binds to this: replacing the package revokes the old approval.
        "digest": installed.digest,
        "tests_run": tests.tests_run,
        "directory": installed.directory.as_posix(),
        "activation": (
            f"{installed.tool_name} is installed but DISABLED and NOT APPROVED. "
            "The operator must enable and approve it in Settings -> Capabilities "
            "before any model can call it."
        ),
    }


def load_authoring_manifest(workspace, name: str) -> FoundryManifest:
    return load_manifest(_package_dir(workspace, name))
