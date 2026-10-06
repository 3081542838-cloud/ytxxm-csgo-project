"""Builds must not resolve Windows/Qt dependencies from unrelated tool runtimes."""
import importlib.util
import json
import os
from pathlib import Path

import pytest


PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture
def build():
    spec = importlib.util.spec_from_file_location(
        'cs2pov_native_isolation_build', PROJECT / 'scripts' / 'build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project(tmp_path):
    root = tmp_path / 'project'
    resources = root / 'src' / 'cs2pov' / 'resources'
    resources.mkdir(parents=True)
    (resources.parent / 'app.py').write_text('raise SystemExit(0)')
    for name in ('shrimp.ico', 'replay_telemetry.js', 'pov_visibility.js'):
        (resources / name).write_bytes(b'fixture')
    (root / 'requirements.lock').write_text(
        'fixture==1 --hash=sha256:' + 'a' * 64 + '\n')
    return root


def _builder(command, **kwargs):
    package = Path(command[command.index('--distpath') + 1]) / 'XiamiPOV'
    package.mkdir(parents=True)
    (package / 'XiamiPOV.exe').write_bytes(b'fixture-executable')
    return type('Result', (), {'returncode': 0})()


def _licenses(destination, locked):
    return {'distributions': [], 'missing_license_evidence': [], 'legal_approval': False}


def test_run_build_supplies_isolated_child_environment_not_parent_search_path(tmp_path, build, monkeypatch):
    root = _project(tmp_path)
    foreign = tmp_path / 'dependencies' / 'native' / 'poppler' / 'Library' / 'bin'
    monkeypatch.setenv('PATH', str(foreign))
    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'foreign-python'))
    monkeypatch.setenv('QT_PLUGIN_PATH', str(tmp_path / 'foreign-qt'))
    before = dict(os.environ)
    seen = {}

    def runner(command, **kwargs):
        seen.update(kwargs)
        return _builder(command, **kwargs)

    package = build.run_build(root, runner=runner, license_collector=_licenses)
    child = seen['env']
    assert child is not os.environ and str(foreign) not in child['PATH']
    assert not any(key.upper() in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH') for key in child)
    assert str(Path(build.sys.executable).parent) in child['PATH'].split(os.pathsep)
    assert str(Path(os.environ['SystemRoot']) / 'System32') in child['PATH'].split(os.pathsep)
    assert dict(os.environ) == before
    request = json.loads((package.parents[1] / 'build-request.json').read_text(encoding='utf-8'))
    assert request['build_environment']['path'] == child['PATH'].split(os.pathsep)


def test_clean_environment_removes_case_aliases_and_keeps_only_required_existing_paths(tmp_path, build):
    python = tmp_path / 'venv' / 'Scripts' / 'python.exe'
    python.parent.mkdir(parents=True)
    base = tmp_path / 'runtime-python'
    (base / 'DLLs').mkdir(parents=True)
    windows = tmp_path / 'Windows'
    (windows / 'System32').mkdir(parents=True)
    inherited = {'Path': 'foreign', 'PATH': 'other', 'PythonPath': 'foreign',
                 'PYTHONHOME': 'foreign', 'Qt_Plugin_Path': 'foreign',
                 'QML_IMPORT_PATH': 'foreign', 'QML2_IMPORT_PATH': 'foreign',
                 'SystemRoot': str(windows), 'TEMP': 'keep-temporary-directory'}
    child = build.clean_build_environment(environ=inherited, python=python, base_prefix=base)
    assert child['PATH'].split(os.pathsep) == [str(python.parent), str(base), str(base / 'DLLs'),
                                               str(windows / 'System32'), str(windows)]
    assert child['TEMP'] == inherited['TEMP']
    assert inherited['Path'] == 'foreign'
    assert all(key not in child for key in ('Path', 'PythonPath', 'PYTHONHOME', 'Qt_Plugin_Path',
                                          'QML_IMPORT_PATH', 'QML2_IMPORT_PATH'))


@pytest.mark.parametrize('name', ['icuuc.dll', 'icuin.dll', 'icudt78.dll', 'ucrtbase.dll',
                                 'api-ms-win-crt-runtime-l1-1-0.dll', 'ext-ms-win-test.dll'])
def test_final_inventory_rejects_interposing_system_dll_and_preserves_evidence(tmp_path, build, name):
    package = tmp_path / 'package'
    path = package / '_internal' / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b'foreign-system-library')
    with pytest.raises(build.BuildError, match='DLL|动态库|系统'):
        build.artifact_inventory(package)
    assert path.read_bytes() == b'foreign-system-library'


@pytest.mark.parametrize('folder', ['poppler/Library/bin', 'libheif/libheif/bin'])
def test_run_build_rejects_foreign_native_source_even_if_library_renamed(tmp_path, build, folder):
    root = _project(tmp_path)

    def runner(command, **kwargs):
        result = _builder(command, **kwargs)
        work = Path(command[command.index('--workpath') + 1]) / 'XiamiPOV'
        work.mkdir(parents=True)
        foreign = tmp_path / 'codex-runtimes' / 'dependencies' / 'native' / folder / 'renamed.dll'
        (work / 'COLLECT-00.toc').write_text(repr((False, [('renamed.dll', str(foreign), 'BINARY')])))
        return result

    with pytest.raises(build.BuildError, match='来源|native|外来'):
        build.run_build(root, runner=runner, license_collector=_licenses)
    candidates = list((root / '.build').iterdir())
    assert (candidates[0] / 'dist' / 'XiamiPOV' / 'XiamiPOV.exe').is_file()
    assert not (candidates[0] / 'build-result.json').exists()


def test_collect_audit_accepts_venv_qt_and_python_runtime_binaries_and_records_sources(tmp_path, build):
    work = tmp_path / 'work'
    work.mkdir()
    paths = [tmp_path / '.venv' / 'Lib' / 'site-packages' / 'PySide6' / 'Qt6Core.dll',
             tmp_path / 'runtime-python' / 'python312.dll']
    rows = [(path.name, str(path), 'BINARY') for path in paths]
    (work / 'COLLECT-00.toc').write_text(repr((False, rows)))
    result = build.audit_native_sources(work)
    assert result['binary_count'] == 2
    assert {row['source'] for row in result['binaries']} == {str(path) for path in paths}


def test_collect_audit_refuses_malformed_or_executable_python_without_evaluation(tmp_path, build):
    work = tmp_path / 'work'
    work.mkdir()
    target = tmp_path / 'must-not-be-written'
    (work / 'COLLECT-00.toc').write_text(f"__import__('pathlib').Path({str(target)!r}).write_text('bad')")
    with pytest.raises(build.BuildError, match='审计|清单|解析'):
        build.audit_native_sources(work)
    assert not target.exists()
