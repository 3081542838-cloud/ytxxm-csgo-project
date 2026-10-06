"""Exercise the delivered single file with isolated data and the original Demo."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

import build_onefile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'src'))
    from cs2pov.adapters.demo import fingerprint
    from cs2pov.adapters.video import read_lease
    from cs2pov.services.demo_worker import verify_cache
    executable = root / 'dist/虾米pov/虾米pov.exe'
    directory = root / '.reports' / ('onefile-' + uuid.uuid4().hex)
    directory.mkdir()
    checksum = build_onefile.base.file_sha256(executable)
    build_onefile.audit_archive(executable)
    env = build_onefile.base.clean_build_environment()
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    def run(options, timeout):
        result = subprocess.run([str(executable), *map(str, options)],
                                cwd=directory, env=env, timeout=timeout,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            raise RuntimeError('单文件运行检查失败：' + str(result.returncode))
    run(['--package-smoke', '--data-dir', directory / 'runtime'], 60)
    runtime = json.loads((directory / 'runtime/package-smoke-result.json').read_text())
    assert runtime['ok'] and runtime['frozen'] and runtime['sqlite'] and runtime['parser_dataframe_bridge']
    assert all(runtime['imports'].values())
    for asset in build_onefile.base.ASSETS:
        assert runtime['assets'][asset] == build_onefile.base.file_sha256(root / 'src/cs2pov/resources' / asset)
    with read_lease(args.demo):
        before = fingerprint(args.demo)
        run(['--demo-worker', '--demo', args.demo, '--cache', directory / 'cache',
             '--result', directory / 'demo.json'], 120)
        result = json.loads((directory / 'demo.json').read_text())
        assert result['fingerprint'] == before
        cached = json.loads(Path(result['cache']).read_text(encoding='utf-8'))
        assert cached['fingerprint'] == before
        analysis = verify_cache(cached)
        assert fingerprint(args.demo) == before
    assert build_onefile.base.file_sha256(executable) == checksum
    report = {'ok': True, 'executable': str(executable), 'sha256': checksum,
              'runtime': runtime, 'demo_unchanged': True, 'players': len(analysis['players']),
              'map': analysis['map'], 'analysis_sha256': cached['analysis_sha256']}
    build_onefile.base._exclusive_json(directory / 'verification.json', report)
    print(json.dumps({'ok': True, 'report': str(directory / 'verification.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
