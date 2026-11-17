"""Stop an image build unless the Camoufox browser is installed.

`camoufox fetch` exits 0 even when it could not read its release list and installed nothing.
"""

import os
import sys

try:
    from camoufox.pkgman import camoufox_path, launch_path

    path = launch_path(camoufox_path(download_if_missing=False))
except Exception as error:
    path = None
    print(error)
sys.exit(
    0
    if path and os.path.exists(path)
    else "Camoufox was not installed: its release list could not be read. Retry the build, "
    "or pass a GitHub token as the github_token build secret."
)
