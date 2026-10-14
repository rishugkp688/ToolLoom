"""Fixed filesystem-search worker. Never evaluates caller-supplied Python code."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main():
    # -I excludes the conversation cwd and PYTHONPATH from module lookup.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from backend.services.sandbox.fs_tool import _grep
    from backend.services.sandbox.workspace import Workspace, WorkspaceError

    request = json.load(sys.stdin)
    try:
        response = {"result": _grep(Workspace(Path(request["root"])), request["arguments"])}
    except (WorkspaceError, OSError) as exc:
        response = {"error": str(exc)}
    print(json.dumps(response))


if __name__ == "__main__":
    main()
