"""Create an isolated internal onedir candidate; never alter prior builds/data."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid


class BuildError(ValueError):
    pass


APP_NAME = 'XiamiPOV'
ASSETS = ('shrimp.ico', 'replay_telemetry.js', 'pov_visibility.js')
_FORBIDDEN_SUFFIXES = frozenset({'.vpk', '.dem', '.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv'})
_FORBIDDEN_COMPONENTS = frozenset({'private', 'incoming', 'local-validation', 'sessions', 'videos'})
_SEARCH_ENVIRONMENT = frozenset({'PATH', 'PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH',
                               'QML_IMPORT_PATH', 'QML2_IMPORT_PATH'})


def clean_build_environment(*, environ=None, python=None, base_prefix=None):
    """Resolve build DLLs from this Python and Windows, never host tool PATHs."""
    inherited = dict(os.environ if environ is None else environ)
    child = {key: value for key, value in inherited.items()
             if key.upper() not in _SEARCH_ENVIRONMENT}
    windows = next((value for key, value in inherited.items() if key.upper() == 'SYSTEMROOT'), None)
    if windows is None:
        windows = next((value for key, value in inherited.items() if key.upper() == 'WINDIR'), None)
    if not windows:
        raise BuildError('构建环境缺少 Windows SystemRoot，不能确定系统 DLL 搜索目录。')
    windows = _plain(windows, directory=True)
    interpreter = Path(sys.executable if python is None else python).absolute()
    runtime = Path(sys.base_prefix if base_prefix is None else base_prefix).absolute()
    values = [interpreter.parent, runtime, runtime / 'DLLs', runtime / 'Scripts',
              windows / 'System32', windows]
    paths, seen = [], set()
    for value in values:
        if value.is_dir():
            value = _plain(value, directory=True)
            normalized = str(value).casefold()
            if normalized not in seen:
                paths.append(str(value))
                seen.add(normalized)
    child['PATH'] = os.pathsep.join(paths)
    return child


def _interposing_system_dll(name):
    name = Path(name).name.casefold()
    return (name == 'ucrtbase.dll'
            or name.startswith(('api-ms-win-', 'ext-ms-win-')) and name.endswith('.dll')
            or re.fullmatch(r'icu(?:uc|in|dt)\d*\.dll', name) is not None)


def audit_native_sources(work, *, required=False):
    """Read PyInstaller's literal COLLECT evidence without evaluating Python."""
    toc = Path(work) / 'COLLECT-00.toc'
    if not toc.exists():
        if required:
            raise BuildError('构建缺少 COLLECT 动态库来源审计清单。')
        return {'available': False, 'binary_count': 0, 'binaries': []}
    toc = _plain(toc)
    if toc.stat().st_size > 8_388_608:
        raise BuildError('构建动态库来源审计清单超过大小限额。')
    try:
        tree = ast.literal_eval(toc.read_text(encoding='utf-8'))
    except (SyntaxError, ValueError, TypeError, RecursionError) as error:
        raise BuildError('不能解析构建动态库来源审计清单。') from error
    stack, binaries, visited = [tree], [], 0
    windows = next((value for key, value in os.environ.items() if key.upper() == 'SYSTEMROOT'), None)
    windows = str(Path(windows).absolute()).casefold().rstrip('\\/') if windows else None
    while stack:
        value = stack.pop()
        visited += 1
        if visited > 200000:
            raise BuildError('构建动态库来源审计清单超过条目限额。')
        if isinstance(value, (tuple, list)):
            if (len(value) == 3 and all(isinstance(item, str) for item in value)
                    and value[2] in ('BINARY', 'EXTENSION')):
                name, source, kind = value
                normalized = source.replace('\\', '/').casefold()
                parts = normalized.split('/')
                foreign_native = ('native' in parts and
                                  ('poppler' in parts or 'libheif' in parts
                                   or 'codex-runtimes' in parts))
                from_windows = windows and (source.casefold().startswith(windows + '\\')
                                             or source.casefold().startswith(windows + '/'))
                if foreign_native or from_windows or _interposing_system_dll(name):
                    raise BuildError(f'生成目录动态库来源包含外来 native 或系统 DLL：{name} ← {source}')
                binaries.append({'path': name, 'source': source, 'kind': kind})
            else:
                stack.extend(value)
    if required and not binaries:
        raise BuildError('构建动态库来源审计清单没有可核验的二进制条目。')
    binaries.sort(key=lambda item: (item['path'].casefold(), item['source'].casefold()))
    return {'available': True, 'binary_count': len(binaries), 'binaries': binaries}


