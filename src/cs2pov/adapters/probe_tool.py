"""Authenticated local development ffprobe. Never searches PATH or downloads."""
from contextlib import contextmanager
import hashlib
from pathlib import Path

from cs2pov.adapters.video import read_lease, VideoError

PROBE_VERSION = '9.0.2-essentials_build-www.gyan.dev'
PROBE_SHA256 = 'f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52'
PROBE_BYTES = 105221120


def development_probe_path():
    return (Path(__file__).resolve().parents[3] / 'local-validation/tools/ffprobe-9.0.2/'
            'ffmpeg-9.0.2-essentials_build/bin/ffprobe.exe')


@contextmanager
def trusted_probe(path=None):
    """Keep the executable and parents protected until its child has exited."""
    path = Path(path) if path is not None else development_probe_path()
    if not path.is_absolute() or path.name.casefold() != 'ffprobe.exe':
        raise VideoError('请选择已审核的本机 ffprobe.exe；不会使用 PATH 中的程序。')
    if not path.is_file():
        raise VideoError('没有本地已审核的视频检查工具；开发工具不随当前内部包分发。')
    with read_lease(path) as signature:
        if signature.bytes != PROBE_BYTES:
            raise VideoError('视频检查工具大小与审核版本不一致。')
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(chunk)
        if hasher.hexdigest() != PROBE_SHA256:
            raise VideoError('视频检查工具校验失败，不能执行。')
        yield str(path)
