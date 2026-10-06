"""Portable-candidate construction never packages recordings or unlicensed HUD."""
import importlib.util
import json
from pathlib import Path

import pytest


PROJECT = Path(__file__).resolve().parents[2]


def load_script(name):
    location = PROJECT / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location('cs2pov_test_' + name, location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def build():
    return load_script('build')


@pytest.fixture
def smoke():
    return load_script('package_smoke')


def source_fixture(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    resources = root / 'src' / 'cs2pov' / 'resources'; resources.mkdir(parents=True)
    (root / 'src' / 'cs2pov' / 'app.py').write_text('raise SystemExit(0)')
    for name in ('shrimp.ico', 'replay_telemetry.js', 'pov_visibility.js'):
        (resources / name).write_bytes(b'asset-' + name.encode())
    (root / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    (root / 'requirements.lock').write_text('fixture==1.0 --hash=sha256:' + 'a'*64 + '\n')
    return root


def test_build_command_is_onedir_explicit_resources_and_never_private_or_recordings(tmp_path, build):
    root = source_fixture(tmp_path)
    candidate = root / '.build' / 'candidate-fixture'
    command = build.build_command(root, candidate, python=Path('C:/python/python.exe'))
    assert '--onedir' in command and '--windowed' in command
    assert '--onefile' not in command and '--noconfirm' not in command and '--clean' not in command
    assert command[:3] == ['C:\\python\\python.exe', '-m', 'PyInstaller']
    assert '--add-data' in command
    for name in ('PySide6-Essentials', 'demoparser2', 'pandas', 'numpy', 'comtypes'):
        assert name in command
    for name in ('shrimp.ico', 'replay_telemetry.js', 'pov_visibility.js'):
        assert any(name in item for item in command)
    assert not any('private' in item or 'local-validation' in item or 'ffprobe' in item for item in command)
    assert command[-1] == str(root / 'src' / 'cs2pov' / 'app.py')


def test_build_command_cannot_choose_external_or_existing_candidate(tmp_path, build):
    root = source_fixture(tmp_path)
    with pytest.raises(build.BuildError, match='项目|目录'):
        build.build_command(root, tmp_path / 'external')
    existing = root / '.build' / 'old'; existing.mkdir(parents=True)
    (existing / 'user-video.mp4').write_bytes(b'keep')
    with pytest.raises(build.BuildError, match='已存在|覆盖'):
        build.build_command(root, existing)
    assert (existing / 'user-video.mp4').read_bytes() == b'keep'


def test_lock_parser_requires_exact_versions_hashes_and_rejects_duplicates(tmp_path, build):
    lock = tmp_path / 'requirements.lock'
    lock.write_text('--index-url https://pypi.org/simple\n--only-binary=:all:\nFoo_Bar==1.2 --hash=sha256:' + 'a'*64 + '\n')
    assert build.read_lock(lock) == [{'name': 'foo-bar', 'version': '1.2', 'wheel_sha256': 'a'*64}]
    for content in ('foo>=1\n', 'foo==1\n', 'foo==1 --hash=sha256:zz\n',
                    'foo==1 --hash=sha256:' + 'a'*64 + '\nFoo==1 --hash=sha256:' + 'a'*64 + '\n'):
        lock.write_text(content)
        with pytest.raises(build.BuildError):
            build.read_lock(lock)


def test_source_manifest_detects_changes_and_does_not_hash_game_or_local_data(tmp_path, build):
    root = source_fixture(tmp_path)
    private = root / 'resources' / 'private'; private.mkdir(parents=True)
    (private / 'pov.vpk').write_bytes(b'never package')
    first = build.source_manifest(root)
    assert all(not row['path'].startswith('resources/private') for row in first['files'])
    (root / 'src' / 'cs2pov' / 'app.py').write_text('raise SystemExit(1)')
    assert build.source_manifest(root)['sha256'] != first['sha256']


@pytest.mark.parametrize('relative', ['resources/private/pov.vpk', 'example.dem',
                                     'Counter-strike 2/recording.mp4', 'ffprobe.exe',
                                     'ffmpeg.exe', 'local-validation/settings.json'])
def test_package_inventory_rejects_forbidden_resources_and_never_deletes_them(tmp_path, relative, build):
    package = tmp_path / 'package'; package.mkdir()
    path = package / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'keep')
    with pytest.raises(build.BuildError, match='禁止|不允许'):
        build.artifact_inventory(package)
    assert path.read_bytes() == b'keep'


def test_artifact_inventory_is_bounded_and_detects_later_file_changes(tmp_path, build):
    package = tmp_path / 'package'; package.mkdir()
    file = package / 'XiamiPOV.exe'; file.write_bytes(b'binary')
    result = build.artifact_inventory(package)
    assert result['files'][0]['path'] == 'XiamiPOV.exe'
    assert result['files'][0]['bytes'] == 6
    with pytest.raises(build.BuildError, match='数量|限额'):
        build.artifact_inventory(package, max_files=0)
    file.write_bytes(b'changed')
    assert build.artifact_inventory(package)['sha256'] != result['sha256']


class FakeMetadata(dict):
    def get_all(self, key, default=None):
        value = self.get(key)
        return default if value is None else value if isinstance(value, list) else [value]


class FakeDistribution:
    def __init__(self, directory, *, version='1.0', with_license=True):
        self.version = version
        self.directory = directory
        self.metadata = FakeMetadata({'Name': 'fixture', 'License': 'MIT',
                                      'License-File': ['LICENSE.txt'] if with_license else []})
        self.files = [Path('fixture-1.0.dist-info/METADATA')]
        folder = directory / 'fixture-1.0.dist-info'; folder.mkdir(parents=True)
        (folder / 'METADATA').write_text('Name: fixture\nVersion: ' + version + '\nLicense: MIT\n')
        if with_license:
            self.files.append(Path('fixture-1.0.dist-info/licenses/LICENSE.txt'))
            (folder / 'licenses').mkdir()
            (folder / 'licenses' / 'LICENSE.txt').write_text('MIT fixture license')

    def locate_file(self, value):
        return self.directory / value


def test_license_collection_preserves_actual_text_hash_and_metadata(tmp_path, build):
    distribution = FakeDistribution(tmp_path / 'installed')
    destination = tmp_path / 'licenses'
    locked = [{'name': 'fixture', 'version': '1.0', 'wheel_sha256': 'a'*64}]
    result = build.collect_licenses(destination, locked, distribution_getter=lambda name: distribution,
                                    python_prefix=None)
    row = result['distributions'][0]
    assert row['version'] == '1.0' and row['wheel_sha256'] == 'a'*64
    assert row['license_files'] and row['metadata_sha256']
    copied = destination / row['license_files'][0]['packaged_path']
    assert copied.read_text() == 'MIT fixture license'
    assert row['license_files'][0]['sha256'] == build.file_sha256(copied)
    assert result['legal_approval'] is False


def test_missing_license_remains_an_explicit_release_gap_and_version_mismatch_blocks(tmp_path, build):
    distribution = FakeDistribution(tmp_path / 'installed', with_license=False)
    locked = [{'name': 'fixture', 'version': '1.0', 'wheel_sha256': 'a'*64}]
    result = build.collect_licenses(tmp_path / 'licenses', locked,
                                    distribution_getter=lambda name: distribution, python_prefix=None)
    assert result['missing_license_evidence'] == ['fixture==1.0']
    mismatch = FakeDistribution(tmp_path / 'mismatch', version='2.0')
    with pytest.raises(build.BuildError, match='版本'):
        build.collect_licenses(tmp_path / 'mismatch-licenses', locked,
                               distribution_getter=lambda name: mismatch, python_prefix=None)


def test_license_metadata_uses_own_dist_info_and_preserves_vendored_licenses(tmp_path, build):
    distribution = FakeDistribution(tmp_path / 'installed')
    vendor = distribution.directory / 'fixture' / '_vendor' / 'other-1.0.dist-info'
    vendor.mkdir(parents=True)
    (vendor / 'METADATA').write_text('Name: other\nVersion: 1.0\n')
    (vendor / 'LICENSE').write_text('vendored other license')
    distribution.files.extend([Path('fixture/_vendor/other-1.0.dist-info/METADATA'),
                               Path('fixture/_vendor/other-1.0.dist-info/LICENSE')])
    locked = [{'name': 'fixture', 'version': '1.0', 'wheel_sha256': 'a'*64}]
    destination = tmp_path / 'licenses'
    result = build.collect_licenses(destination, locked, distribution_getter=lambda name: distribution,
                                    python_prefix=None)
    row = result['distributions'][0]
    assert (destination / row['metadata_path']).read_text().startswith('Name: fixture')
    assert len(row['license_files']) == 2
    assert any(item['original_path'].endswith('other-1.0.dist-info/LICENSE') for item in row['license_files'])


def package_fixture(tmp_path, build):
    package = tmp_path / 'package'; package.mkdir()
    (package / 'XiamiPOV.exe').write_bytes(b'binary fixture')
    for name in ('shrimp.ico', 'replay_telemetry.js', 'pov_visibility.js'):
        resource = package / '_internal' / 'cs2pov' / 'resources' / name
        resource.parent.mkdir(parents=True, exist_ok=True); resource.write_bytes(b'asset')
    (package / 'INTERNAL_CANDIDATE.txt').write_text('INTERNAL CANDIDATE - NOT RELEASE\n')
    manifest = {'schema': 1, 'release': False, 'artifact': build.artifact_inventory(package),
                'third_party': {'distributions': [], 'missing_license_evidence': [], 'legal_approval': False}}
    (package / 'build-manifest.json').write_text(json.dumps(manifest))
    return package


def test_static_package_smoke_requires_manifest_assets_internal_marker_and_exact_hashes(tmp_path, build, smoke):
    package = package_fixture(tmp_path, build)
    report = smoke.validate_package(package)
    assert report['static_passed'] is True and report['release'] is False
    (package / 'XiamiPOV.exe').write_bytes(b'tampered')
    with pytest.raises(smoke.SmokeError, match='hash|变化|签名'):
        smoke.validate_package(package)


def test_smoke_never_runs_gui_or_accepts_runtime_result_without_required_evidence(tmp_path, build, smoke):
    package = package_fixture(tmp_path, build)
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        return type('Result', (), {'returncode': 0, 'stdout': json.dumps({'ok': True})})()
    with pytest.raises(smoke.SmokeError, match='证据|检查|报告'):
        smoke.runtime_smoke(package, tmp_path / 'smoke-data', runner=runner)
    assert commands and '--package-smoke' in commands[0] and '--data-dir' in commands[0]
    assert '--record' not in commands[0]


def test_windowed_runtime_smoke_reads_new_isolated_result_file_instead_of_stdout(tmp_path, build, smoke):
    package = package_fixture(tmp_path, build)
    data = tmp_path / 'smoke-data'
    def runner(command, **kwargs):
        assert kwargs['stdout'] is not None
        directory = Path(command[command.index('--data-dir') + 1]); directory.mkdir()
        assets = {name: build.file_sha256(package / '_internal' / 'cs2pov' / 'resources' / name)
                  for name in build.ASSETS}
        (directory / 'package-smoke-result.json').write_text(json.dumps({
            'schema': 1, 'ok': True, 'gui_started': False, 'game_started': False,
            'network_used': False, 'frozen': True, 'sqlite': True,
            'recording_triggered': False, 'worker_dispatch': ['output', 'demo'], 'stage': 'internal-only',
            'imports': {'PySide6': True, 'demoparser2': True, 'pandas': True,
                        'numpy': True, 'comtypes': True, 'polars': True,
                        '_polars_runtime_32': True, 'pyarrow': True, 'tqdm': True},
            'parser_dataframe_bridge': True, 'assets': assets,
            'extra_valid_field': 'allowed'}))
        return type('Result', (), {'returncode': 0, 'stdout': None})()
    assert smoke.runtime_smoke(package, data, runner=runner)['runtime_passed'] is True
    with pytest.raises(smoke.SmokeError, match='新的|隔离'):
        smoke.runtime_smoke(package, data, runner=runner)


def test_runtime_smoke_rejects_asset_hash_mismatch_missing_import_or_mutated_package(tmp_path, build, smoke):
    package = package_fixture(tmp_path, build)
    def runner(command, **kwargs):
        directory = Path(command[command.index('--data-dir') + 1]); directory.mkdir()
        (directory / 'package-smoke-result.json').write_text(json.dumps({
            'schema': 1, 'ok': True, 'gui_started': False, 'game_started': False,
            'network_used': False, 'frozen': True, 'sqlite': True,
            'recording_triggered': False, 'worker_dispatch': ['output', 'demo'], 'stage': 'internal-only',
            'imports': {'PySide6': True}, 'assets': {name: 'a'*64 for name in build.ASSETS}}))
        return type('Result', (), {'returncode': 0, 'stdout': None})()
    with pytest.raises(smoke.SmokeError, match='证据|hash|资源'):
        smoke.runtime_smoke(package, tmp_path / 'missing-import-data', runner=runner)


def test_malformed_frozen_worker_dispatch_uses_only_isolated_requests_and_results(tmp_path, build, smoke):
    package = package_fixture(tmp_path, build)
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        result = Path(command[command.index('--result') + 1])
        result.write_text(json.dumps({'error': 'invalid fixture request'}))
        return type('Result', (), {'returncode': 1})()
    report = smoke.worker_dispatch_smoke(package, tmp_path / 'workers', runner=runner)
    assert report['worker_dispatch_passed'] is True
    assert len(commands) == 2 and '--output-worker' in commands[0] and '--demo-worker' in commands[1]
    assert not any('--record' in command for command in commands)
    assert not (tmp_path / 'workers' / 'does-not-exist.dem').exists()


def test_frozen_worker_cannot_report_success_for_invalid_task(tmp_path, build, smoke):
    package = package_fixture(tmp_path, build)
    def runner(command, **kwargs):
        return type('Result', (), {'returncode': 0})()
    with pytest.raises(smoke.SmokeError, match='拒绝'):
        smoke.worker_dispatch_smoke(package, tmp_path / 'workers', runner=runner)


def fake_builder(command, **kwargs):
    dist = Path(command[command.index('--distpath') + 1]) / 'XiamiPOV'
    dist.mkdir(parents=True)
    (dist / 'XiamiPOV.exe').write_bytes(b'isolated candidate executable')
    for name in ('shrimp.ico', 'replay_telemetry.js', 'pov_visibility.js'):
        source = Path(command[-1]).parent / 'resources' / name
        target = dist / '_internal' / 'cs2pov' / 'resources' / name
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(source.read_bytes())
    return type('Result', (), {'returncode': 0})()


def test_run_build_creates_unique_internal_candidate_with_source_dependencies_and_tree_hash(tmp_path, build, smoke):
    root = source_fixture(tmp_path)
    distribution = FakeDistribution(tmp_path / 'installed')
    def licenses(destination, locked):
        return build.collect_licenses(destination, locked, distribution_getter=lambda name: distribution,
                                       python_prefix=None)
    first = build.run_build(root, runner=fake_builder, license_collector=licenses)
    second = build.run_build(root, runner=fake_builder, license_collector=licenses)
    assert first != second and first.is_dir() and second.is_dir()
    raw = json.loads((first / 'build-manifest.json').read_text(encoding='utf-8'))
    assert raw['release'] is False and raw['source']['sha256'] and raw['requirements_lock_sha256']
    assert raw['third_party']['distributions'][0]['wheel_sha256'] == 'a'*64
    assert smoke.validate_package(first)['static_passed'] is True


def test_failed_build_preserves_log_and_source_and_never_reports_success(tmp_path, build):
    root = source_fixture(tmp_path)
    source = (root / 'src' / 'cs2pov' / 'app.py').read_bytes()
    def fail(command, **kwargs):
        kwargs['stdout'].write(b'real compiler failure evidence')
        return type('Result', (), {'returncode': 1})()
    with pytest.raises(build.BuildError, match='失败'):
        build.run_build(root, runner=fail)
    candidates = list((root / '.build').iterdir())
    assert len(candidates) == 1
    assert (candidates[0] / 'build.log').read_bytes() == b'real compiler failure evidence'
    assert not (candidates[0] / 'build-result.json').exists()
    assert (root / 'src' / 'cs2pov' / 'app.py').read_bytes() == source


def test_source_changed_while_building_blocks_candidate_without_removing_files(tmp_path, build):
    root = source_fixture(tmp_path)
    def changed(command, **kwargs):
        result = fake_builder(command, **kwargs)
        (root / 'src' / 'cs2pov' / 'app.py').write_text('changed during analysis')
        return result
    with pytest.raises(build.BuildError, match='源码发生变化'):
        build.run_build(root, runner=changed)
    candidates = list((root / '.build').iterdir())
    assert (candidates[0] / 'dist' / 'XiamiPOV' / 'XiamiPOV.exe').is_file()
    assert not (candidates[0] / 'build-result.json').exists()


@pytest.mark.parametrize('distributions', [['corrupt row'], [{'license_files': [None]}], None])
def test_static_smoke_corrupt_license_payload_fails_clearly_without_traceback(tmp_path, build, smoke, distributions):
    package = package_fixture(tmp_path, build)
    path = package / 'build-manifest.json'
    raw = json.loads(path.read_text())
    raw['third_party']['distributions'] = distributions
    path.write_text(json.dumps(raw))
    with pytest.raises(smoke.SmokeError, match='许可证|清单|证据'):
        smoke.validate_package(package)
