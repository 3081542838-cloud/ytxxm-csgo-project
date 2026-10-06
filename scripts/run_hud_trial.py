"""Supervise one local -insecure Demo and restore after the owned game exits.

Run from an approved terminal tool, never through desktop UI. This script does
not send recording hotkeys. Closing the game triggers independently supervised
recovery even while the assistant is waiting for the user.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cs2pov.services.hud_trial import check_trial, prepare_trial, trial_transaction
from cs2pov.adapters.demo_path import playdemo_command
from cs2pov.storage.transaction import atomic_write

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--installation", type=Path, required=True)
parser.add_argument("--demo", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--cfg", type=Path, required=True)
parser.add_argument("--resource", type=Path, default=Path("resources/private/minimal-pov/pov.vpk"))
parser.add_argument("--session", type=Path, required=True)
parser.add_argument("--restore-only", action="store_true")
args = parser.parse_args()

if args.restore_only:
    from cs2pov.services.hud_trial import TrialCheck
    raw = json.loads((args.session / "trial-context.json").read_text(encoding="utf-8"))
    # Rebuild allowed targets from explicit installation/config inputs, not paths
    # from the journal. Context only supplies the previous CONFIG_NAMES subset.
    from cs2pov.services.hud_trial import CONFIG_NAMES
    cfg = args.cfg.absolute()
    configs = tuple(cfg / name for name in CONFIG_NAMES if str(cfg / name) in raw["protected_configs"])
    check = TrialCheck(args.installation.absolute(), raw["gameinfo_sha256"], args.demo,
                       args.output, 0, configs, raw.get("patch_version"))
    transaction = trial_transaction(check, args.session)
    transaction.load()
    results = transaction.restore()
    print(json.dumps(results, ensure_ascii=False), flush=True)
    sys.exit(0 if transaction.data["complete"] else 2)

check = check_trial(args.installation, args.demo, args.output, args.resource, args.cfg)
transaction = prepare_trial(check, args.session, args.resource)
context = asdict(check)
atomic_write(args.session / "trial-context.json", json.dumps(context, ensure_ascii=False, default=str).encode("utf-8"))
process = None
try:
    transaction.deploy()
    # This engine version lost Unicode characters in startup +playdemo. The
    # quoted forward-slash console command was verified in the actual replay.
    command = [str(check.installation / "game/bin/win64/cs2.exe"),
               "-insecure", "-novid", "-console"]
    atomic_write(args.session / "launch.json", json.dumps({"argv": command}, ensure_ascii=False).encode("utf-8"))
    process = subprocess.Popen(command, cwd=check.installation / "game/bin/win64")
    atomic_write(args.session / "process.json", json.dumps({"pid": process.pid, "argv": command}, ensure_ascii=False).encode("utf-8"))
    print(f"DEPLOYED; local -insecure CS2 PID={process.pid}; backups={args.session}", flush=True)
    print("No recording hotkeys sent. Exit this game normally to trigger restoration.", flush=True)
    print(f"Demo console command: {playdemo_command(check.demo)}", flush=True)
    process.wait()
    print(f"Owned CS2 exited with code {process.returncode}", flush=True)
finally:
    if process is None or process.poll() is not None:
        results = transaction.restore()
        atomic_write(args.session / "restore-result.json", json.dumps(results, ensure_ascii=False).encode("utf-8"))
        print(json.dumps(results, ensure_ascii=False), flush=True)
        if not transaction.data["complete"]:
            print("RESTORE BLOCKED: keep backups and inspect conflicts; do not start normal play.", flush=True)
            sys.exit(2)
        print("RESTORED: original bytes and file attributes verified.", flush=True)
    else:
        print("Supervisor interrupted while CS2 remains open; recovery journal retained.", flush=True)
