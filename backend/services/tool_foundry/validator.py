"""Package validation and the test gate.

Stage 1's acceptance is that the CLI reproduces the original behaviour. The
tests are the evidence, so this module refuses to call a package valid unless
tests actually ran and passed.

The runner uses the standard library ``unittest`` rather than pytest, because a
capability has to validate on any install, and pytest is a development
dependency here rather than a runtime one. A package with zero discovered tests
fails: an empty suite proves nothing.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from backend.services.sandbox._exec import minimal_env, run_argv
from backend.services.tool_foundry.manifest import FoundryError, FoundryManifest, load_manifest

MIN_TESTS = 1
_RAN = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)


@dataclass
class ValidationReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    manifest: FoundryManifest | None = None

    def summary(self) -> str:
        if self.ok and self.manifest is not None:
            return f"{self.manifest.name} {self.manifest.version} looks structurally valid"
        return "; ".join(self.problems) or "invalid package"


@dataclass
class TestReport:
    ok: bool
    tests_run: int
    exit_code: int | None
    stop_reason: str
    output: str

    def summary(self) -> str:
        if self.stop_reason == "timeout":
            return "tests timed out"
        if self.stop_reason == "output_limit":
            return "tests exceeded the output limit"
        if self.tests_run == 0:
            return "no tests were discovered"
        return f"{self.tests_run} test(s) run, exit code {self.exit_code}"


def validate_package(package_dir: Path) -> ValidationReport:
    """Structural checks only. Nothing is executed here."""
    root = Path(package_dir).resolve()
    if not root.is_dir():
        return ValidationReport(False, [f"package directory not found: {package_dir}"])
    try:
        manifest = load_manifest(root)
    except FoundryError as exc:
        return ValidationReport(False, [str(exc)], None)

    problems: list[str] = []

    entrypoint = (root / manifest.entrypoint).resolve()
    if root not in entrypoint.parents:
        problems.append("entrypoint escapes the package directory")
    elif not entrypoint.is_file():
        problems.append(f"entrypoint not found: {manifest.entrypoint}")

    tests = (root / manifest.tests).resolve()
    if root not in tests.parents:
        problems.append("tests path escapes the package directory")
    elif not tests.exists():
        problems.append(f"tests path not found: {manifest.tests}")
    else:
        candidates = [tests] if tests.is_file() else sorted(tests.glob("test_*.py"))
        if not candidates:
            problems.append(f"no test_*.py files found under {manifest.tests}")

    return ValidationReport(not problems, problems, manifest)


async def run_tests(package_dir: Path, manifest: FoundryManifest, settings) -> TestReport:
    """Execute the package's test suite with the standard library runner."""
    argv = [
        sys.executable,
        "-s",
        "-u",
        "-X",
        "utf8",
        "-m",
        "unittest",
        "discover",
        "-s",
        manifest.tests,
        "-p",
        "test_*.py",
        "-v",
    ]
    result = await run_argv(
        argv,
        cwd=str(Path(package_dir).resolve()),
        env=minimal_env(),
        timeout=float(settings.toolloom_foundry_test_timeout_seconds),
        max_bytes=settings.toolloom_foundry_output_bytes,
    )
    combined = f"{result.stdout}{result.stderr}".strip()
    match = _RAN.search(combined)
    tests_run = int(match.group(1)) if match else 0
    return TestReport(
        ok=result.ok and tests_run >= MIN_TESTS,
        tests_run=tests_run,
        exit_code=result.exit_code,
        stop_reason=result.stop_reason,
        output=combined,
    )
