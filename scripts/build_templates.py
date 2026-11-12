"""Validate and hash a bot-template repository without executing its contents."""

import hashlib
import json
import sys
from pathlib import Path

from shinbot.bots import Template

root = Path(sys.argv[1])
items = []
for file in sorted(root.glob("templates/*/template.json")):
    data = file.read_bytes()
    template = Template.model_validate_json(data)
    items.append(
        {
            "id": template.id,
            "version": template.version,
            "path": str(file.relative_to(root)),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
(root / "shinbot-templates.json").write_text(
    json.dumps({"schema_version": 1, "templates": items}, indent=2) + "\n"
)
print(f"Validated {len(items)} bot templates.")
