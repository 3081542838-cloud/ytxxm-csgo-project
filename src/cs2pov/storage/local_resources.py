"""References to audited, user-owned local files; never bundles resources.

The HUD hash is the experimental candidate recorded in
docs/validation/stage-1-resource-review.md. Matching it proves reviewed bytes,
not CS2 version compatibility or permission to redistribute the asset.
The executable policy comes from probe_tool and is never selected from PATH.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType

from cs2pov.adapters.probe_tool import PROBE_BYTES, PROBE_SHA256
from cs2pov.adapters.video import VideoError, read_data_lease, read_lease
from cs2pov.storage.settings import DataError, DataVersionError, local_path
from cs2pov.storage.transaction import atomic_write


HUD_SHA256 = '98cffb9688c227a5f8edba50508e25cef0568a9e541ba6024d9d0eac24828af9'
HUD_BYTES = 410376
MAX_REGISTRATION_BYTES = 32768
_APPROVED = MappingProxyType({'hud': (HUD_SHA256, HUD_BYTES), 'probe': (PROBE_SHA256, PROBE_BYTES)})


class LocalResourceError(DataError):
    pass


@dataclass(frozen=True)
class LocalResourceReference:
    path: str
    sha256: str
    bytes: int


def _approval(role):
    if type(role) is not str or role not in _APPROVED:
        raise LocalResourceError('本地资源只能选择 hud 或 probe。')
    return _APPROVED[role]


def _path(value):
    if not isinstance(value, (str, Path)) or not str(value):
        raise LocalResourceError('请选择明确的本地资源文件。')
    try:
        local_path(str(value))
    except DataError as error:
        raise LocalResourceError('本地资源必须使用本机磁盘的绝对路径。') from error
    path = Path(value)
    if '..' in path.parts or (os.name == 'nt' and any(':' in part for part in path.parts[1:])):
        raise LocalResourceError('本地资源路径不能包含上级跳转或替代数据流。')
    # Do not resolve: it would hide a junction or symbolic link.
    return path.absolute()


def _resource_path(role, value):
    _approval(role)
    path = _path(value)
    if (role == 'hud' and path.suffix.casefold() != '.vpk') or (role == 'probe' and path.name.casefold() != 'ffprobe.exe'):
        raise LocalResourceError('请选择 HUD 的 .vpk 文件或已审核的 ffprobe.exe。')
    return path


def _reference(role, raw):
    expected_sha, expected_bytes = _approval(role)
    if type(raw) is not dict or set(raw) != {'path', 'sha256', 'bytes'}:
        raise LocalResourceError('本地资源引用结构不完整或包含未知字段。')
    if (type(raw['path']) is not str or type(raw['sha256']) is not str
            or re.fullmatch(r'[0-9a-f]{64}', raw['sha256']) is None
            or type(raw['bytes']) is not int or raw['bytes'] != expected_bytes
            or raw['sha256'] != expected_sha):
        raise LocalResourceError('本地资源引用不是已审核的固定版本。')
    path = _resource_path(role, raw['path'])
    return LocalResourceReference(str(path), raw['sha256'], raw['bytes'])


def _verify(role, reference):
    reference = _reference(role, asdict(reference))
    path = _resource_path(role, reference.path)
    try:
        if not _ordinary_config_path(path):
            raise LocalResourceError('本地资源文件不存在。')
        # Windows SHARE_READ only for the source, plus ancestor leases: no
        # content writes or file/parent replacement while its hash is read.
        with read_lease(path) as signature:
            if signature.bytes != reference.bytes:
                raise LocalResourceError('本地资源文件大小已改变或不是已审核版本。')
            digest, total = hashlib.sha256(), 0
            with path.open('rb') as stream:
                while True:
                    chunk = stream.read(min(1_048_576, reference.bytes + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > reference.bytes:
                        raise LocalResourceError('本地资源读取超过审核大小。')
                    digest.update(chunk)
            if total != reference.bytes or digest.hexdigest() != reference.sha256:
                raise LocalResourceError('本地资源 SHA256 已改变或不是已审核版本。')
    except (OSError, VideoError) as error:
        raise LocalResourceError('本地资源不是可安全读取的普通文件：' + str(error)) from error
    return path


def _ordinary_config_path(path):
    """Reject all reparse attributes, including types is_junction misses."""
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component == path:
                return False
            raise LocalResourceError('本地资源登记目录不存在。')
        except OSError as error:
            raise LocalResourceError('本地资源登记路径不可读取。') from error
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise LocalResourceError('本地资源登记路径包含链接或 reparse point。')
        if component == path:
            if not stat.S_ISREG(info.st_mode):
                raise LocalResourceError('本地资源登记必须是普通文件。')
        elif not stat.S_ISDIR(info.st_mode):
            raise LocalResourceError('本地资源登记的父路径不是文件夹。')
    return True


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LocalResourceError('本地资源登记包含重复字段。')
        result[key] = value
    return result


def _constant(_value):
    raise LocalResourceError('本地资源登记包含非有限数字。')


def _decode(raw):
    if type(raw) is not dict or type(raw.get('schema')) is not int:
        raise LocalResourceError('本地资源登记版本或结构无效。')
    if raw['schema'] > 1:
        raise DataVersionError('本地资源登记版本较新，不能覆盖。')
    if raw['schema'] != 1 or set(raw) != {'schema', 'resources'} or type(raw['resources']) is not dict:
        raise LocalResourceError('本地资源登记结构不完整或包含未知字段。')
    return {role: _reference(role, value) for role, value in raw['resources'].items()}


class LocalResources:
    """Independent strict registry. Missing files start empty; corruption does not.

    ``load`` validates references without treating them as live approval.
    ``get_verified_path`` and ``save`` always re-read and hash the real source.
    Consumers must still validate/lease the source at the time they use it.
    """

    def __init__(self, path):
        self.path = _path(path)
        if self.path.name != 'local-resources.json':
            raise LocalResourceError('本地资源必须使用独立的 local-resources.json 登记。')

    def load(self):
        if not _ordinary_config_path(self.path):
            return {}
        try:
            with read_data_lease(self.path) as signature:
                if signature.bytes > MAX_REGISTRATION_BYTES:
                    raise LocalResourceError('本地资源登记超过大小限制。')
                with self.path.open('rb') as stream:
                    content = stream.read(MAX_REGISTRATION_BYTES + 1)
                if len(content) > MAX_REGISTRATION_BYTES:
                    raise LocalResourceError('本地资源登记超过大小限制。')
            raw = json.loads(content.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            return _decode(raw)
        except DataVersionError:
            raise
        except (OSError, VideoError, ValueError, UnicodeError, RecursionError) as error:
            raise LocalResourceError('本地资源登记无法读取；原文保留，请检查文件。') from error

    def pick(self, role, path):
        sha, size = _approval(role)
        path = _resource_path(role, path)
        reference = LocalResourceReference(str(path), sha, size)
        _verify(role, reference)
        return reference

    def save(self, role, path_or_reference):
        _approval(role)
        references = self.load()  # A corrupt/future version must not be reset.
        if type(path_or_reference) is LocalResourceReference:
            reference = _reference(role, asdict(path_or_reference))
            _verify(role, reference)
        else:
            reference = self.pick(role, path_or_reference)
        # Resource reads can take time; reject a concurrent registry change.
        if self.load() != references:
            raise LocalResourceError('本地资源登记在审核期间改变，请重新选择。')
        references[role] = reference
        content = json.dumps({'schema': 1, 'resources': {key: asdict(value) for key, value in references.items()}},
                             ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'
        if len(content) > MAX_REGISTRATION_BYTES:
            raise LocalResourceError('本地资源登记超过大小限制。')
        _ordinary_config_path(self.path)
        try:
            atomic_write(self.path, content)
        except OSError as error:
            raise LocalResourceError('本地资源登记保存失败；既有登记保留。') from error
        return reference

    def get_verified_path(self, role):
        _approval(role)
        references = self.load()
        if role not in references:
            raise LocalResourceError('尚未登记本机的已审核 ' + role + ' 资源。')
        return _verify(role, references[role])
