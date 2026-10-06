"""Export installed dependency provenance; this is not legal approval."""

import importlib.metadata
import json
from pathlib import Path

entries = []
for distribution in importlib.metadata.distributions():
    metadata = distribution.metadata
    entries.append({
        "name": metadata["Name"],
        "version": distribution.version,
        "license": metadata.get("License"),
        "license_expression": metadata.get("License-Expression"),
        "license_classifiers": [item for item in metadata.get_all("Classifier", [])
                                if item.startswith("License ::")],
        "project_urls": metadata.get_all("Project-URL", []),
        "license_files": metadata.get_all("License-File", []),
    })
target = Path("docs/licenses/dependencies.json")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(sorted(entries, key=lambda entry: entry["name"].lower()),
                            indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Recorded {len(entries)} distributions in {target}")
