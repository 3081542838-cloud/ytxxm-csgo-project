"""Prepare a HUD-only experimental candidate, never mount it automatically."""

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cs2pov.adapters.vpk import build_vpk, validate_resource

reference = Path("resources/private/reference-f05c698c755dc7806855bb13a5c823dbf6a21de6")
manifest = json.loads((reference / "source-manifest.json").read_text(encoding="utf-8-sig"))
record = next(record for record in manifest["Files"] if record["Path"] == "pov/pov_voice_template.vpk")
audit = json.loads(Path("docs/validation/pov-source-audit.json").read_text(encoding="utf-8"))
review = next(item for item in audit["archives"] if item["file"] == record["File"])
archive = validate_resource(reference / record["File"], record["SHA256"], {item["path"] for item in review["entries"]})
files = {entry.path: entry.data for entry in archive.entries if entry.path in {
    "panorama/styles/hud/hudteamcounter.vcss_c",
    "panorama/styles/hud/hudteamcounter-equipmentinfo.vcss_c",
    "panorama/scripts/hud/huddemocontroller.vts_c",
}}
source = (reference / "pov--voice_hud_injection.js").read_bytes()
source_record = next(record for record in manifest["Files"] if record["Path"] == "pov/voice_hud_injection.js")
if hashlib.sha256(source).hexdigest() != source_record["SHA256"]:
    raise RuntimeError("Reference script changed")
replacement = Path("src/cs2pov/resources/pov_visibility.js").read_bytes()
script_path = "panorama/scripts/hud/huddemocontroller.vts_c"
compiled = files[script_path]
if compiled.count(source) != 1 or len(replacement) > len(source):
    raise RuntimeError("Compiled script cannot be safely replaced")
# Equal-length replacement preserves every resource block size/offset. The VPK
# CRC is regenerated. Actual engine compatibility still requires stage 3.
files[script_path] = compiled.replace(source, replacement.ljust(len(source), b" "), 1)
output = Path("resources/private/minimal-pov")
output.mkdir(parents=True, exist_ok=True)
raw = build_vpk(files)
target = output / "pov.vpk"
target.write_bytes(raw)
digest = hashlib.sha256(raw).hexdigest()
validate_resource(target, digest, set(files))
(output / "REFERENCE_LICENSE.txt").write_bytes((reference / "LICENSE").read_bytes())
(output / "manifest.json").write_text(json.dumps({
    "sha256": digest, "bytes": len(raw), "paths": sorted(files),
    "reference_commit": manifest["Commit"], "experimental_only": True,
    "engine_compatibility_verified": False, "distribution_review_complete": False,
    "required_notice": "Copyright (c) 2026 DrEAmSs59",
    "changes": "Removed input audio, alerts, voice/input/radar tracks and extra playback controls; HUD visibility only.",
}, indent=2) + "\n", encoding="utf-8")
print(f"Prepared {len(files)}-entry HUD-only candidate: {len(raw)} bytes, SHA256 {digest}")
print("Not installed; actual HUD and engine compatibility remain unverified.")
