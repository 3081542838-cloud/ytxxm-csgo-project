"""Inspect the pinned candidate files without executing or mounting them."""

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cs2pov.adapters.vpk import inspect_vpk

root = Path("resources/private/reference-f05c698c755dc7806855bb13a5c823dbf6a21de6")
manifest = json.loads((root / "source-manifest.json").read_text(encoding="utf-8-sig"))
reports = []
for record in manifest["Files"]:
    path = root / record["File"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["SHA256"]:
        raise RuntimeError(f"Source file changed: {path.name}")
    if path.suffix != ".vpk":
        continue
    archive = inspect_vpk(path)
    report = {"file": path.name, "sha256": archive.sha256, "version": archive.version,
              "deployment_approved": False, "entries": []}
    for entry in archive.entries:
        report["entries"].append({"path": entry.path, "bytes": len(entry.data),
                                  "sha256": hashlib.sha256(entry.data).hexdigest(), "crc32": entry.crc32})
        print(f"{path.name}: {entry.path} ({len(entry.data)} bytes)")
    reports.append(report)
target = Path("docs/validation/pov-source-audit.json")
target.write_text(json.dumps({"commit": manifest["Commit"], "archives": reports}, indent=2) + "\n", encoding="utf-8")
print("All source hashes and VPK entry CRCs verified. Deployment is still NOT approved.")
