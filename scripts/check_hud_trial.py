"""Read-only game/Demo preflight; only output probe/report writes. No deployment."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cs2pov.services.hud_trial import check_trial

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--installation", type=Path, required=True)
parser.add_argument("--demo", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--cfg", type=Path, required=True)
parser.add_argument("--resource", type=Path, default=Path("resources/private/minimal-pov/pov.vpk"))
parser.add_argument("--report", type=Path, default=Path("local-validation/trial-preflight.json"))
args = parser.parse_args()
check = check_trial(args.installation, args.demo, args.output, args.resource, args.cfg)
report = asdict(check)
report.update(nvidia_output_location_verified=False, deployed=False, launched=False)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
