"""Static checks on the frontend assets.

These live in the Python suite on purpose: the frontend has no build step and no
test runner of its own, so a mistake here is otherwise only visible by loading the
page. Each check below pins something that fails *silently* in a browser — an
undefined custom property makes a declaration compute to nothing, a missing
element id throws only when that code path runs, and a mistyped icon name renders
an empty box.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
JS = (FRONTEND / "app.js").read_text(encoding="utf-8")

# Exercises the real groupByServer out of app.js rather than a copy of it, because
# the frontend has no test runner of its own.
GROUP_CHECK_JS = """
const fs = require("fs");
const src = fs.readFileSync("app.js", "utf8");
const match = src.match(/function groupByServer\\(tools\\) \\{[\\s\\S]*?\\n\\}/);
if (!match) { console.error("groupByServer not found in app.js"); process.exit(1); }
const groupByServer = eval("(" + match[0] + ")");

const tools = [
  { namespaced_name: "sandbox__fs_read", server_name: "sandbox", risk: "low" },
  { namespaced_name: "sandbox__web_search", server_name: "sandbox", risk: "high" },
  { namespaced_name: "generated__analyze_logs", server_name: "generated", risk: "high" },
  { namespaced_name: "github__search", server_name: "github", risk: "high" },
  { namespaced_name: "mystery__ping", risk: "low" },
];

const grouped = groupByServer(tools);
const problems = [];
const keys = [...grouped.keys()];
if (keys.join(",") !== "sandbox,generated,github,unknown source") {
  problems.push("group order/keys: " + keys.join(","));
}
if (grouped.get("sandbox").length !== 2) problems.push("sandbox group size");
if (grouped.get("github")[0].namespaced_name !== "github__search") {
  problems.push("github membership");
}

const labels = [];
for (const [serverName, group] of grouped) {
  for (const tool of group) {
    labels.push(
      tool.namespaced_name.startsWith(serverName + "__")
        ? tool.namespaced_name.slice(serverName.length + 2)
        : tool.namespaced_name
    );
  }
}
for (const expected of ["fs_read", "web_search", "analyze_logs", "search"]) {
  if (!labels.includes(expected)) problems.push("row label missing: " + expected);
}
// A name whose prefix does not match its group keeps the full name.
if (!labels.includes("mystery__ping")) problems.push("unmatched prefix was truncated");

if (problems.length) { console.error(problems.join("; ")); process.exit(1); }
console.log("ok: " + labels.join(", "));
"""


def _without_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _icon_paths() -> dict[str, str]:
    block = re.search(r"const ICON_PATHS = \{(.*?)\n\};", JS, re.DOTALL)
    assert block, "ICON_PATHS table not found in app.js"
    return dict(re.findall(r"^\s*([A-Za-z]+):\s*'(.*?)',\s*$", block.group(1), re.MULTILINE))


def test_every_css_custom_property_is_defined():
    """An undefined var() silently drops the declaration that uses it."""
    css = _without_css_comments(CSS)
    defined = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", css))
    used = set(re.findall(r"var\((--[A-Za-z0-9_-]+)", css))
    assert used - defined == set()


def test_every_element_id_used_by_the_script_exists():
    """$("x") throws at runtime only when that branch is reached."""
    referenced = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', JS))
    present = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', HTML))
    assert referenced - present == set()


def test_every_icon_reference_has_a_path():
    """A missing path renders an empty svg rather than raising."""
    known = set(_icon_paths())
    referenced = set(re.findall(r'data-icon="([A-Za-z]+)"', HTML))
    referenced |= set(re.findall(r'icon\("([A-Za-z]+)"\)', JS))
    assert referenced - known == set()


def test_icon_paths_are_parsed_not_silently_empty():
    """Guard the guard: the table must actually contain entries."""
    assert len(_icon_paths()) > 10


class _Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, tuple[int, int]]] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            self.open.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        if not self.open:
            self.problems.append(f"stray </{tag}>")
            return
        opened, position = self.open.pop()
        if opened != tag:
            self.problems.append(f"</{tag}> closes <{opened}> opened at {position}")


def test_html_tags_are_balanced():
    parser = _Balance()
    parser.feed(HTML)
    assert [tag for tag, _ in parser.open] == []
    assert parser.problems == []


def test_tabs_and_panels_are_wired_both_ways():
    """A panel with no tab (or the reverse) is unreachable and never noticed."""
    tabs = set(re.findall(r'data-tab="([A-Za-z0-9_-]+)"', HTML))
    panels = set(re.findall(r'data-panel="([A-Za-z0-9_-]+)"', HTML))
    assert tabs and tabs == panels


@pytest.mark.parametrize("tab", sorted(re.findall(r'data-tab="([A-Za-z0-9_-]+)"', HTML)))
def test_each_tab_controls_an_existing_panel(tab):
    markup = re.search(rf'<button[^>]*data-tab="{tab}"[^>]*>', HTML)
    assert markup, f"tab {tab} has no button"
    target = re.search(r'aria-controls="([A-Za-z0-9_-]+)"', markup.group(0))
    assert target, f"tab {tab} has no aria-controls"
    panel = re.search(rf'id="{target.group(1)}"[^>]*role="tabpanel"', HTML)
    assert panel, f"tab {tab} points at {target.group(1)}, which is not a tabpanel"


def test_tools_are_grouped_by_their_source_server(tmp_path):
    """The inspector groups tools by server, and strips that prefix from each row."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    (tmp_path / "app.js").write_text(JS, encoding="utf-8")
    script = tmp_path / "group_check.cjs"
    script.write_text(GROUP_CHECK_JS, encoding="utf-8")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, cwd=tmp_path, timeout=30
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
