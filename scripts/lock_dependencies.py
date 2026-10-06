"""Lock the exact downloaded Windows wheels, including the verified Qt wheel."""

import json
from pathlib import Path
from urllib.parse import urlparse

report = json.loads(Path(".reports-install.json").read_text(encoding="utf-8"))
records = []
for item in report["install"]:
    download = item["download_info"]
    if urlparse(download["url"]).hostname != "files.pythonhosted.org":
        raise RuntimeError("Unexpected package source; verify it against official PyPI first.")
    records.append({"name": item["metadata"]["name"], "version": item["metadata"]["version"],
                    "sha256": download["archive_info"]["hashes"]["sha256"],
                    "source": download["url"]})
qt = json.loads(Path(".wheels/qt-official-metadata.json").read_text(encoding="utf-8-sig"))
records.append({"name": "PySide6-Essentials", "version": "6.11.2", "sha256": qt["digests"]["sha256"],
                "source": qt["url"]})
records.sort(key=lambda item: item["name"].lower())
Path("requirements.lock").write_text(
    "# Python 3.12 / Windows x64; use pip --require-hashes -r requirements.lock\n"
    "--index-url https://pypi.org/simple\n--only-binary=:all:\n" +
    "".join(f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}\n" for row in records),
    encoding="utf-8",
)
target = Path("docs/licenses/source-checksums.json")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
print(f"Locked {len(records)} exact package versions and official download hashes.")
