"""Frozen, serializable requests for read-only recording output workers.

These results identify candidate files and their decodable streams. Neither
filesystem timestamps nor ffprobe metadata prove what the video contains.
SQLite and workflow state belong exclusively to the caller's commit step.
"""

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re

from cs2pov.adapters.video import (
    FileSignature, VideoMetadata, VIDEO_SUFFIXES, candidates_since,
    probe_video, wait_stable,
)
from cs2pov.storage.settings import DataError, local_path


class OutputValidationError(DataError):
    pass


# NVIDIA may finish writing immediately after the stop input. This bounded
# candidate window is not an allowance for extra video content or ownership.
STOP_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class OutputDiscoveryRequest:
    task_id: str
    request_id: str
    output_directory: str
    started_wall: float
    stopped_wall: float
    baseline: tuple[FileSignature, ...]


@dataclass(frozen=True)
class OutputDiscoveryResult:
    task_id: str
    request_id: str
    candidates: tuple[FileSignature, ...]


@dataclass(frozen=True)
class OutputValidationRequest:
    task_id: str
    request_id: str
    output_directory: str
    candidate: FileSignature
    started_wall: float
    stopped_wall: float


@dataclass(frozen=True)
class OutputValidationResult:
    task_id: str
    request_id: str
    metadata: VideoMetadata


def output_json_value(value):
    """Return a JSON-native value; decoding remains strict on both sides."""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: output_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [output_json_value(item) for item in value]
    return value


