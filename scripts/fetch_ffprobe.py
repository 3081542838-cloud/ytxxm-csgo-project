"""Fetch a pinned development ffprobe; never install globally or bundle it."""
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.7z'
SHA256 = '4705843ccaaf54257c16ad90f3e952ece33c17df964ecf7bfdbb0f49c7171077'
MAX_BYTES = 45_000_000


def main():
    directory = ROOT/'local-validation/tools/ffprobe-9.0.2'
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory/'ffmpeg-9.0.2-essentials_build.7z'
    if not archive.exists():
        temporary = archive.with_suffix('.partial')
        digest = hashlib.sha256(); count = 0
        try:
            with urllib.request.urlopen(URL, timeout=60) as response, temporary.open('xb') as target:
                while block := response.read(1_048_576):
                    count += len(block)
                    if count > MAX_BYTES:
                        raise ValueError('Development archive exceeds the fixed download limit')
                    digest.update(block); target.write(block)
            if digest.hexdigest() != SHA256:
                raise ValueError('Published archive checksum mismatch')
            temporary.replace(archive)
        except Exception:
            # Preserve partial/error evidence rather than silently redownload.
            raise
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError('Local archive checksum mismatch')
    listing = subprocess.run(['tar', '-tf', str(archive)], capture_output=True, text=True,
                             timeout=30, check=True).stdout.splitlines()
    prefix = 'ffmpeg-9.0.2-essentials_build/'
    names = [prefix+'bin/ffprobe.exe', prefix+'LICENSE', prefix+'README.txt']
    if not all(name in listing for name in names):
        raise ValueError('Pinned archive layout mismatch')
    for name in names:
        subprocess.run(['tar', '-xf', str(archive), '-C', str(directory), name],
                       timeout=30, check=True)
    executable = directory/prefix/'bin/ffprobe.exe'
    result = subprocess.run([str(executable), '-version'], capture_output=True,
                            text=True, timeout=10, check=True)
    if not result.stdout.startswith('ffprobe version 9.0.2-essentials_build-www.gyan.dev'):
        raise ValueError('Pinned ffprobe version mismatch')
    manifest = dict(schema=1, version='9.0.2', source=URL, archive_sha256=SHA256,
        bytes=archive.stat().st_size, executable=str(executable.relative_to(ROOT)),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        license='GPL-3.0-or-later', distribution='development-only-not-bundled',
        source_commit='946fcce07b', version_output=result.stdout)
    (directory/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key:manifest[key] for key in ('bytes','executable','executable_sha256','distribution')}))


if __name__ == '__main__':
    main()
