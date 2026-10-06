"""Read-only portable-package checks; optional pure headless runtime probe."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys


_spec = importlib.util.spec_from_file_location('cs2pov_build_support', Path(__file__).with_name('build.py'))
_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_build)


class SmokeError(ValueError):
    pass


def _json(path):
    path = _build._plain(path)
    if path.stat().st_size > 16_777_216:
        raise SmokeError('打包报告超过大小限额。')
    try:
        def unique(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError('duplicate JSON key')
                result[name] = value
            return result
        result = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError) as error:
        raise SmokeError('打包报告不是有效 JSON。') from error
    if not isinstance(result, dict):
        raise SmokeError('打包报告结构无效。')
    return result


def validate_package(package):
    try:
        package = _build._plain(package, directory=True)
        manifest = _json(package / 'build-manifest.json')
        if type(manifest.get('schema')) is not int or manifest['schema'] != 1 or manifest.get('release') is not False:
            raise SmokeError('内部包标志或报告版本无效。')
        marker = package / 'INTERNAL_CANDIDATE.txt'
        if 'INTERNAL CANDIDATE - NOT RELEASE' not in _build._plain(marker).read_text(encoding='utf-8'):
            raise SmokeError('内部候选说明缺失。')
        _build._plain(package / (_build.APP_NAME + '.exe'))
        for name in _build.ASSETS:
            _build._plain(package / '_internal' / 'cs2pov' / 'resources' / name)
        inventory = _build.artifact_inventory(package)
        if inventory != manifest.get('artifact'):
            raise SmokeError('生成目录 hash 或文件签名发生变化。')
        licenses = manifest.get('third_party')
        if not isinstance(licenses, dict) or licenses.get('legal_approval') is not False:
            raise SmokeError('许可证证据报告缺失或错误宣称分发批准。')
        distributions = licenses.get('distributions')
        if not isinstance(distributions, list) or len(distributions) > 512:
            raise SmokeError('依赖许可证清单无效。')
        for distribution in distributions:
            if not isinstance(distribution, dict):
                raise SmokeError('依赖许可证证据条目不是对象。')
            license_files = distribution.get('license_files')
            if (not isinstance(license_files, list) or len(license_files) > 1024
                    or any(not isinstance(row, dict) for row in license_files)):
                raise SmokeError('依赖许可证文件证据清单无效。')
            paths = [(distribution.get('metadata_path'), distribution.get('metadata_sha256'))]
            paths.extend((row.get('packaged_path'), row.get('sha256'))
                         for row in license_files)
            for relative, sha256 in paths:
                if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
                    raise SmokeError('许可证证据路径越界。')
                if _build.file_sha256(package / 'THIRD_PARTY' / relative) != sha256:
                    raise SmokeError('许可证证据 hash 不匹配。')
        python = licenses.get('python')
        if python is not None:
            if (not isinstance(python, dict) or python.get('packaged_path') != 'PYTHON-LICENSE.txt'
                    or _build.file_sha256(package / 'THIRD_PARTY' / 'PYTHON-LICENSE.txt') != python.get('sha256')):
                raise SmokeError('Python 许可证证据 hash 不匹配。')
        return {'schema': 1, 'static_passed': True, 'release': False,
                'artifact_sha256': inventory['sha256'],
                'missing_license_evidence': licenses.get('missing_license_evidence', [])}
    except _build.BuildError as error:
        raise SmokeError(str(error)) from error


def runtime_smoke(package, data_directory, *, runner=subprocess.run, timeout=60):
    static = validate_package(package)
    package = Path(package).absolute()
    data_directory = _build._plain(data_directory, directory=True, must_exist=False)
    if data_directory.exists():
        raise SmokeError('无界面检查的数据目录必须是新的隔离目录。')
    command = [str(package / (_build.APP_NAME + '.exe')), '--package-smoke',
               '--data-dir', str(data_directory)]
    try:
        result = runner(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise SmokeError('便携包无界面运行检查失败；没有启动游戏。') from error
    try:
        if result.returncode != 0:
            raise ValueError('invalid result')
        # --windowed applications have sys.stdout=None. The headless branch
        # writes one bounded JSON result in this new isolated data directory.
        report_file = _build._plain(data_directory / 'package-smoke-result.json')
        if report_file.stat().st_size > 262144:
            raise ValueError('oversized result')
        report = json.loads(report_file.read_text(encoding='utf-8'))
        if (not isinstance(report, dict) or report.get('ok') is not True
                or type(report.get('schema')) is not int or report['schema'] != 1 or report.get('gui_started') is not False
                or report.get('game_started') is not False or report.get('network_used') is not False
                or report.get('frozen') is not True or report.get('sqlite') is not True
                or report.get('recording_triggered') is not False
                or report.get('worker_dispatch') != ['output', 'demo']
                or report.get('stage') != 'internal-only'
                or not isinstance(report.get('imports'), dict) or not report['imports']
                or not {'PySide6', 'demoparser2', 'pandas', 'numpy', 'comtypes',
                        'polars', '_polars_runtime_32', 'pyarrow', 'tqdm'} <= set(report['imports'])
                or report.get('parser_dataframe_bridge') is not True
                or not all(value is True for value in report['imports'].values())
                or not isinstance(report.get('assets'), dict)
                or set(report['assets']) != set(_build.ASSETS)
                or not all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
                           for value in report['assets'].values())):
            raise ValueError('missing runtime evidence')
        expected_assets = {name: _build.file_sha256(package / '_internal' / 'cs2pov' / 'resources' / name)
                           for name in _build.ASSETS}
        if report['assets'] != expected_assets:
            raise ValueError('runtime resource hashes differ')
    except (AttributeError, TypeError, ValueError, OSError) as error:
        raise SmokeError('便携包运行报告缺少必须的无界面检查证据。') from error
    # Runtime probe cannot change the package; all its writes use isolated data.
    if validate_package(package) != static:
        raise SmokeError('无界面检查后生成目录发生变化。')
    return {**static, 'runtime_passed': True, 'runtime': report}


def worker_dispatch_smoke(package, directory, *, runner=subprocess.run, timeout=30):
    """Malformed requests prove frozen dispatch fails closed without game input."""
    static = validate_package(package)
    package = Path(package).absolute()
    directory = _build._plain(directory, directory=True, must_exist=False)
    if directory.exists():
        raise SmokeError('工作进程检查需要新的隔离目录。')
    directory.mkdir(parents=True)
    request = directory / 'invalid-output-request.json'
    request.write_text('{}', encoding='utf-8')
    executable = package / (_build.APP_NAME + '.exe')
    commands = [
        ('output', [str(executable), '--output-worker', '--phase', 'discover',
                    '--request', str(request), '--result', str(directory / 'output-result.json')]),
        ('demo', [str(executable), '--demo-worker', '--demo', str(directory / 'does-not-exist.dem'),
                  '--cache', str(directory / 'unused-cache'), '--result', str(directory / 'demo-result.json')]),
    ]
    results = {}
    for name, command in commands:
        try:
            result = runner(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode != 1:
                raise SmokeError('无效工作进程任务没有明确拒绝。')
            report = _json(directory / (name + '-result.json'))
            if set(report) != {'error'} or not isinstance(report['error'], str) or not 0 < len(report['error']) <= 4096:
                raise SmokeError('工作进程失败报告缺少明确错误。')
            results[name] = {'invalid_request_rejected': True, 'error': report['error']}
        except (_build.BuildError, OSError, subprocess.TimeoutExpired) as error:
            raise SmokeError('工作进程无界面分派检查未完成。') from error
    if (directory / 'unused-cache').exists() or (directory / 'does-not-exist.dem').exists():
        raise SmokeError('无效任务创建了不应存在的 Demo 或缓存。')
    if validate_package(package) != static:
        raise SmokeError('工作进程检查改变了生成目录。')
    return {**static, 'worker_dispatch_passed': True, 'workers': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--runtime', action='store_true')
    parser.add_argument('--workers', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        if (args.runtime or args.workers) and args.data_dir is None:
            raise SmokeError('运行检查需要新的 --data-dir 隔离目录。')
        if args.runtime and args.workers:
            raise SmokeError('运行检查和工作进程检查请分别使用新隔离目录。')
        result = runtime_smoke(args.package, args.data_dir) if args.runtime else validate_package(args.package)
        if args.workers:
            result = worker_dispatch_smoke(args.package, args.data_dir)
        if args.report:
            _build._exclusive_json(_build._plain(args.report, must_exist=False), result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (SmokeError, _build.BuildError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
