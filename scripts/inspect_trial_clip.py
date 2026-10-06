"""Read the provided Demo in place to choose a bounded HUD test sample."""
import argparse
import json
from pathlib import Path
from demoparser2 import DemoParser

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--demo", type=Path, required=True)
args = parser.parse_args()
demo = DemoParser(str(args.demo))
players = demo.parse_player_info()
events = demo.parse_events(["player_death", "player_hurt"])
result = {"players": json.loads(players.to_json(orient="records")), "events": {}}
for name, table in events:
    for col in table.columns:
        if "steamid" in col:
            table[col] = table[col].astype(str)
    target_rows = table[[str(76561199198478034) in str(row) for row in table.to_dict(orient="records")]]
    result["events"][name] = json.loads(target_rows.head(20).to_json(orient="records", force_ascii=False))
out = Path("local-validation/trial-demo-sample.json")
out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
