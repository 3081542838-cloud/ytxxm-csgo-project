"""Prepare pinned local recording resources without changing game files.

Only fixed upstream bytes are accepted. Partial downloads stay in a fresh
isolated stage; an installed file is never replaced by this service.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cs2pov.adapters.probe_tool import PROBE_BYTES, PROBE_SHA256, PROBE_VERSION
from cs2pov.adapters.video import read_lease
from cs2pov.adapters.vpk import build_vpk, parse_vpk, validate_resource
from cs2pov.storage.local_resources import HUD_BYTES, HUD_SHA256
from cs2pov.storage.settings import local_path


class ResourceSetupError(ValueError):
    pass


class ResourceRedirectError(ResourceSetupError):
    pass


@dataclass(frozen=True)
class Source:
    url: str
    sha256: str
    size: int
    maximum: int


REFERENCE_COMMIT = 'f05c698c755dc7806855bb13a5c823dbf6a21de6'
_REFERENCE = 'https://raw.githubusercontent.com/DrEAmSs59/CS2-insight-agent/' + REFERENCE_COMMIT + '/'
HUD_SOURCES = {
    'template.vpk': Source(_REFERENCE + 'pov/pov_voice_template.vpk',
        '1ecace2f72176a1217c9fb470961a939c3a63bf95d9b97ac202a2316fae72012', 755012, 800000),
    'voice_hud_injection.js': Source(_REFERENCE + 'pov/voice_hud_injection.js',
        '0c4d09e99324aff1c987729aa730b6b546f53c38a85743542adf016a69106928', 339773, 400000),
    'REFERENCE_LICENSE.txt': Source(_REFERENCE + 'LICENSE',
        '99bc238cbe8930f00f51ad4bd5cc4958fa229be7060ff85ed73c019fbb7a5d53', 4744, 10000),
    'REFERENCE_THIRD_PARTY_LICENSES.md': Source(_REFERENCE + 'THIRD_PARTY_LICENSES.md',
        '1789ebcf6f06873fa7c766726642577faae6919f8c7298de8264b0d6a9a0eb84', 5246, 10000),
}
HUD_MIRRORS = {source.url: source.url.replace(
    _REFERENCE, 'https://cdn.jsdelivr.net/gh/DrEAmSs59/CS2-insight-agent@' + REFERENCE_COMMIT + '/')
    for source in HUD_SOURCES.values()}
FFPROBE_ARCHIVE = Source(
    'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.7z',
    '4705843ccaaf54257c16ad90f3e952ece33c17df964ecf7bfdbb0f49c7171077',
    35430500, 45000000)
_ARCHIVE_PREFIX = 'ffmpeg-9.0.2-essentials_build/'
_ARCHIVE_FILES = tuple(_ARCHIVE_PREFIX + name for name in ('bin/ffprobe.exe', 'LICENSE', 'README.txt'))
HUD_PATHS = frozenset({
    'panorama/styles/hud/hudteamcounter.vcss_c',
    'panorama/styles/hud/hudteamcounter-equipmentinfo.vcss_c',
    'panorama/scripts/hud/huddemocontroller.vts_c',
})


def _safe(path):
    """Inspect each component before use; resolving would conceal redirection."""
    path = Path(path)
    local_path(str(path))
    if '..' in path.parts or (os.name == 'nt' and any(':' in item for item in path.parts[1:])):
        raise ResourceSetupError('资源路径不能包含上级跳转或替代数据流。')
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ResourceSetupError('资源路径包含链接、junction 或 reparse point，已阻断。')
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise ResourceSetupError('资源路径的父目录不是普通文件夹。')
    return path


def _root(value):
    root = _safe(value)
    if not root.is_dir():
        raise ResourceSetupError('便携应用文件夹不存在。')
    return root


def _directory(path):
    _safe(path)
    path.mkdir(parents=True, exist_ok=True)
    _safe(path)
    if not path.is_dir():
        raise ResourceSetupError('资源目标不是普通文件夹。')
    return path


def _check_file(path, sha256, size, maximum=None):
    _safe(path)
    if not path.is_file():
        raise ResourceSetupError('资源不是普通文件：' + str(path))
    with read_lease(path) as signature:
        if signature.bytes != size or signature.bytes > (maximum if maximum is not None else size):
            raise ResourceSetupError('资源大小与固定版本不一致，原文件保留：' + str(path))
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1048576), b''):
                digest.update(block)
        if digest.hexdigest() != sha256:
            raise ResourceSetupError('资源 SHA256 校验失败，原文件保留：' + str(path))
    return path


def _targets(root):
    return {'hud': root / 'resources/pov.vpk', 'probe': root / 'tools/ffprobe.exe'}


def _installed(root):
    targets = _targets(root)
    ready = {}
    for role, path in targets.items():
        _safe(path)
        if path.exists():
            expected = (HUD_SHA256, HUD_BYTES) if role == 'hud' else (PROBE_SHA256, PROBE_BYTES)
            _check_file(path, *expected)
            ready[role] = path
    return ready


def resources_ready(root):
    """Missing files return False; modified installed files remain a hard error."""
    return len(_installed(_root(root))) == 2


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise ResourceSetupError('固定资源地址发生重定向，已阻断；请更新应用或稍后重试。')


class _VerifiedRedirect(urllib.request.HTTPRedirectHandler):
    """At most three same-origin HTTPS hops; content is still hash checked."""
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.host = urllib.parse.urlsplit(source).hostname
        self.hops = 0

    def allows(self, url):
        try:
            parts = urllib.parse.urlsplit(url)
            return (not any(char in url for char in '\r\n\x00')
                    and parts.scheme == 'https' and parts.hostname == self.host
                    and parts.port in (None, 443) and parts.username is None
                    and parts.password is None and not parts.fragment)
        except ValueError:
            return False

    def redirect_request(self, request, fp, code, message, headers, new_url):
        if self.hops >= 3 or not self.allows(new_url):
            raise ResourceRedirectError(
                f'资源重定向未获允许或超过3次：{self.source} -> {new_url}')
        self.hops += 1
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _download_once(url, target, *, maximum):
    parsed = urllib.parse.urlsplit(url)
    fixed_urls = {source.url for source in HUD_SOURCES.values()} | set(HUD_MIRRORS.values()) | {FFPROBE_ARCHIVE.url}
    if url not in fixed_urls or parsed.scheme != 'https' or parsed.hostname not in {'raw.githubusercontent.com', 'cdn.jsdelivr.net', 'www.gyan.dev'}:
        raise ResourceSetupError('拒绝非固定 HTTPS 资源地址。')
    _safe(target)
    redirects = _VerifiedRedirect(url)
    opener = urllib.request.build_opener(redirects)
    with opener.open(urllib.request.Request(url, headers={'User-Agent': 'XiamiPOV-resource-setup/1'}), timeout=60) as response:
        if not redirects.allows(response.geturl()) or response.status != 200:
            raise ResourceSetupError('固定资源来源返回了异常地址或状态。')
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) > maximum):
            raise ResourceSetupError('资源下载长度超过限制。')
        total = 0
        with target.open('xb') as output:
            while block := response.read(min(1048576, maximum + 1 - total)):
                total += len(block)
                if total > maximum:
                    raise ResourceSetupError('资源下载超过大小限制，未发布。')
                output.write(block)
            output.flush()
            os.fsync(output.fileno())


def _download(url, target, *, maximum):
    """Retry transport failures only; pinned content validation remains mandatory."""
    target = _safe(target)
    urls = (url, HUD_MIRRORS.get(url, url), url)
    for index, address in enumerate(urls):
        try:
            _download_once(address, target, maximum=maximum)
            return
        except (urllib.error.URLError, ConnectionError, TimeoutError, ResourceRedirectError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                raise ResourceSetupError(f'资源来源 {address} 返回 HTTP {error.code}，未发布。') from error
            if index == len(urls) - 1:
                raise ResourceSetupError(f'资源下载失败（已尝试3次）：{address}：{error}') from error
            # Keep interrupted bytes separate; never append to unverified data
            # or overwrite an existing resource during a transport retry.
            _safe(target)
            if target.exists():
                if not target.is_file():
                    raise ResourceSetupError('下载临时目标不是普通文件。') from error
                failed = _safe(target.with_name(target.name + '.failed-' + uuid.uuid4().hex))
                target.rename(failed)
            time.sleep(0.5 * (index + 1))


def _fetch(source, path, download):
    download(source.url, path, maximum=source.maximum)
    return _check_file(path, source.sha256, source.size, source.maximum)


def _write_new(path, content):
    _safe(path)
    with path.open('xb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


@contextmanager
def _lock(root):
    from PySide6.QtCore import QLockFile
    data = _directory(root / 'data')
    path = _safe(data / 'resource-setup.lock')
    lock = QLockFile(str(path))
    # Never expire a live lock merely because a slow download took time. Qt
    # checks the owning process and can recover a genuinely dead worker lock.
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise ResourceSetupError('资源准备锁不可取得：另一任务仍在运行，或锁文件损坏。请先关闭其他虾米 POV；损坏锁需保留检查。')
    try:
        yield
    finally:
        _safe(path)
        lock.unlock()


def _hud(stage, download):
    directory = _directory(stage / 'hud')
    for name, source in HUD_SOURCES.items():
        _fetch(source, directory / name, download)
    archive = parse_vpk((directory / 'template.vpk').read_bytes())
    files = {entry.path: entry.data for entry in archive.entries if entry.path in HUD_PATHS}
    if set(files) != HUD_PATHS:
        raise ResourceSetupError('固定 HUD 模板缺少必要条目。')
    source = (directory / 'voice_hud_injection.js').read_bytes()
    replacement = _safe(Path(__file__).resolve().parents[1] / 'resources/pov_visibility.js').read_bytes()
    script = 'panorama/scripts/hud/huddemocontroller.vts_c'
    compiled = files[script]
    if compiled.count(source) != 1 or len(replacement) > len(source):
        raise ResourceSetupError('HUD 脚本无法按审核算法安全替换。')
    files[script] = compiled.replace(source, replacement.ljust(len(source), b' '), 1)
    target = directory / 'pov.vpk'
    _write_new(target, build_vpk(files))
    _check_file(target, HUD_SHA256, HUD_BYTES)
    validate_resource(target, HUD_SHA256, set(HUD_PATHS))
    notice = ('Required Notice: Copyright (c) 2026 DrEAmSs59\n'
        'Reference commit: ' + REFERENCE_COMMIT + '\n'
        'Changes: Removed input audio, alerts, voice/input/radar tracks and extra playback controls; HUD visibility only.\n'
        'Personal/noncommercial use. Source game resource rights are not independently granted by the application.\n')
    _write_new(directory / 'CHANGES.txt', notice.encode('utf-8'))
    return target, {
        'hud-reference-LICENSE.txt': directory / 'REFERENCE_LICENSE.txt',
        'hud-reference-THIRD_PARTY_LICENSES.md': directory / 'REFERENCE_THIRD_PARTY_LICENSES.md',
        'hud-CHANGES.txt': directory / 'CHANGES.txt',
    }


def _tar_path():
    if os.name != 'nt':
        raise ResourceSetupError('自动资源准备目前仅支持 Windows。')
    buffer = ctypes.create_unicode_buffer(32768)
    function = ctypes.WinDLL('kernel32', use_last_error=True).GetSystemDirectoryW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    function.restype = ctypes.c_uint
    count = function(buffer, len(buffer))
    if not count or count >= len(buffer):
        raise ResourceSetupError('无法确认 Windows 系统工具目录。')
    path = _safe(Path(buffer.value) / 'tar.exe')
    if not path.is_file():
        raise ResourceSetupError('系统缺少 tar.exe，请更新 Windows 后重试。')
    return str(path)


def _run(runner, command, *, timeout):
    result = runner(command, capture_output=True, text=True, timeout=timeout, check=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode != 0 or len(result.stdout or '') > 1048576 or len(result.stderr or '') > 1048576:
        raise ResourceSetupError('资源工具检查失败或输出超过限制。')
    return result


def _probe(stage, download, runner):
    directory = _directory(stage / 'probe')
    archive = _fetch(FFPROBE_ARCHIVE, directory / 'ffmpeg.7z', download)
    tar = _tar_path()
    listing = _run(runner, [tar, '-tf', str(archive)], timeout=30).stdout.splitlines()
    if not all(listing.count(name) == 1 for name in _ARCHIVE_FILES):
        raise ResourceSetupError('固定 ffprobe 压缩包结构不一致。')
    for name in _ARCHIVE_FILES:
        _run(runner, [tar, '-xf', str(archive), '-C', str(directory), name], timeout=30)
    prefix = directory / _ARCHIVE_PREFIX
    executable = prefix / 'bin/ffprobe.exe'
    # Keep the authenticated executable and ancestors leased through execution;
    # an earlier checksum alone cannot authorize bytes replaced afterwards.
    with read_lease(executable):
        _check_file(executable, PROBE_SHA256, PROBE_BYTES)
        result = _run(runner, [str(executable), '-version'], timeout=10)
    if not result.stdout.startswith('ffprobe version ' + PROBE_VERSION):
        raise ResourceSetupError('ffprobe 固定版本不一致，未发布。')
    evidence = {}
    for source_name, target_name in (('LICENSE', 'ffprobe-LICENSE.txt'), ('README.txt', 'ffprobe-README.txt')):
        path = _safe(prefix / source_name)
        if not path.is_file() or not 1 <= path.stat().st_size <= 1048576:
            raise ResourceSetupError('ffprobe 许可资料缺失或异常，未发布。')
        evidence[target_name] = path
    return executable, evidence


def _publish(source, destination):
    """Exclusive copy to a sibling stage; Windows rename cannot replace a file."""
    _directory(destination.parent)
    _safe(destination)
    if destination.exists():
        raise ResourceSetupError('资源目标在准备期间出现，原文件保留：' + str(destination))
    temporary = destination.with_name('.' + destination.name + '.' + uuid.uuid4().hex + '.tmp')
    _safe(source)
    with source.open('rb') as incoming, temporary.open('xb') as output:
        while block := incoming.read(1048576):
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    _safe(destination)
    # On Windows Path.rename fails if the destination exists. On other hosts
    # use an exclusive hard link so an existing file can never be replaced.
    if os.name == 'nt':
        temporary.rename(destination)
    else:
        os.link(temporary, destination)
        temporary.unlink()


def _cleanup_success(stage, roles):
    """Remove only files this successful attempt explicitly created/extracted."""
    paths = []
    if 'hud' in roles:
        paths.extend(stage / 'hud' / name for name in (*HUD_SOURCES, 'pov.vpk', 'CHANGES.txt'))
    if 'probe' in roles:
        paths.append(stage / 'probe/ffmpeg.7z')
        paths.extend(stage / 'probe' / name for name in _ARCHIVE_FILES)
    warnings = []
    for path in paths:
        try:
            _safe(path)
            if path.exists():
                if not path.is_file():
                    raise ResourceSetupError('本次下载清理目标不是普通文件。')
                path.unlink()
        except (OSError, ValueError) as error:
            warnings.append('已准备资源；下载临时文件清理失败，保留：' + str(path) + '：' + str(error))
    return warnings


def prepare_resources(root, *, download=None, runner=None):
    """Download only missing roles, validate everything, then publish locally."""
    root = _root(root)
    download = download or _download
    runner = runner or subprocess.run
    with _lock(root):
        existing = _installed(root)
        if len(existing) == 2:
            return {'schema': 1, 'ok': True, 'resources': {role: str(path) for role, path in existing.items()},
                    'installed': [], 'network_used': False, 'stage': None}
        stage = _directory(root / ('.resource-setup-' + uuid.uuid4().hex))
        pending, evidence = {}, {}
        try:
            for role, producer in (('hud', lambda: _hud(stage, download)),
                                   ('probe', lambda: _probe(stage, download, runner))):
                if role not in existing:
                    pending[role], materials = producer()
                    evidence.update(materials)
            # All required resource bytes have passed validation before any
            # installed resource is published. Preserve conflicting material.
            licenses = _directory(root / 'licenses')
            for name, source in evidence.items():
                destination = _safe(licenses / name)
                if destination.exists():
                    info = destination.stat()
                    if info.st_size > 1048576 or destination.read_bytes() != source.read_bytes():
                        raise ResourceSetupError('既有许可资料与固定来源不一致，原文件保留：' + str(destination))
            for name, source in evidence.items():
                destination = licenses / name
                if not destination.exists():
                    _publish(source, destination)
            targets = _targets(root)
            for role, source in pending.items():
                expected = (HUD_SHA256, HUD_BYTES) if role == 'hud' else (PROBE_SHA256, PROBE_BYTES)
                with read_lease(source):
                    _check_file(source, *expected)
                    _publish(source, targets[role])
            ready = _installed(root)
            report = {'schema': 1, 'ok': len(ready) == 2, 'resources': {role: str(path) for role, path in ready.items()},
                      'installed': list(pending), 'network_used': True, 'stage': str(stage),
                      'reference_commit': REFERENCE_COMMIT, 'ffprobe_archive_sha256': FFPROBE_ARCHIVE.sha256}
            warnings = _cleanup_success(stage, pending)
            if warnings:
                report['cleanup_warnings'] = warnings
            _write_new(stage / 'report.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
            return report
        except Exception as error:
            raise ResourceSetupError('资源准备失败，下载证据保留在 ' + str(stage) + '：' + str(error)) from error


def worker_main(argv=None):
    """Frozen/source worker dispatch: fixed URLs only, unique file report."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = _root(args.root)
        result = _safe(args.result)
        result.relative_to(root / 'data')
        if result.suffix != '.json' or result.exists():
            raise ResourceSetupError('资源准备报告必须是 data 内尚不存在的 JSON 文件。')
        _directory(result.parent)
    except Exception:
        return 1
    try:
        from cs2pov.adapters.worker_job import contain_current_worker
        contain_current_worker()
        report = prepare_resources(root)
    except Exception as error:
        report = {'schema': 1, 'ok': False, 'error': str(error)}
    try:
        _write_new(result, json.dumps(report, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except Exception:
        return 1
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(worker_main())