def _plain(path, *, directory=False, must_exist=True):
    path = Path(path).absolute()
    if '..' in path.parts or str(path).startswith(('\\\\', '//')):
        raise BuildError('只允许本机普通路径。')
    parents = tuple(reversed(path.parents))
    for value in (*parents, path):
        try:
            info = value.lstat()
        except FileNotFoundError:
            if value == path and not must_exist:
                continue
            # New candidate parents may not exist yet; their existing ancestors
            # still must be plain directories.
            if not must_exist:
                continue
            raise BuildError('构建所需文件或目录不存在。')
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BuildError('构建路径包含链接、junction 或 reparse point。')
        is_directory = value != path or directory
        if not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)):
            raise BuildError('构建路径不是普通文件或目录。')
    return path


def file_sha256(path):
    path = _plain(path)
    before = path.stat()
    if before.st_size > 536_870_912:
        raise BuildError('单个构建文件大小超过安全限额。')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        held = os.fstat(source.fileno())
        while chunk := source.read(1_048_576):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    named = path.stat()
    # On this Python 3.12 Windows runtime path.stat().ctime is creation time,
    # while os.fstat().ctime can report last-write time. Compare each API's
    # ctime before/after, and compare file identity/size/mtime across both APIs.
    signatures = [(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                  for value in (before, held, after, named)]
    if not all(signature == signatures[0] for signature in signatures[1:]):
        raise BuildError('构建文件在生成 hash 时发生变化。')
    if before.st_ctime_ns != named.st_ctime_ns or held.st_ctime_ns != after.st_ctime_ns:
        raise BuildError('构建文件在生成 hash 时修改时间发生变化。')
    return digest.hexdigest()


def _digest_rows(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def _entry(path, root):
    return {'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size,
            'sha256': file_sha256(path)}


def read_lock(path):
    content = _plain(path).read_text(encoding='utf-8')
    if len(content) > 262144:
        raise BuildError('依赖锁定文件超过大小限额。')
    result, names = [], set()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line in ('--index-url https://pypi.org/simple', '--only-binary=:all:'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.+_-]*) --hash=sha256:([0-9a-f]{64})', line)
        if not match:
            raise BuildError('所有构建依赖必须锁定确切版本和 SHA256。')
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in names:
            raise BuildError('依赖锁定文件包含重复包。')
        names.add(name)
        result.append({'name': name, 'version': match[2], 'wheel_sha256': match[3]})
    if not result:
        raise BuildError('依赖锁定文件为空。')
    return sorted(result, key=lambda item: item['name'])


def source_manifest(root):
    root = _plain(root, directory=True)
    paths = []
    source = root / 'src' / 'cs2pov'
    _plain(source, directory=True)
    for folder, directories, files in os.walk(source, followlinks=False):
        folder = _plain(folder, directory=True)
        for name in directories:
            _plain(folder / name, directory=True)
        directories[:] = [name for name in directories if name != '__pycache__']
        for name in files:
            path = folder / name
            if path.suffix.casefold() in ('.py', '.js', '.ico'):
                paths.append(path)
    for relative in ('pyproject.toml', 'requirements.lock', 'BUILD.md', 'scripts/build.py',
                     'scripts/package_smoke.py', 'scripts/portable_demo_smoke.py'):
        path = root / relative
        if path.exists():
            paths.append(path)
    if len(paths) > 4096:
        raise BuildError('源码文件数量超过限额。')
    rows = [_entry(path, root) for path in sorted(paths)]
    return {'files': rows, 'sha256': _digest_rows(rows)}


def build_command(root, candidate, *, python=None):
    root = _plain(root, directory=True)
    candidate = _plain(candidate, directory=True, must_exist=False)
    try:
        relative = candidate.relative_to(root / '.build')
    except ValueError as error:
        raise BuildError('候选目录必须在项目 .build 内。') from error
    if len(relative.parts) != 1:
        raise BuildError('候选构建目录必须是项目 .build 的直接子目录。')
    if candidate.exists():
        raise BuildError('构建目录已存在；不能覆盖已有候选或用户文件。')
    resource = root / 'src' / 'cs2pov' / 'resources'
    for name in ASSETS:
        _plain(resource / name)
    entrypoint = _plain(root / 'src' / 'cs2pov' / 'app.py')
    command = [str(python or sys.executable), '-m', 'PyInstaller', '--onedir', '--windowed',
               '--name', APP_NAME, '--distpath', str(candidate / 'dist'),
               '--workpath', str(candidate / 'work'), '--specpath', str(candidate / 'spec'),
               '--paths', str(root / 'src'), '--icon', str(resource / 'shrimp.ico'),
               '--collect-submodules', 'cs2pov', '--collect-all', 'demoparser2',
               '--exclude-module', 'pytest', '--exclude-module', 'pip']
    # demoparser2's Rust/PyO3 extension imports these at runtime. Static
    # bytecode analysis cannot see its Arrow -> Polars -> pandas conversion.
    for name in ('polars', '_polars_runtime_32', 'pyarrow', 'tqdm'):
        command.extend(['--collect-all', name])
    for name in ('PySide6-Essentials', 'shiboken6', 'demoparser2', 'pandas', 'numpy', 'comtypes',
                 'polars', 'polars-runtime-32', 'pyarrow', 'tqdm'):
        command.extend(['--copy-metadata', name])
    for name in ASSETS:
        command.extend(['--add-data', str(resource / name) + os.pathsep + 'cs2pov/resources'])
    command.append(str(entrypoint))
    return command


def _license_name(path):
    name = path.name.casefold()
    return (any(name.startswith(prefix) for prefix in ('license', 'licence', 'copying', 'notice', 'copyright'))
            or any(part.casefold() == 'licenses' for part in path.parts))


def collect_licenses(destination, locked, *, distribution_getter=importlib.metadata.distribution,
                     python_prefix=sys.base_prefix):
    destination = _plain(destination, directory=True, must_exist=False)
    if destination.exists():
        raise BuildError('许可证收集目录已存在，不能覆盖。')
    destination.mkdir(parents=True)
    rows, missing = [], []
    for item in locked:
        try:
            distribution = distribution_getter(item['name'])
        except importlib.metadata.PackageNotFoundError as error:
            raise BuildError('锁定的构建依赖未安装。') from error
        if distribution.version != item['version']:
            raise BuildError(f"依赖 {item['name']} 的已安装版本与锁定版本不一致。")
        files = distribution.files or []
        if len(files) > 200000:
            raise BuildError('单个依赖文件清单超过限额。')
        metadata_files = [file for file in files if Path(str(file)).name == 'METADATA'
                          and len(Path(str(file)).parts) == 2
                          and Path(str(file)).parent.name.endswith('.dist-info')]
        if len(metadata_files) != 1:
            raise BuildError('依赖没有唯一可核验的 METADATA 文件。')
        metadata_path = _plain(distribution.locate_file(metadata_files[0]))
        if metadata_path.stat().st_size > 4_194_304:
            raise BuildError('依赖 METADATA 超过大小限额。')
        package_folder = destination / (item['name'] + '-' + item['version'])
        package_folder.mkdir()
        metadata_copy = package_folder / 'METADATA.txt'
        metadata_copy.write_bytes(metadata_path.read_bytes())
        licenses = []
        source_licenses = [file for file in files if _license_name(Path(str(file)))]
        if len(source_licenses) > 1024:
            raise BuildError('单个依赖许可证数量超过限额。')
        for index, source in enumerate(source_licenses):
            original = _plain(distribution.locate_file(source))
            if original.stat().st_size > 4_194_304:
                raise BuildError('依赖许可证大小超过限额。')
            copied = package_folder / f'{index:04d}.txt'
            copied.write_bytes(original.read_bytes())
            licenses.append({'original_path': Path(str(source)).as_posix(),
                             'packaged_path': copied.relative_to(destination).as_posix(),
                             'sha256': file_sha256(copied)})
        metadata = distribution.metadata
        row = {**item, 'metadata_path': metadata_copy.relative_to(destination).as_posix(),
               'metadata_sha256': file_sha256(metadata_copy), 'license': metadata.get('License'),
               'license_expression': metadata.get('License-Expression'),
               'project_urls': metadata.get_all('Project-URL', []), 'license_files': licenses}
        rows.append(row)
        if not licenses:
            missing.append(item['name'] + '==' + item['version'])
    python_license = None
    if python_prefix is not None:
        original = _plain(Path(python_prefix) / 'LICENSE.txt')
        copied = destination / 'PYTHON-LICENSE.txt'
        copied.write_bytes(original.read_bytes())
        python_license = {'version': sys.version, 'packaged_path': copied.name,
                          'sha256': file_sha256(copied)}
    return {'distributions': rows, 'python': python_license,
            'missing_license_evidence': sorted(missing), 'legal_approval': False}


def artifact_inventory(package, *, max_files=20000, max_bytes=3_221_225_472):
    package = _plain(package, directory=True)
    if type(max_files) is not int or not 1 <= max_files <= 20000:
        raise BuildError('生成目录文件数量限额无效。')
    paths, total = [], 0
    for folder, directories, files in os.walk(package, followlinks=False):
        folder = _plain(folder, directory=True)
        for name in directories:
            _plain(folder / name, directory=True)
        for name in files:
            path = _plain(folder / name)
            relative = path.relative_to(package)
            if name == 'build-manifest.json' and len(relative.parts) == 1:
                continue  # Manifest stores this tree digest and is recorded externally.
            if (path.suffix.casefold() in _FORBIDDEN_SUFFIXES
                    or path.name.casefold() in ('ffprobe.exe', 'ffmpeg.exe')
                    or any(part.casefold() in _FORBIDDEN_COMPONENTS for part in relative.parts)):
                raise BuildError('生成目录包含禁止分发的 HUD、Demo、录制视频或本地资源。')
            if _interposing_system_dll(path.name):
                raise BuildError('生成目录包含会覆盖 Windows 系统依赖的 DLL。')
            total += path.stat().st_size
            paths.append(path)
            if len(paths) > max_files or total > max_bytes:
                raise BuildError('生成目录的文件数量或大小超过限额。')
    rows = [_entry(path, package) for path in sorted(paths)]
    return {'files': rows, 'bytes': total, 'sha256': _digest_rows(rows)}


def _exclusive_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as target:
        json.dump(value, target, indent=2, ensure_ascii=False, allow_nan=False)
        target.write('\n')


def run_build(root, *, runner=subprocess.run, license_collector=collect_licenses):
    root = _plain(root, directory=True)
    nonce = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
    candidate = root / '.build' / ('candidate-' + nonce)
    command = build_command(root, candidate)
    locked = read_lock(root / 'requirements.lock')
    before = source_manifest(root)
    environment = clean_build_environment(python=command[0])
    candidate.mkdir(parents=True)
    _exclusive_json(candidate / 'build-request.json', {'schema': 1, 'release': False,
                                                       'command': command, 'source': before,
                                                       'build_environment': {'path': environment['PATH'].split(os.pathsep)},
                                                       'locked_dependencies': locked})
    with (candidate / 'build.log').open('xb') as log:
        result = runner(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, timeout=1200, check=False,
                        env=environment,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode != 0:
        raise BuildError(f'PyInstaller 构建失败；日志和目录保留：{candidate}')
    if source_manifest(root) != before:
        raise BuildError('构建期间源码发生变化；本候选不能通过验收，已保留。')
    native_sources = audit_native_sources(candidate / 'work' / APP_NAME,
                                          required=runner is subprocess.run)
    package = candidate / 'dist' / APP_NAME
    _plain(package / (APP_NAME + '.exe'))
    licenses = license_collector(package / 'THIRD_PARTY', locked)
    marker = package / 'INTERNAL_CANDIDATE.txt'
    marker.write_text('INTERNAL CANDIDATE - NOT RELEASE\n'
                      '尚未完成 NVIDIA 实机闭环、内容验收、两机各十次验收及分发授权审查。\n'
                      '不包含私有 HUD、Demo、视频或 ffprobe；需用户本地配置。\n', encoding='utf-8')
    manifest = {'schema': 1, 'release': False, 'application': APP_NAME,
                'built_at': datetime.now(timezone.utc).isoformat(), 'command': command,
                'source': before, 'requirements_lock_sha256': file_sha256(root / 'requirements.lock'),
                'build_environment': {'path': environment['PATH'].split(os.pathsep)},
                'native_sources': native_sources,
                'third_party': licenses, 'artifact': artifact_inventory(package),
                'pending_gates': ['NVIDIA real recording', 'video content review',
                                  'two PCs ten runs each', 'HUD/resource distribution approval']}
    _exclusive_json(package / 'build-manifest.json', manifest)
    _exclusive_json(candidate / 'build-result.json', {'package': str(package),
                    'manifest_sha256': file_sha256(package / 'build-manifest.json'),
                    'artifact_sha256': manifest['artifact']['sha256'], 'release': False})
    return package


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--plan', action='store_true', help='Read-only build command; do not build')
    args = parser.parse_args(argv)
    try:
        if args.plan:
            candidate = args.root / '.build' / ('candidate-plan-' + uuid.uuid4().hex[:12])
            print(json.dumps({'release': False, 'command': build_command(args.root, candidate),
                              'source': source_manifest(args.root),
                              'locked_dependencies': read_lock(args.root / 'requirements.lock')},
                             ensure_ascii=False))
        else:
            # Contain this build process and only its PyInstaller helper children.
            # A timeout/exit cannot leave analysis subprocesses behind.
            sys.path.insert(0, str(_plain(args.root, directory=True) / 'src'))
            from cs2pov.adapters.worker_job import contain_current_worker
            contain_current_worker()
            print(run_build(args.root))
        return 0
    except (BuildError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
