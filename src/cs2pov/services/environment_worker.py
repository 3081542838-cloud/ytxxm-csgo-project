"""Read-only current-environment worker protocol; no SQLite or GUI."""
import argparse
import ctypes
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

from cs2pov.adapters.video import read_data_lease
from cs2pov.services.environment_receipt import EnvironmentSnapshot, observe_environment
from cs2pov.storage.settings import DataError, Settings, local_path
from cs2pov.storage.transaction import no_redirection

MAX_WORKER_JSON = 65_536
PATH_FIELDS = ("installation", "cfg_directory", "output_directory", "hud_resource_path",
               "nvidia_executable", "nvidia_app_executable")


def _object(raw, fields):
    if type(raw) is not dict or set(raw) != set(fields):
        raise DataError("环境观察任务结构不完整或包含未知字段。")
    return raw


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise DataError("环境观察任务标识无效。")
    return value


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise DataError("环境观察时间无效。")
    return float(value)


def _path(value):
    local_path(value)
    path = Path(value)
    if not value or ".." in path.parts or any(":" in part for part in path.parts[1:]):
        raise DataError("环境观察路径包含跳转或替代数据流。")
    return str(path)


def strict_json(data):
    if type(data) not in (bytes, str) or len(data if type(data) is bytes else data.encode("utf-8")) > MAX_WORKER_JSON:
        raise DataError("环境观察任务或结果超过 64KB 大小限制。")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DataError("环境观察 JSON 包含重复字段。")
            result[key] = value
        return result
    def constant(_value):
        raise DataError("环境观察 JSON 包含非有限数字。")
    try:
        raw = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        if type(raw) is not dict:
            raise DataError("环境观察 JSON 不是对象。")
        pending = [(raw, 0)]
        count = 0
        while pending:
            item, depth = pending.pop(); count += 1
            if depth > 16 or count > 1024:
                raise DataError("环境观察 JSON 结构超过限制。")
            if type(item) is float and not math.isfinite(item):
                raise DataError("环境观察 JSON 包含非有限数字。")
            values = item.values() if type(item) is dict else item if type(item) is list else ()
            pending.extend((value, depth + 1) for value in values)
        return raw
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise DataError("环境观察 JSON 无法安全读取。") from error


def _configuration_sha256(values):
    raw = {key: values[key] for key in PATH_FIELDS}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EnvironmentObservationRequest:
    schema: int
    request_id: str
    installation: str
    cfg_directory: str
    output_directory: str
    hud_resource_path: str
    nvidia_executable: str
    nvidia_app_executable: str

    def to_dict(self):
        return asdict(self)

    @property
    def configuration_sha256(self):
        return _configuration_sha256(self.to_dict())


def decode_observation_request(raw):
    raw = _object(raw, EnvironmentObservationRequest.__dataclass_fields__)
    if type(raw["schema"]) is not int or raw["schema"] != 1:
        raise DataError("环境观察任务版本不受支持。")
    return EnvironmentObservationRequest(1, _identifier(raw["request_id"]),
                                         **{key: _path(raw[key]) for key in PATH_FIELDS})


def make_observation_request(settings, hud_resource, nvidia_executable, *, request_id=None, nvidia_app_executable=None):
    if type(settings) is not Settings:
        raise DataError("环境观察需要当前有效设置。")
    Settings.decode(asdict(settings))
    main = Path(nvidia_executable).with_name("NVIDIA App.exe") if nvidia_app_executable is None else Path(nvidia_app_executable)
    return decode_observation_request(dict(schema=1, request_id=request_id or uuid.uuid4().hex,
        installation=settings.installation, cfg_directory=settings.cfg, output_directory=settings.video_directory,
        hud_resource_path=str(hud_resource), nvidia_executable=str(nvidia_executable), nvidia_app_executable=str(main)))


@dataclass(frozen=True)
class EnvironmentObservationResult:
    schema: int
    request_id: str
    configuration_sha256: str
    started_at: float
    observed_at: float
    snapshot: EnvironmentSnapshot | None
    reason: str

    def to_dict(self):
        return asdict(self)