def candidate_id_for(task_id, signature):
    """Bind one full file version to the originating recording task."""
    _id(task_id)
    signature = decode_file_signature(output_json_value(signature))
    value = {'task_id': task_id, 'signature': output_json_value(signature)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _object(raw, fields):
    if type(raw) is not dict or set(raw) != set(fields):
        raise OutputValidationError('录制输出任务结构不完整或包含未知字段。')
    return raw


def _id(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise OutputValidationError('录制输出任务标识无效。')
    return value


def _number(value, *, positive=False):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0 and (not positive or value > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise OutputValidationError('录制输出时间或长度无效。')
    return float(value)


def _integer(value, *, positive=False, limit=2**128-1):
    if type(value) is not int or value < 0 or value > limit or (positive and value <= 0):
        raise OutputValidationError('录制输出文件属性无效。')
    return value


def _path(value):
    if type(value) is not str or not value:
        raise OutputValidationError('录制输出路径无效。')
    try:
        local_path(value)
    except DataError as error:
        raise OutputValidationError('录制输出路径必须是本机绝对路径。') from error
    path = Path(value)
    if '..' in path.parts or (os.name == 'nt' and any(':' in part for part in path.parts[1:])):
        raise OutputValidationError('录制输出路径不能包含上级跳转。')
    return path


def decode_file_signature(raw):
    raw = _object(raw, ('path', 'bytes', 'mtime_ns', 'ctime_ns', 'identity', 'change_ns'))
    identity = raw['identity']
    if type(identity) not in (list, tuple) or len(identity) != 3 or identity[0] not in ('windows', 'stat'):
        raise OutputValidationError('候选视频缺少可靠的文件身份。')
    identity = (identity[0], _integer(identity[1], positive=True), _integer(identity[2], positive=True))
    change = raw['change_ns']
    if change is not None:
        change = _integer(change)
    if identity[0] == 'windows' and change is None:
        raise OutputValidationError('候选视频缺少文件变化时间。')
    return FileSignature(_path(raw['path']), _integer(raw['bytes']),
                         _integer(raw['mtime_ns']), _integer(raw['ctime_ns']), identity, change)


def _window(raw):
    start, stop = _number(raw['started_wall']), _number(raw['stopped_wall'])
    if stop < start:
        raise OutputValidationError('录制停止时间早于开始时间。')
    return start, stop


def _scope(signature, directory):
    try:
        relative = signature.path.relative_to(directory)
    except ValueError as error:
        raise OutputValidationError('候选视频不属于冻结的输出目录。') from error
    if len(relative.parts) not in (1, 2) or signature.path.suffix.casefold() not in VIDEO_SUFFIXES:
        raise OutputValidationError('候选视频超出允许的目录层级或文件格式。')
    return signature


def _signature_list(raw):
    if type(raw) not in (list, tuple) or len(raw) > 4096:
        raise OutputValidationError('录制输出文件列表无效或超过限制。')
    items = tuple(decode_file_signature(item) for item in raw)
    if len({item.path for item in items}) != len(items):
        raise OutputValidationError('录制输出文件列表包含重复路径。')
    return items


def decode_discovery_request(raw):
    raw = _object(raw, OutputDiscoveryRequest.__dataclass_fields__)
    directory = _path(raw['output_directory'])
    start, stop = _window(raw)
    baseline = tuple(_scope(item, directory) for item in _signature_list(raw['baseline']))
    return OutputDiscoveryRequest(_id(raw['task_id']), _id(raw['request_id']), str(directory), start, stop, baseline)


def decode_discovery_result(raw):
    raw = _object(raw, OutputDiscoveryResult.__dataclass_fields__)
    return OutputDiscoveryResult(_id(raw['task_id']), _id(raw['request_id']), _signature_list(raw['candidates']))


def decode_validation_request(raw):
    raw = _object(raw, OutputValidationRequest.__dataclass_fields__)
    directory = _path(raw['output_directory'])
    start, stop = _window(raw)
    candidate = _scope(decode_file_signature(raw['candidate']), directory)
    return OutputValidationRequest(_id(raw['task_id']), _id(raw['request_id']), str(directory), candidate, start, stop)


def decode_video_metadata(raw):
    raw = _object(raw, ('path', 'duration', 'width', 'height', 'video_streams', 'audio_streams', 'signature'))
    signature = decode_file_signature(raw['signature'])
    path = _path(raw['path'])
    if path != signature.path or signature.bytes <= 0:
        raise OutputValidationError('视频探测结果与冻结文件不一致。')
    return VideoMetadata(path, _number(raw['duration'], positive=True),
                         _integer(raw['width'], positive=True, limit=65536),
                         _integer(raw['height'], positive=True, limit=65536),
                         _integer(raw['video_streams'], positive=True, limit=4096),
                         _integer(raw['audio_streams'], positive=True, limit=4096), signature)


def decode_validation_result(raw):
    raw = _object(raw, OutputValidationResult.__dataclass_fields__)
    return OutputValidationResult(_id(raw['task_id']), _id(raw['request_id']), decode_video_metadata(raw['metadata']))


def _cancelled(cancel):
    if cancel is not None and cancel():
        raise OutputValidationError('录制输出检查已取消。')


def discover_output_files(request, *, finder=candidates_since, cancel=None):
    request = decode_discovery_request(output_json_value(request))
    _cancelled(cancel)
    candidates = finder(Path(request.output_directory), {item.path: item for item in request.baseline},
                        started_wall=request.started_wall, stopped_wall=request.stopped_wall + STOP_GRACE_SECONDS,
                        cancel=cancel)
    _cancelled(cancel)
    result = decode_discovery_result(output_json_value(OutputDiscoveryResult(request.task_id, request.request_id, tuple(candidates))))
    for item in result.candidates:
        _scope(item, Path(request.output_directory))
    return result


def validate_output(request, *, stabilize=wait_stable, probe=probe_video, cancel=None, ffprobe='ffprobe'):
    request = decode_validation_request(output_json_value(request))
    _cancelled(cancel)
    stable = stabilize(request.candidate.path, expected=request.candidate, cancel=cancel)
    stable = decode_file_signature(output_json_value(stable))
    if stable.path != request.candidate.path or stable.identity != request.candidate.identity:
        raise OutputValidationError('候选视频已被替换，不能继续检查。')
    _cancelled(cancel)
    metadata = probe(stable.path, expected=stable, cancel=cancel, ffprobe=ffprobe)
    _cancelled(cancel)
    result = decode_validation_result(output_json_value(OutputValidationResult(request.task_id, request.request_id, metadata)))
    if result.metadata.signature != stable:
        raise OutputValidationError('视频探测后的文件身份或内容属性发生变化。')
    return result
