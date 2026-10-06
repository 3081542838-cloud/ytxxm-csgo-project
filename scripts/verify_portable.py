"""Verify a full portable executable, its workers, and first-run resource setup."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import uuid

_spec = importlib.util.spec_from_file_location('portable_verification_build', Path(__file__).with_name('build.py'))
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--demo', type=Path, required=True)
    parser.add_argument('--prepare-resources', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'src'))
    from cs2pov.adapters.demo import fingerprint
    from cs2pov.adapters.video import read_lease
    from cs2pov.services.demo_worker import verify_cache
    from cs2pov.services.portable import portable_root
    folder = args.folder.absolute()
    executable = folder / '虾米pov.exe'
    assert portable_root(executable, True) == folder
    checksum = build.file_sha256(executable)
    directory = root / '.reports' / ('portable-verification-' + uuid.uuid4().hex)
    directory.mkdir()
    env = build.clean_build_environment()
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    def run(options, timeout):
        completed = subprocess.run([str(executable), *map(str, options)], cwd=directory,
            env=env, timeout=timeout, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        if completed.returncode:
            raise RuntimeError('便携进程失败：' + str(options))
    run(['--package-smoke', '--data-dir', directory / 'runtime'], 60)
    runtime = json.loads((directory / 'runtime/package-smoke-result.json').read_text())
    assert runtime['ok'] and runtime['frozen'] and runtime['parser_dataframe_bridge'] and runtime['sqlite']
    assert all(runtime['imports'].values())
    for asset in ('shrimp.ico', 'replay_telemetry.js', 'pov_visibility.js'):
        assert runtime['assets'][asset] == build.file_sha256(root / 'src/cs2pov/resources' / asset)
    resources = None
    if args.prepare_resources:
        first_result = folder / 'data' / ('resource-first-' + uuid.uuid4().hex + '.json')
        run(['--resource-worker', '--root', folder, '--result', first_result], 180)
        resources = json.loads(first_result.read_text(encoding='utf-8'))
        assert resources['ok']
        second_result = folder / 'data' / ('resource-offline-' + uuid.uuid4().hex + '.json')
        run(['--resource-worker', '--root', folder, '--result', second_result], 60)
        offline = json.loads(second_result.read_text(encoding='utf-8'))
        assert offline['ok'] and offline['installed'] == [] and offline['network_used'] is False
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
    assert build.file_sha256(executable) == checksum
    report = {'ok': True, 'executable': str(executable), 'sha256': checksum,
              'runtime': runtime, 'resources': resources, 'demo_unchanged': True,
              'map': analysis['map'], 'players': len(analysis['players']),
              'analysis_sha256': cached['analysis_sha256']}
    build._exclusive_json(directory / 'verification.json', report)
    print(json.dumps({'ok': True, 'report': str(directory / 'verification.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
