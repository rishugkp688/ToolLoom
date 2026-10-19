"""What the model can actually run on this machine.

Without this the agent spends turns discovering its own environment — running
`node --version`, `which npx`, listing caches — before it can start.

These facts are probed **fresh on every context build**, not cached at startup.
A snapshot would keep claiming "not installed" after the operator installed
something, which is worse than saying nothing. `shutil.which` is a handful of
PATH lookups per turn; correctness is worth more than that.
"""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

_PROBES = (
    ("bash", "shell"),
    ("python3", "python"),
    ("node", "node"),
    ("npm", "npm"),
    ("npx", "npx"),
    ("git", "git"),
)


def _browser_automation() -> bool:
    if shutil.which("playwright") or shutil.which("puppeteer"):
        return True
    cache = Path.home() / ".cache" / "ms-playwright"
    try:
        return cache.is_dir() and any(cache.iterdir())
    except OSError:
        return False


def _static_facts() -> tuple[str, ...]:
    """Probed live: an installed tool must show up on the next turn, not next boot."""
    facts = [f"os: {platform.system()} {platform.release()}"]
    for executable, label in _PROBES:
        found = shutil.which(executable)
        facts.append(f"{label}: {found or 'not installed'}")
    facts.append(
        "browser automation: "
        + ("installed" if _browser_automation() else "not installed")
    )
    return tuple(facts)


def describe(workspace_root: str | None = None) -> str:
    lines = ["<environment>", *_static_facts()]
    if workspace_root:
        lines.append(
            f"workspace: {workspace_root} (default working directory; files here are kept)"
        )
        lines.append(
            f"scratch: {workspace_root}/.tmp (hidden from listings; use for temporary artifacts)"
        )
    lines.append("These facts are current; do not spend a turn re-probing them.")
    lines.append("</environment>")
    return "\n".join(lines)
