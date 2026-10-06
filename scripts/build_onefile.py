"""Build the shareable single executable without embedding local user data."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

_spec = importlib.util.spec_from_file_location('onefile_build_base', Path(__file__).with_name('build.py'))
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)


def command(root, candidate, *, python=None):
    args = base.build_command(root, candidate, python=python)
    args[args.index('--onedir')] = '--onefile'
    args[-1:-1] = ['--add-data', str(candidate / 'licenses') + os.pathsep + 'THIRD_PARTY']
    return args


def audit_archive(executable):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    names = [name.replace('\\', '/') for name in archive.toc]
    for name in names:
        path = Path(name)
        if (path.suffix.lower() in base._FORBIDDEN_SUFFIXES
                or any(part.lower() in base._FORBIDDEN_COMPONENTS for part in path.parts)
                or base._interposing_system_dll(path.name)):
            raise base.BuildError('单文件包包含不应分发的文件：' + name)
    for asset in base.ASSETS:
        if 'cs2pov/resources/' + asset not in names:
            raise base.BuildError('单文件包缺少资源：' + asset)
    if 'THIRD_PARTY/PYTHON-LICENSE.txt' not in names:
        raise base.BuildError('单文件包缺少许可证。')
    return names


def main():
    root = Path(__file__).resolve().parents[1]
    candidate = root / '.build' / ('singlefile-' + uuid.uuid4().hex)
    args = command(root, candidate)
    before = base.source_manifest(root)
    candidate.mkdir(parents=True)
    licenses = base.collect_licenses(candidate / 'licenses', base.read_lock(root / 'requirements.lock'))
    environment = base.clean_build_environment(python=args[0])
    with (candidate / 'build.log').open('xb') as log:
        result = subprocess.run(args, cwd=root, env=environment, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                timeout=1200, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise base.BuildError('构建失败，请查看：' + str(candidate / 'build.log'))
    if base.source_manifest(root) != before:
        raise base.BuildError('构建期间源码变化。')
    # The onefile equivalent of COLLECT is PKG; reuse the source auditor.
    audit_dir = candidate / 'native-audit'
    audit_dir.mkdir()
    shutil.copyfile(candidate / 'work' / base.APP_NAME / 'PKG-00.toc', audit_dir / 'COLLECT-00.toc')
    native = base.audit_native_sources(audit_dir, required=True)
    executable = candidate / 'dist' / (base.APP_NAME + '.exe')
    names = audit_archive(executable)
    checksum = base.file_sha256(executable)
    delivery = root / 'dist' / '虾米pov'
    delivery.mkdir(parents=True, exist_ok=True)
    target = delivery / '虾米pov.exe'
    if target.exists():
        raise base.BuildError('目标文件已存在，禁止覆盖：' + str(target))
    shutil.copyfile(executable, target)
    if base.file_sha256(target) != checksum:
        raise base.BuildError('复制后的程序校验失败。')
    base._exclusive_json(candidate / 'onefile-manifest.json', {
        'executable': str(target), 'sha256': checksum, 'bytes': target.stat().st_size,
        'command': args, 'source': before, 'native_sources': native,
        'embedded_files': names, 'licenses': licenses})
    print(json.dumps({'executable': str(target), 'candidate': str(candidate),
                      'bytes': target.stat().st_size, 'sha256': checksum}, ensure_ascii=False))


if __name__ == '__main__':
    main()
