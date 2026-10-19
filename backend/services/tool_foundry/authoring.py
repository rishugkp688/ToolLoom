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

import json
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
    provided = sys.argv[1:] if argv is None else argv
    if provided and provided[0] == "--toolloom-json":
        if len(provided) != 2:
            raise ValueError("--toolloom-json requires one JSON object")
        data = json.loads(provided[1])
        if not isinstance(data, dict):
            raise ValueError("tool arguments must be an object")
        defaults = __DEFAULTS_REPR__
        defaults.update({key.replace("-", "_"): value for key, value in data.items()})
        args = argparse.Namespace(**defaults)
    else:
        args = build_parser().parse_args(provided)
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

_FORMAT_ARGUMENT = """    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="output format (default: json)",
    )
"""

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
        self.assertEqual(json.loads(result.stdout), json.loads(__EXPECTED_REPR__))

    # Keep a case that reproduces the original script's output on a real
    # fixture. That comparison is what proves the conversion, so do not drop it.
'''


def _dest(prop: str) -> str:
    return prop.replace("-", "_")


def _validate_properties(properties: dict[str, Any]) -> None:
    """Reject names argparse cannot represent, before writing a package."""
    flags: dict[str, str] = {}
    dests: dict[str, str] = {}
    for prop in properties:
        if not isinstance(prop, str) or not prop:
            raise FoundryError("property names must be non-empty strings")
        dest = _dest(prop)
        if not dest.isidentifier() or prop.startswith("-"):
            raise FoundryError(
                f"property {prop!r} cannot be mapped to a CLI flag; use letters, "
                "digits, underscores or hyphens, starting with a letter or underscore"
            )
        flag = prop.replace("_", "-")
        if flag in {"help", "toolloom-json"}:
            raise FoundryError(f"property {prop!r} collides with a reserved CLI flag")
        if flag in flags:
            raise FoundryError(
                f"properties {flags[flag]!r} and {prop!r} map to the same --{flag} flag"
            )
        if dest in dests:
            raise FoundryError(
                f"properties {dests[dest]!r} and {prop!r} map to the same argument name"
            )
        flags[flag] = prop
        dests[dest] = prop


def _argparse_line(prop: str, spec: dict[str, Any], required: bool) -> str:
    flag = "--" + prop.replace("_", "-")
    kind = spec.get("type", "string")
    help_text = str(spec.get("description") or prop)
    # repr for both: a description containing a quote, backslash or newline used to
    # produce a CLI that did not compile.
    if kind == "boolean":
        return f'    parser.add_argument({flag!r}, action="store_true", help={help_text!r})\n'
    if kind == "integer":
        signature = "type=int"
    elif kind == "number":
        signature = "type=float"
    elif kind == "object":
        signature = "type=json.loads"
    elif kind == "array":
        item_type = (spec.get("items") or {}).get("type")
        item = {"integer": "int", "number": "float", "object": "json.loads"}.get(item_type)
        signature = f'type={item}, action="append"' if item else 'action="append"'
    else:
        signature = "type=str"
    required_text = "required=True, " if required else ""
    return f"    parser.add_argument({flag!r}, {required_text}{signature}, help={help_text!r})\n"


def _result_line(prop: str) -> str:
    return f"        {prop!r}: getattr(args, {_dest(prop)!r}),\n"


def _sample_arg(prop: str, spec: dict[str, Any]) -> str:
    flag = "--" + prop.replace("_", "-")
    kind = spec.get("type", "string")
    if kind == "boolean":
        return repr(flag)
    if kind == "integer":
        return f'{flag!r}, "1"'
    if kind == "number":
        return f'{flag!r}, "1.0"'
    if kind == "object":
        return f'{flag!r}, "{{}}"'
    if kind == "array":
        return f'{flag!r}, "example"'
    return f'{flag!r}, "REPLACE_ME"'


def _sample_value(spec: dict) -> Any:
    if "const" in spec:
        return spec["const"]
    if spec.get("enum"):
        return spec["enum"][0]
    if "default" in spec:
        return spec["default"]
    kind = spec.get("type", "string")
    if kind == "null":
        return None
    if kind == "boolean":
        return True
    if kind in {"integer", "number"}:
        value = max(1, spec.get("minimum", 1))
        if "exclusiveMinimum" in spec:
            value = max(value, spec["exclusiveMinimum"] + 1)
        value = min(value, spec.get("maximum", value))
        if "exclusiveMaximum" in spec:
            value = min(value, spec["exclusiveMaximum"] - 1)
        return int(value) if kind == "integer" else float(value)
    if kind == "array":
        count = max(1, spec.get("minItems", 0))
        count = min(count, spec.get("maxItems", count))
        return [_sample_value(spec.get("items") or {}) for _ in range(count)]
    if kind == "object":
        return {
            key: _sample_value(value if isinstance(value, dict) else {})
            for key, value in (spec.get("properties") or {}).items()
        }
    text = "example".ljust(spec.get("minLength", 0), "x")
    return text[: spec.get("maxLength", len(text))]


def scaffold_package(workspace, name: str, description: str, input_schema: dict) -> dict:
    """Create a package skeleton with CLI flags matching the declared schema."""
    manifest = FoundryManifest(
        name=name,
        description=description,
        entrypoint=f"scripts/{name}.py",
        input_schema=input_schema,
        argument_encoding="json",
        tests="tests",
    )
    properties: dict[str, Any] = manifest.input_schema.get("properties") or {}
    _validate_properties(properties)
    sample_values = {
        prop: _sample_value(spec if isinstance(spec, dict) else {})
        for prop, spec in properties.items()
    }
    from backend.services.tool_executor import validate_arguments

    if validate_arguments(manifest.input_schema, sample_values):
        raise FoundryError("cannot generate a valid fixture for this schema; simplify constraints")
    package = authoring_package(workspace, manifest.name)
    scripts = package / "scripts"
    tests = package / "tests"
    scripts.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)

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
        .replace(
            "__DEFAULTS_REPR__",
            repr(
                {
                    _dest(prop): False
                    if isinstance(spec, dict) and spec.get("type") == "boolean"
                    else None
                    for prop, spec in properties.items()
                }
            ),
        )
    )
    script_path = package / manifest.entrypoint
    if not script_path.exists():
        script_path.write_text(script, encoding="utf-8")

    sample = f"'--toolloom-json', {json.dumps(sample_values)!r}"
    test_body = (
        _TEST_TEMPLATE.replace("__NAME__", manifest.name)
        .replace("__ENTRYPOINT__", manifest.entrypoint)
        .replace("__CLASS__", manifest.name.title().replace("_", ""))
        .replace("__SAMPLE_ARGS__", sample)
        .replace("__EXPECTED_REPR__", repr(json.dumps(sample_values or {"result": "TODO"})))
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


async def install_from_workspace(workspace, name: str, settings, conversation_id: str) -> dict:
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
        # The installing tool reconciles the catalogue and replaces this with the
        # state that actually resulted. Do not claim a live approval state here: an
        # identical reinstall preserves the operator's existing flags.
        "activation": (
            "Installed into the capability store; the installer reports the "
            "resulting enablement and approval state after reconciliation."
        ),
    }


def load_authoring_manifest(workspace, name: str) -> FoundryManifest:
    return load_manifest(_package_dir(workspace, name))
