"""Linux process-group leader that stays alive until every descendant exits.

The runtime records this leader's kernel identity. Becoming a subreaper keeps
orphan grandchildren attached to a verifiable owner after their parent exits.
This file is a fixed subprocess entrypoint and uses only the standard library.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys


def main() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot establish process ownership")
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)
    code = child.wait()
    while True:
        try:
            os.waitpid(-1, 0)
        except InterruptedError:
            continue
        except ChildProcessError:
            break
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
