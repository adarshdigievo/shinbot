"""Build an immutable catalog from plugin manifests in a marketplace repository."""

import json
import sys
from pathlib import Path

from shinbot.plugins import validate_package

root = Path(sys.argv[1])
plugins = []
for file in sorted(root.glob("plugins/*/shinbot-plugin.json")):
    manifest = json.loads(file.read_text())
    package = {
        name: (file.parent / name).read_bytes() for name in ["shinbot-plugin.json", *manifest["files"]]
    }
    checked, digest = validate_package(package)
    plugins.append(
        {
            "id": checked.id,
            "version": checked.version,
            "name": checked.name,
            "description": checked.description,
            "path": str(file.parent.relative_to(root)),
            "sha256": digest,
        }
    )
(root / "shinbot-marketplace.json").write_text(
    json.dumps({"schema_version": 1, "name": root.name, "plugins": plugins}, indent=2) + "\n"
)
print(f"Catalog contains {len(plugins)} validated plugins.")
