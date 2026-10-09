"""Read only the installed game's VPK index and a bounded overview text entry."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import struct
import zlib

from cs2pov.storage.settings import DataError
from cs2pov.storage.transaction import no_redirection


@dataclass(frozen=True)
class Overview:
    map: str
    pos_x: float
    pos_y: float
    scale: float
    sha256: str
    texture: str

    def percent(self, x, y):
        return ((x - self.pos_x) / (self.scale * 1024) * 100,
                (self.pos_y - y) / (self.scale * 1024) * 100)


def parse_overview(map_name, raw, texture):
    import math
    if not re.fullmatch(r'de_[a-z0-9_]{1,48}', map_name) or len(raw) > 65536:
        raise DataError('雷达地图名称或坐标文件无效。')
    try:
        text = re.sub(r'//[^\r\n]*', '', raw.decode('utf-8-sig'))
        pairs = re.findall(r'"([^"\r\n]+)"\s+"([^"\r\n]*)"', text)
        values = {}
        for key, value in pairs:
            if key in ('pos_x', 'pos_y', 'scale'):
                if key in values:
                    raise DataError('雷达地图坐标字段重复。')
                values[key] = float(value)
        if set(values) != {'pos_x', 'pos_y', 'scale'}:
            raise DataError('雷达地图坐标字段缺失。')
        if not all(math.isfinite(v) for v in values.values()) or not 0 < values['scale'] <= 100:
            raise DataError('雷达地图坐标范围无效。')
        expected = 'panorama/images/overheadmaps/' + map_name + '_radar_psd.vtex_c'
        if texture != expected:
            raise DataError('当前游戏没有可验证的雷达背景资源。')
        return Overview(map_name, **values, sha256=hashlib.sha256(raw).hexdigest(),
                        texture='s2r://' + texture.removesuffix('_c'))
    except (UnicodeError, ValueError) as error:
        raise DataError('无法读取游戏地图坐标。') from error


def read_overview(installation, map_name):
    if not isinstance(map_name, str) or not re.fullmatch(r'de_[a-z0-9_]{1,48}', map_name):
        raise DataError('雷达地图名称无效。')
    root = Path(installation) / 'game/csgo'
    index = root / 'pak01_dir.vpk'
    no_redirection(index)
    with index.open('rb') as stream:
        header = stream.read(28)
        if len(header) != 28:
            raise DataError('游戏资源目录截断。')
        signature, version, size = struct.unpack_from('<3I', header)
        if signature != 0x55AA1234 or version != 2 or not 0 < size <= 8 * 1024 * 1024:
            raise DataError('不支持此游戏资源目录。')
        tree = stream.read(size)
        if len(tree) != size:
            raise DataError('游戏资源目录树截断。')
    position, overview, texture, entries = 0, None, None, 0
    wanted = 'resource/overviews/' + map_name + '.txt'
    image = 'panorama/images/overheadmaps/' + map_name + '_radar_psd.vtex_c'

    def string():
        nonlocal position
        end = tree.find(b'\0', position)
        if end < 0 or end - position > 1024:
            raise DataError('游戏资源目录字符串无效。')
        value = tree[position:end].decode('utf-8')
        position = end + 1
        return value

    try:
        while extension := string():
            while directory := string():
                while name := string():
                    entries += 1
                    if entries > 250000 or position + 18 > size:
                        raise DataError('游戏资源目录条目无效。')
                    crc, preload, archive, offset, length, terminator = struct.unpack_from('<IHHIIH', tree, position)
                    position += 18
                    if terminator != 0xffff or position + preload > size:
                        raise DataError('游戏资源目录数据无效。')
                    prefix = tree[position:position + preload]
                    position += preload
                    path = (directory + '/' if directory != ' ' else '') + name + ('.' + extension if extension != ' ' else '')
                    if path == image:
                        if texture is not None:
                            raise DataError('游戏雷达纹理条目重复。')
                        texture = path
                    if path != wanted:
                        continue
                    if overview is not None or preload + length > 65536:
                        raise DataError('游戏雷达坐标条目重复或过大。')
                    part = index if archive == 0x7fff else root / f'pak01_{archive:03d}.vpk'
                    no_redirection(part)
                    at = 28 + size + offset if archive == 0x7fff else offset
                    with part.open('rb') as stream:
                        stream.seek(at)
                        overview = prefix + stream.read(length)
                    if len(overview) != preload + length or zlib.crc32(overview) & 0xffffffff != crc:
                        raise DataError('游戏雷达坐标完整性校验失败。')
        if position != size or overview is None or texture is None:
            raise DataError('当前地图缺少雷达坐标或背景。')
        return parse_overview(map_name, overview, texture)
    except (UnicodeError, struct.error, OSError) as error:
        raise DataError('无法只读取得游戏雷达资源。') from error
