"""Tool Foundry: turn a repeatedly used script into an installed capability.

The pipeline has three separate steps on purpose, because collapsing them would
produce a machine that writes arbitrary code and then authorises itself to run
it:

1. **Author.** The agent writes a CLI plus tests into the conversation workspace.
   This happens only when the operator asks for it.
2. **Validate.** Tests run and the package is checked. Nothing is installed.
3. **Install.** The package is copied outside the workspace, hashed, and
   registered as a disabled, unapproved tool. Enabling it is the operator's job.

`permissions` in a manifest is a *proposal shown for review*. It is not
enforcement: an installed tool runs as the operator with no OS isolation, so a
script that ignores ``network: false`` will still reach the network.
"""

from backend.services.tool_foundry.manifest import (
    MANIFEST_FILENAME,
    FoundryError,
    FoundryManifest,
    load_manifest,
    save_manifest,
)
from backend.services.tool_foundry.store import (
    InstalledPackage,
    install_package,
    installed_root,
    list_installed,
    remove_installed,
    verify_integrity,
)

__all__ = [
    "MANIFEST_FILENAME",
    "FoundryError",
    "FoundryManifest",
    "InstalledPackage",
    "install_package",
    "installed_root",
    "list_installed",
    "load_manifest",
    "remove_installed",
    "save_manifest",
    "verify_integrity",
]
