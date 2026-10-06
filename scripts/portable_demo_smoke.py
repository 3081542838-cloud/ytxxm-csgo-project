"""Explicit frozen parser trial against an original, read-only local Demo."""
import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
_spec = importlib.util.spec_from_file_location('portable_package_smoke', ROOT / 'scripts/package_smoke.py')
_package = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_package)
from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.video import read_lease, read_data_lease
from cs2pov.services.demo_worker import CACHE_VERSION, verify_cache


def _cache_json(path):
    with read_data_lease(path) as signature:
        if signature.bytes > 32 * 1024 * 1024:
            raise _package.SmokeError('解析缓存超过产品 32MB 限额。')
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result: raise _package.SmokeError('解析缓存包含重复字段。')
                result[key] = value
            return result
        try:
            result = json.loads(Path(path).read_bytes(), object_pairs_hook=pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, RecursionError, UnicodeError) as error:
            raise _package.SmokeError('解析缓存不是有效 JSON。') from error
        if type(result) is not dict:
            raise _package.SmokeError('解析缓存不是对象。')
        return result


def parse_smoke(package, demo, directory, *, runner=subprocess.run, timeout=120,
                expected_sha256=None):
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise _package.SmokeError('原 Demo 检查需要至多 120 秒的有限期限。')
    if expected_sha256 is not None and (type(expected_sha256) is not str
            or re.fullmatch('[0-9a-f]{64}', expected_sha256) is None):
        raise _package.SmokeError('原 Demo 的预期 SHA256 无效。')
    static = _package.validate_package(package)
    package, demo = Path(package).absolute(), Path(demo).absolute()
    if demo.suffix.casefold() != '.dem':
        raise _package.SmokeError('只接受显式提供的原 .dem 文件。')
    directory = _package._build._plain(directory, directory=True, must_exist=False)
    if directory.exists():
        raise _package.SmokeError('原 Demo 检查需要新的隔离目录，不能复用旧缓存。')
    directory.mkdir(parents=True)
    cache, result_path = directory / 'cache', directory / 'demo-result.json'
    with read_lease(demo) as signature:
        before = fingerprint(demo)
        if expected_sha256 is not None and before['sha256'] != expected_sha256:
            raise _package.SmokeError('原 Demo 的当前 SHA256 与预期不一致，未解析。')
        command = [str(package / 'XiamiPOV.exe'), '--demo-worker', '--demo', str(demo),
                   '--cache', str(cache), '--result', str(result_path)]
        try:
            result = runner(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise _package.SmokeError('原 Demo 便携包解析未完成：' + str(error)) from error
        raw = _package._json(result_path)
        if result.returncode != 0 or 'error' in raw:
            raise _package.SmokeError('原 Demo 便携包解析失败：' + str(raw.get('error', result.returncode)))
        expected_cache = cache / (CACHE_VERSION + before['sha256'] + '.json')
        if set(raw) != {'cache', 'fingerprint'} or raw['fingerprint'] != before or Path(raw['cache']) != expected_cache:
            raise _package.SmokeError('解析结果没有绑定本次原 Demo 和新缓存。')
        cached = _cache_json(expected_cache)
        if cached.get('fingerprint') != before:
            raise _package.SmokeError('解析缓存的原 Demo 身份不一致。')
        try:
            analysis = verify_cache(cached)
        except (KeyError, ValueError, TypeError) as error:
            raise _package.SmokeError('解析缓存未通过完整性检查：' + str(error)) from error
        if fingerprint(demo) != before:
            raise _package.SmokeError('原 Demo 在便携包解析中变化，不能通过。')
    if _package.validate_package(package) != static:
        raise _package.SmokeError('原 Demo 检查改变了候选包。')
    return {**static, 'parser_passed': True, 'original_demo': str(demo),
            'original_signature': {**asdict(signature), 'path': str(signature.path)},
            'fingerprint_before': before, 'fingerprint_after': before,
            'cache': str(expected_cache), 'analysis_sha256': cached['analysis_sha256'],
            'map': analysis['map'], 'players': len(analysis['players']),
            'timeline_pairs': len(analysis['timeline']), 'game_started': False,
            'gui_started': False, 'recording_triggered': False, 'network_used': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--demo', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        report = parse_smoke(args.package, args.demo, args.data_dir, expected_sha256=args.expected_sha256)
        code = 0
    except Exception as error:
        report, code = {'schema': 1, 'parser_passed': False, 'error': str(error),
                       'gui_started': False, 'game_started': False, 'recording_triggered': False}, 1
    if args.report:
        _package._build._exclusive_json(_package._build._plain(args.report, must_exist=False), report)
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