def decode_observation_result(raw, *, request=None):
    raw = _object(raw, EnvironmentObservationResult.__dataclass_fields__)
    if type(raw["schema"]) is not int or raw["schema"] != 1:
        raise DataError("环境观察结果版本不受支持。")
    request_id = _identifier(raw["request_id"])
    digest = raw["configuration_sha256"]
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise DataError("环境观察配置标识无效。")
    start, observed = _number(raw["started_at"]), _number(raw["observed_at"])
    if observed < start:
        raise DataError("环境观察时钟倒退；结果不能使用。")
    snapshot = None if raw["snapshot"] is None else EnvironmentSnapshot.decode(raw["snapshot"])
    reason = raw["reason"]
    if type(reason) is not str or len(reason) > 1024 or (snapshot is None and not reason) or (snapshot is not None and reason):
        raise DataError("环境观察的已知/未知状态与原因不一致。")
    if snapshot is not None and _configuration_sha256(snapshot.to_dict()) != digest:
        raise DataError("环境快照路径不属于观察配置。")
    if request is not None:
        if type(request) is not EnvironmentObservationRequest:
            raise DataError("环境观察结果缺少有效原请求。")
        request = decode_observation_request(request.to_dict())
        if (request_id, digest) != (request.request_id, request.configuration_sha256):
            raise DataError("环境观察结果属于过期任务或其他配置。")
    return EnvironmentObservationResult(1, request_id, digest, start, observed, snapshot, reason)


def run_request(raw, *, observer=observe_environment, clock=time.time):
    request = decode_observation_request(raw)
    started_at = _number(clock())
    snapshot = observer(request.installation, request.cfg_directory, request.output_directory,
        request.hud_resource_path, request.nvidia_executable, nvidia_app_executable=request.nvidia_app_executable)
    if snapshot is not None:
        if type(snapshot) is not EnvironmentSnapshot:
            raise DataError("环境提供者没有返回有效当前快照。")
        snapshot = EnvironmentSnapshot.decode(snapshot.to_dict())
    result = EnvironmentObservationResult(1, request.request_id, request.configuration_sha256,
        started_at, _number(clock()), snapshot,
        "当前环境的路径或版本未能核实；试录状态保持未验证。" if snapshot is None else "")
    return decode_observation_result(result.to_dict(), request=request)


def _write_new_result(path, data):
    """Flush a new result and publish without replacing an existing task."""
    path = Path(_path(str(path)))
    no_redirection(path)
    if path.exists():
        raise DataError("环境观察结果已存在，禁止覆盖旧证据。")
    stage = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with stage.open("xb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        no_redirection(path)
        if os.name == "nt":
            from ctypes import wintypes
            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes, move.restype = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL
            if not move(str(stage), str(path), 8):  # WRITE_THROUGH, no REPLACE_EXISTING.
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.link(stage, path)  # Atomic create; fails when the destination exists.
    finally:
        if stage.exists():
            stage.unlink()


def emit_result(value, path=None):
    try:
        data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
        strict_json(data)
        if path is not None:
            _write_new_result(path, data)
        if sys.stdout is not None:
            print(data.decode("utf-8"), flush=True)
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise DataError("环境观察结果不能保存；已有证据保留。") from error


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("observe",), required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from cs2pov.adapters.worker_job import contain_current_worker
        contain_current_worker()
        no_redirection(args.result)
        if args.result.exists():
            raise DataError("环境观察结果已存在，未开始观察。")
        with read_data_lease(args.request) as signature:
            if signature.bytes > MAX_WORKER_JSON:
                raise DataError("环境观察任务超过 64KB 大小限制。")
            with args.request.open("rb") as stream:
                raw = strict_json(stream.read(MAX_WORKER_JSON + 1))
        result = run_request(raw)
        emit_result(result.to_dict(), args.result)
        return 0
    except Exception as error:
        value = {"error": (str(error) or type(error).__name__)[:1024]}
        try:
            emit_result(value, args.result)
        except DataError:
            if sys.stdout is not None:
                print(json.dumps(value, ensure_ascii=True), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
