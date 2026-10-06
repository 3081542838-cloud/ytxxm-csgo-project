"""First-run resources are fixed, verified and isolated from game files."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import urllib.error

import pytest

from cs2pov.services import resource_setup as setup


def test_connection_reset_retries_fixed_mirror_and_preserves_partial_bytes(tmp_path, monkeypatch):
    source = setup.HUD_SOURCES['template.vpk']
    target = tmp_path / 'template.vpk'
    calls = []
    def attempt(url, path, *, maximum):
        calls.append(url)
        if len(calls) == 1:
            path.write_bytes(b'interrupted')
            raise urllib.error.URLError(ConnectionResetError(10054, 'reset'))
        path.write_bytes(b'complete')
    monkeypatch.setattr(setup, '_download_once', attempt)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    setup._download(source.url, target, maximum=source.maximum)
    assert calls == [source.url, setup.HUD_MIRRORS[source.url]]
    assert target.read_bytes() == b'complete'
    assert [p.read_bytes() for p in tmp_path.glob('*.failed-*')] == [b'interrupted']


def test_network_retry_is_bounded_and_names_failed_source(tmp_path, monkeypatch):
    source = setup.FFPROBE_ARCHIVE
    calls = []
    def fail(url, path, *, maximum):
        calls.append(url)
        raise urllib.error.URLError('network reset')
    monkeypatch.setattr(setup, '_download_once', fail)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    with pytest.raises(setup.ResourceSetupError, match='gyan.dev'):
        setup._download(source.url, tmp_path / 'archive', maximum=source.maximum)
    assert calls == [source.url] * 3


def test_download_integrity_errors_are_not_retried(tmp_path, monkeypatch):
    calls = []
    def fail(url, path, *, maximum):
        calls.append(url)
        raise setup.ResourceSetupError('资源下载超过大小限制')
    monkeypatch.setattr(setup, '_download_once', fail)
    with pytest.raises(setup.ResourceSetupError, match='超过大小限制'):
        setup._download(setup.FFPROBE_ARCHIVE.url, tmp_path / 'archive', maximum=10)
    assert len(calls) == 1


def test_same_origin_https_redirect_is_bounded_and_downgrade_is_rejected():
    source = setup.FFPROBE_ARCHIVE.url
    handler = setup._VerifiedRedirect(source)
    request = setup.urllib.request.Request(source)
    destination = source + '?download=1'
    assert handler.redirect_request(request, None, 302, '', {}, destination).full_url == destination
    for unsafe in ('http://www.gyan.dev/file', 'https://evil.invalid/file',
                   'https://user:password@www.gyan.dev/file', 'https://www.gyan.dev:444/file'):
        with pytest.raises(setup.ResourceRedirectError):
            setup._VerifiedRedirect(source).redirect_request(request, None, 302, '', {}, unsafe)
    handler.redirect_request(request, None, 302, '', {}, destination)
    handler.redirect_request(request, None, 302, '', {}, destination)
    with pytest.raises(setup.ResourceRedirectError):
        handler.redirect_request(request, None, 302, '', {}, destination)


def test_unapproved_primary_redirect_uses_pinned_mirror(tmp_path, monkeypatch):
    source = setup.HUD_SOURCES['template.vpk']
    calls = []
    def attempt(url, path, *, maximum):
        calls.append(url)
        if url == source.url:
            raise setup.ResourceRedirectError('重定向不允许：' + url + ' -> https://evil.invalid/')
        path.write_bytes(b'complete')
    monkeypatch.setattr(setup, '_download_once', attempt)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    setup._download(source.url, tmp_path / 'download', maximum=source.maximum)
    assert calls == [source.url, setup.HUD_MIRRORS[source.url]]


@pytest.mark.parametrize('payload', [b'approved', b'tampered'])
def test_redirected_download_still_requires_original_content_hash(tmp_path, monkeypatch, payload):
    url = setup.HUD_SOURCES['template.vpk'].url
    destination = url + '?download=1'
    class Response(io.BytesIO):
        status = 200
        headers = {}
        def geturl(self): return destination
    class Opener:
        def open(self, request, *, timeout):
            return Response(payload)
    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *_: Opener())
    source = setup.Source(url, hashlib.sha256(b'approved').hexdigest(), 8, 10)
    target = tmp_path / 'verified'
    if payload == b'approved':
        assert setup._fetch(source, target, setup._download).read_bytes() == b'approved'
    else:
        with pytest.raises(setup.ResourceSetupError, match='SHA256'):
            setup._fetch(source, target, setup._download)
        assert target.read_bytes() == b'tampered'


REFERENCE = Path(__file__).resolve().parents[2] / 'resources/private/reference-f05c698c755dc7806855bb13a5c823dbf6a21de6'


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    root = tmp_path / '虾米pov'
    root.mkdir()
    probe = b'Approved fixed test executable\0'
    archive = b'Pinned test archive bytes\0'
    monkeypatch.setattr(setup, 'PROBE_BYTES', len(probe))
    monkeypatch.setattr(setup, 'PROBE_SHA256', hashlib.sha256(probe).hexdigest())
    monkeypatch.setattr(setup, 'FFPROBE_ARCHIVE', setup.Source(setup.FFPROBE_ARCHIVE.url,
        hashlib.sha256(archive).hexdigest(), len(archive), 45000000))
    sources = {
        setup.HUD_SOURCES['template.vpk'].url: (REFERENCE / 'pov--pov_voice_template.vpk').read_bytes(),
        setup.HUD_SOURCES['voice_hud_injection.js'].url: (REFERENCE / 'pov--voice_hud_injection.js').read_bytes(),
        setup.HUD_SOURCES['REFERENCE_LICENSE.txt'].url: (REFERENCE / 'LICENSE').read_bytes(),
        setup.HUD_SOURCES['REFERENCE_THIRD_PARTY_LICENSES.md'].url: (REFERENCE / 'THIRD_PARTY_LICENSES.md').read_bytes(),
        setup.FFPROBE_ARCHIVE.url: archive,
    }
    downloads, commands = [], []
    def download(url, target, *, maximum):
        downloads.append((url, target, maximum))
        assert url in sources
        assert len(sources[url]) <= maximum
        target.write_bytes(sources[url])
    def runner(command, **kwargs):
        commands.append(command)
        assert kwargs['check'] is True and kwargs['timeout'] <= 30
        assert kwargs['capture_output'] is True and kwargs['text'] is True
        output = ''
        if '-tf' in command:
            output = '\n'.join(setup._ARCHIVE_FILES) + '\n'
        elif '-xf' in command:
            directory = Path(command[command.index('-C') + 1])
            name = command[-1]
            assert name in setup._ARCHIVE_FILES
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(probe if name.endswith('/ffprobe.exe') else b'Fixed archive license or readme\n')
        else:
            assert command == [str(Path(command[0])), '-version']
            assert Path(command[0]).read_bytes() == probe
            output = 'ffprobe version ' + setup.PROBE_VERSION + '\n'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')
    return SimpleNamespace(root=root, probe=probe, archive=archive, sources=sources,
        downloads=downloads, commands=commands, download=download, runner=runner)


def run(value):
    return setup.prepare_resources(value.root, download=value.download, runner=value.runner)


def test_preparation_rebuilds_real_reviewed_hud_and_extracts_only_three_archive_files(preparation):
    value = preparation
    assert not setup.resources_ready(value.root)
    report = run(value)
    assert report['ok'] and report['network_used']
    assert report['installed'] == ['hud', 'probe']
    hud = value.root / 'resources/pov.vpk'
    assert hud.stat().st_size == 410376
    assert hashlib.sha256(hud.read_bytes()).hexdigest() == '98cffb9688c227a5f8edba50508e25cef0568a9e541ba6024d9d0eac24828af9'
    assert setup.resources_ready(value.root)
    assert (value.root / 'tools/ffprobe.exe').read_bytes() == value.probe
    assert set(report['resources']) == {'hud', 'probe'}
    assert len(value.downloads) == 5
    extracted = [command[-1] for command in value.commands if '-xf' in command]
    assert tuple(extracted) == setup._ARCHIVE_FILES
    assert 'Required Notice: Copyright (c) 2026 DrEAmSs59' in (value.root / 'licenses/hud-reference-LICENSE.txt').read_text()
    assert (value.root / 'licenses/ffprobe-LICENSE.txt').is_file()
    assert not (value.root / 'data/resource-setup.lock').exists()
    assert len(list(value.root.glob('.resource-setup-*'))) == 1
    assert json.loads((Path(report['stage']) / 'report.json').read_text(encoding='utf-8')) == report
    assert not (Path(report['stage']) / 'hud/template.vpk').exists()
    assert not (Path(report['stage']) / 'hud/pov.vpk').exists()
    assert not (Path(report['stage']) / 'probe/ffmpeg.7z').exists()
    assert not (Path(report['stage']) / 'probe' / setup._ARCHIVE_FILES[0]).exists()
    assert set(path for path in Path(report['stage']).rglob('*') if path.is_file()) == {Path(report['stage']) / 'report.json'}


def test_verified_resources_are_usable_offline_and_never_run_a_tool_again(preparation):
    value = preparation
    run(value)
    original = {path: path.read_bytes() for path in (value.root / 'resources/pov.vpk', value.root / 'tools/ffprobe.exe')}
    def forbidden(*args, **kwargs):
        raise AssertionError('Installed resources must not use network or subprocesses')
    report = setup.prepare_resources(value.root, download=forbidden, runner=forbidden)
    assert report['ok'] and not report['network_used']
    assert report['installed'] == [] and report['stage'] is None
    assert {path: path.read_bytes() for path in original} == original


@pytest.mark.parametrize('role', ['hud', 'probe'])
def test_tampered_installed_resource_is_preserved_and_not_redownloaded(preparation, role):
    value = preparation
    path = value.root / ('resources/pov.vpk' if role == 'hud' else 'tools/ffprobe.exe')
    path.parent.mkdir()
    damaged = b'keep this damaged user file'
    path.write_bytes(damaged)
    with pytest.raises(setup.ResourceSetupError, match='原文件保留'):
        setup.resources_ready(value.root)
    with pytest.raises(setup.ResourceSetupError, match='原文件保留'):
        run(value)
    assert path.read_bytes() == damaged
    assert not value.downloads and not value.commands
    assert not (value.root / 'data/resource-setup.lock').exists()


def test_missing_only_probe_does_not_fetch_or_replace_existing_hud(preparation):
    value = preparation
    run(value)
    hud = value.root / 'resources/pov.vpk'
    before = hud.stat().st_mtime_ns
    (value.root / 'tools/ffprobe.exe').unlink()
    value.downloads.clear()
    report = run(value)
    assert report['installed'] == ['probe']
    assert [item[0] for item in value.downloads] == [setup.FFPROBE_ARCHIVE.url]
    assert hud.stat().st_mtime_ns == before


def test_hash_failure_never_publishes_and_retry_uses_fresh_stage(preparation):
    value = preparation
    first = []
    def corrupt(url, target, *, maximum):
        first.append(target)
        target.write_bytes(b'not the approved source')
    with pytest.raises(setup.ResourceSetupError, match='证据保留'):
        setup.prepare_resources(value.root, download=corrupt, runner=value.runner)
    assert first[0].read_bytes() == b'not the approved source'
    assert not (value.root / 'resources/pov.vpk').exists()
    assert not (value.root / 'tools/ffprobe.exe').exists()
    report = run(value)
    assert report['ok'] and len(list(value.root.glob('.resource-setup-*'))) == 2
    assert first[0].parents[1] != Path(report['stage'])
    assert first[0].read_bytes() == b'not the approved source'


def test_archive_failure_after_hud_validation_does_not_publish_either_resource(preparation):
    value = preparation
    def fail_archive(url, target, *, maximum):
        if url == setup.FFPROBE_ARCHIVE.url:
            target.write_bytes(b'failed partial archive')
            raise OSError('Network interrupted')
        value.download(url, target, maximum=maximum)
    with pytest.raises(setup.ResourceSetupError, match='Network interrupted'):
        setup.prepare_resources(value.root, download=fail_archive, runner=value.runner)
    assert not (value.root / 'resources/pov.vpk').exists()
    assert not (value.root / 'tools/ffprobe.exe').exists()
    assert not (value.root / 'data/resource-setup.lock').exists()
    assert list(value.root.glob('.resource-setup-*/hud/pov.vpk'))
    assert list(value.root.glob('.resource-setup-*/probe/ffmpeg.7z'))


@pytest.mark.parametrize('failure', ['missing_entry', 'bad_executable', 'bad_version'])
def test_extraction_or_version_failure_never_publishes(preparation, failure):
    value = preparation
    def invalid(command, **kwargs):
        result = value.runner(command, **kwargs)
        if failure == 'missing_entry' and '-tf' in command:
            result.stdout = setup._ARCHIVE_FILES[0] + '\n'
        if failure == 'bad_executable' and '-xf' in command and command[-1].endswith('/ffprobe.exe'):
            directory = Path(command[command.index('-C') + 1])
            (directory / command[-1]).write_bytes(b'untrusted executable')
        if failure == 'bad_version' and command[-1] == '-version':
            result.stdout = 'ffprobe version other version'
        return result
    with pytest.raises(setup.ResourceSetupError):
        setup.prepare_resources(value.root, download=value.download, runner=invalid)
    assert not (value.root / 'resources/pov.vpk').exists()
    assert not (value.root / 'tools/ffprobe.exe').exists()


def test_conflicting_existing_license_is_preserved_without_resource_publish(preparation):
    value = preparation
    directory = value.root / 'licenses'
    directory.mkdir()
    target = directory / 'hud-reference-LICENSE.txt'
    target.write_text('Preserve the previous license')
    with pytest.raises(setup.ResourceSetupError, match='既有许可资料'):
        run(value)
    assert target.read_text() == 'Preserve the previous license'
    assert not (value.root / 'resources/pov.vpk').exists()
    assert not (value.root / 'tools/ffprobe.exe').exists()


def test_actual_live_lock_cannot_be_stolen(preparation):
    value = preparation
    with setup._lock(value.root):
        with pytest.raises(setup.ResourceSetupError, match='锁不可取得'):
            run(value)
        assert not value.downloads
    assert not (value.root / 'data/resource-setup.lock').exists()


def test_cancelled_real_child_lock_is_recovered_without_reusing_stage(preparation):
    value = preparation
    data = value.root / 'data'
    data.mkdir()
    path = data / 'resource-setup.lock'
    # os._exit models a killed worker: Qt gets no chance to unlock. The next
    # real QLockFile acquisition must recognize the dead owner, not elapsed age.
    code = ('import os,sys; from PySide6.QtCore import QLockFile; '
            'lock=QLockFile(sys.argv[1]); lock.setStaleLockTime(0); '
            'assert lock.tryLock(0); os._exit(0)')
    child = subprocess.run([sys.executable, '-c', code, str(path)],
        capture_output=True, timeout=30, check=True)
    assert child.returncode == 0 and path.is_file()
    stale = value.root / '.resource-setup-before-cancel'
    stale.mkdir()
    (stale / 'unverified.partial').write_bytes(b'partial evidence')
    report = run(value)
    assert report['ok'] and Path(report['stage']) != stale
    assert (stale / 'unverified.partial').read_bytes() == b'partial evidence'
    assert not path.exists()


def test_malformed_lock_is_preserved_and_blocks(preparation):
    value = preparation
    data = value.root / 'data'
    data.mkdir()
    path = data / 'resource-setup.lock'
    path.write_bytes(b'damaged lock without owner')
    with pytest.raises(setup.ResourceSetupError, match='锁不可取得'):
        run(value)
    assert path.read_bytes() == b'damaged lock without owner'
    assert not value.downloads


def test_redirect_and_arbitrary_download_address_are_rejected_without_network(tmp_path):
    with pytest.raises(setup.ResourceSetupError, match='重定向'):
        setup._NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://evil.invalid/file')
    with pytest.raises(setup.ResourceSetupError, match='非固定 HTTPS'):
        setup._download('https://raw.githubusercontent.com/other/arbitrary/file', tmp_path / 'output', maximum=10)
    assert not (tmp_path / 'output').exists()


def test_download_limits_stream_length_even_without_content_length(tmp_path, monkeypatch):
    target = tmp_path / 'partial'
    class Response(io.BytesIO):
        status = 200
        headers = {}
        def geturl(self): return setup.FFPROBE_ARCHIVE.url
    class Opener:
        def open(self, request, *, timeout):
            assert timeout == 60 and request.full_url == setup.FFPROBE_ARCHIVE.url
            return Response(b'12345678901')
    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(setup.ResourceSetupError, match='超过大小限制'):
        setup._download(setup.FFPROBE_ARCHIVE.url, target, maximum=10)
    assert target.exists() and target.stat().st_size <= 10


def test_existing_hash_mismatch_of_same_size_is_detected(preparation):
    value = preparation
    target = value.root / 'tools/ffprobe.exe'
    target.parent.mkdir()
    target.write_bytes(b'x' * len(value.probe))
    with pytest.raises(setup.ResourceSetupError, match='SHA256'):
        setup.resources_ready(value.root)
    assert target.read_bytes() == b'x' * len(value.probe)


def test_worker_unique_report_and_fixed_cli_preserve_existing_report(preparation, monkeypatch):
    value = preparation
    import cs2pov.adapters.worker_job as job
    containment = []
    monkeypatch.setattr(job, 'contain_current_worker', lambda: containment.append(True))
    monkeypatch.setattr(setup, 'prepare_resources', lambda root: {'schema': 1, 'ok': True, 'resources': {}})
    result = value.root / 'data/new-result.json'
    assert setup.worker_main(['--root', str(value.root), '--result', str(result)]) == 0
    raw = result.read_bytes()
    assert json.loads(raw)['ok'] is True and containment == [True]
    assert setup.worker_main(['--root', str(value.root), '--result', str(result)]) == 1
    assert result.read_bytes() == raw and containment == [True]
    assert setup.worker_main(['--root', str(value.root), '--result', str(value.root / 'outside.json')]) == 1
    with pytest.raises(SystemExit):
        setup.worker_main(['--root', str(value.root), '--result', str(result), '--url', 'https://other.invalid'])


def test_worker_failure_report_is_written_without_stdout_dependency(preparation, monkeypatch):
    value = preparation
    import cs2pov.adapters.worker_job as job
    monkeypatch.setattr(job, 'contain_current_worker', lambda: None)
    def fail(root): raise setup.ResourceSetupError('下载失败：保留原文件')
    monkeypatch.setattr(setup, 'prepare_resources', fail)
    result = value.root / 'data/failed.json'
    assert setup.worker_main(['--root', str(value.root), '--result', str(result)]) == 1
    assert json.loads(result.read_text(encoding='utf-8')) == {'schema': 1, 'ok': False, 'error': '下载失败：保留原文件'}


def test_symlink_resource_directory_is_rejected_without_download(preparation, monkeypatch):
    value = preparation
    # Simulate the Windows attribute check without requiring symlink privilege.
    target = value.root / 'resources'
    target.mkdir()
    original = Path.lstat
    def redirected(path):
        info = original(path)
        if path == target:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, 'lstat', redirected)
    with pytest.raises(setup.ResourceSetupError, match='reparse point'):
        setup.resources_ready(value.root)
    assert not value.downloads


def test_success_cleanup_preserves_unknown_files_in_own_stage(preparation):
    value = preparation
    unknown = []
    def download(url, target, *, maximum):
        value.download(url, target, maximum=maximum)
        if url == setup.FFPROBE_ARCHIVE.url:
            path = target.parent / 'unrelated-user-file.bin'
            path.write_bytes(b'preserve unknown contents')
            unknown.append(path)
    report = setup.prepare_resources(value.root, download=download, runner=value.runner)
    assert report['ok'] and unknown[0].read_bytes() == b'preserve unknown contents'
    assert not (Path(report['stage']) / 'probe/ffmpeg.7z').exists()
