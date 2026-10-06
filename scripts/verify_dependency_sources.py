"""Compare mirror download hashes against the official PyPI release records."""

import json
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import urlopen

report = json.loads(Path(".reports-install.json").read_text(encoding="utf-8"))
verified = []
for entry in report["install"]:
    metadata = entry["metadata"]
    name, version = metadata["name"], metadata["version"]
    archive = entry["download_info"]
    actual = archive["archive_info"]["hashes"]["sha256"]
    filename = unquote(Path(urlparse(archive["url"]).path).name)
    official_url = f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json"
    with urlopen(official_url, timeout=30) as response:
        release = json.load(response)
    matching = [item for item in release["urls"]
                if item["filename"] == filename and item["digests"]["sha256"] == actual]
    if not matching:
        raise RuntimeError(f"Official PyPI hash does not match: {name} {version} {filename}")
    verified.append({"name": name, "version": version, "filename": filename,
                     "sha256": actual, "official_url": official_url})
    print(f"Verified {name} {version}", flush=True)
target = Path("docs/licenses/source-checksums.json")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(verified, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"All {len(verified)} downloaded packages match official PyPI records.")
