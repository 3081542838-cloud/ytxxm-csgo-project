"""Environment-bound receipts for genuinely verified local trial recordings.

Receipts index proof produced by the recording workflow; they cannot make an
unknown version or metadata-only file into a successful trial. Windows version
resources and current Win32_VideoController data are read without launching a
game, opening an overlay, or consulting historical NVIDIA logs.

Public source interfaces:
https://learn.microsoft.com/en-us/windows/win32/api/winver/nf-winver-getfileversioninfow
https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-videocontroller
"""
from dataclasses import asdict, dataclass
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from cs2pov.adapters.installation import inspect_installation
from cs2pov.adapters.video import FileSignature, VIDEO_SUFFIXES
from cs2pov.services.output_validation import candidate_id_for, decode_file_signature, output_json_value
from cs2pov.services.recording import RecordingScope
from cs2pov.storage.library import decode_record_payload
from cs2pov.storage.settings import DataError, DataVersionError, HudPreset, local_path
from cs2pov.storage.transaction import atomic_write, no_redirection, read_small

MAX_RECEIPT_BYTES = 65_536
_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"\d+(?:\.\d+){1,7}", re.ASCII)


def _object(raw, fields):
    if type(raw) is not dict or set(raw) != set(fields):
        raise DataError("环境验证收据结构不完整或包含未知字段。")
    return raw


def _identifier(value):
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise DataError("环境验证的任务或随机标识无效。")
    return value


def _digest(value):
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise DataError("环境验证的内容标识无效。")
    return value


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise DataError("环境验证的实际时间无效。")
    return float(value)


def _version(value):
    if (type(value) is not str or len(value) > 64 or _VERSION.fullmatch(value) is None
            or not any(int(part) for part in value.split("."))):
        raise DataError("环境版本未知，不能标为试录通过。")
    return value


def _path(value):
    local_path(value)
    path = Path(value)
    if not value or ".." in path.parts or any(":" in part for part in path.parts[1:]):
        raise DataError("环境路径为空、包含上级跳转或替代数据流。")
    return str(path)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DataError("环境验证收据包含重复字段。")
            result[key] = value
        return result
    def constant(_value):
        raise DataError("环境验证收据包含非有限数字。")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise DataError("环境验证收据内容损坏；原文件保留。") from error


def _hash(raw):
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EnvironmentSnapshot:
    installation: str
    cfg_directory: str
    output_directory: str
    hud_resource_path: str
    nvidia_executable: str
    nvidia_executable_sha256: str
    nvidia_app_executable: str
    nvidia_app_sha256: str
    nvidia_app_component_version: str
    cs2_version: str
    hud_sha256: str
    nvidia_version: str
    driver_version: str

    @classmethod
    def decode(cls, raw):
        raw = _object(raw, cls.__dataclass_fields__)
        result = {key: _path(raw[key]) for key in ("installation", "cfg_directory",
                  "output_directory", "hud_resource_path", "nvidia_executable", "nvidia_app_executable")}
        result.update({key: _version(raw[key]) for key in ("cs2_version", "nvidia_version", "driver_version", "nvidia_app_component_version")})
        result.update({key: _digest(raw[key]) for key in ("hud_sha256", "nvidia_executable_sha256", "nvidia_app_sha256")})
        return cls(**result)

    def to_dict(self):
        return asdict(self)


def freeze_trial_environment(task_id, snapshot, *, observed_at=None):
    """Freeze current observed facts before arm; unknown facts stay absent."""
    if type(snapshot) is not EnvironmentSnapshot:
        raise DataError("尚无完整当前环境观察，不能冻结试录环境。")
    snapshot = EnvironmentSnapshot.decode(snapshot.to_dict())
    return dict(task_id=_identifier(task_id), snapshot=snapshot.to_dict(),
                observed_at=_number(time.time() if observed_at is None else observed_at))


def _trial(raw):
    raw = _object(raw, ("task_id", "snapshot", "observed_at"))
    return _identifier(raw["task_id"]), EnvironmentSnapshot.decode(raw["snapshot"]), _number(raw["observed_at"])


def _signature(raw, directory):
    signature = decode_file_signature(raw)
    if signature.identity[0] != "windows" or signature.bytes <= 0:
        raise DataError("试录视频缺少完整 Windows 文件身份或为空。")
    try:
        relative = signature.path.relative_to(Path(directory))
    except ValueError as error:
        raise DataError("试录视频不属于冻结的视频目录。") from error
    if len(relative.parts) not in (1, 2) or signature.path.suffix.casefold() not in VIDEO_SUFFIXES:
        raise DataError("试录视频路径层级或格式不受支持。")
    _path(str(signature.path))
    return signature


def _scope(raw):
    raw = _object(raw, RecordingScope.__dataclass_fields__)
    draft = {"demo": raw["demo"], "fingerprint": {"sha256": raw["content_sha256"]},
             "selection": {key: value for key, value in raw.items() if key != "demo"}}
    _path(raw["demo"])
    return RecordingScope.freeze(draft)


def _verified_record(current, row, *, now):
    payload = decode_record_payload(row)
    if any(payload.get(key) != "verified" for key in ("metadata_result", "content_result", "video_result")):
        raise DataError("试录必须同时完成实际文件检查和人工画面声音确认。")
    if payload.get("recording_state") != "stopped":
        raise DataError("NVIDIA 录制状态未知，不能签发试录收据。")
    if any(type(payload.get(key)) is not int or payload[key] <= 0
           for key in ("width", "height", "video_streams", "audio_streams")) or "duration" not in payload:
        raise DataError("试录缺少完整可解码画面与音轨元数据。")
    task_id = _identifier(payload.get("task_id"))
    trial_task, trial_snapshot, observed_at = _trial(payload.get("trial_environment"))
    if trial_task != task_id or trial_snapshot != current:
        raise DataError("试录冻结环境不属于当前任务或当前环境已变化。")
    draft = payload.get("draft_snapshot")
    if type(draft) is not dict:
        raise DataError("试录缺少实际冻结的 Demo 片段。")
    scope = RecordingScope.freeze(draft)
    _path(scope.demo)
    try:
        HudPreset.decode(draft["selection"]["hud"])
        fp = _object(draft["fingerprint"], ("sha256", "bytes", "mtime_ns"))
        if type(fp["bytes"]) is not int or fp["bytes"] <= 0 or type(fp["mtime_ns"]) is not int or fp["mtime_ns"] < 0:
            raise DataError("试录原 Demo 身份无效。")
    except (KeyError, TypeError) as error:
        raise DataError("试录原 Demo 或 HUD 快照不完整。") from error
    signature = _signature(payload.get("video_signature"), current.output_directory)
    if payload.get("video") != str(signature.path):
        raise DataError("试录视频路径与文件身份不一致。")
    candidate_id = candidate_id_for(task_id, signature)
    if payload.get("candidate_id") != candidate_id:
        raise DataError("试录候选身份不属于本任务文件版本。")
    proof = _object(payload.get("content_confirmation"), ("task_id", "candidate_id", "step_token",
        "target_view_confirmed", "hud_confirmed", "audio_confirmed", "range_confirmed",
        "clean_picture_confirmed", "head_seconds", "tail_seconds", "confirmed_at"))
    _identifier(proof["step_token"])
    if (proof["task_id"], proof["candidate_id"]) != (task_id, candidate_id):
        raise DataError("试录内容确认属于其他任务或视频。")
    if any(proof[key] is not True for key in ("target_view_confirmed", "hud_confirmed", "audio_confirmed",
                                              "range_confirmed", "clean_picture_confirmed")):
        raise DataError("试录仍有画面、声音或区间确认未通过。")
    if any(_number(proof[key]) > 2 for key in ("head_seconds", "tail_seconds")):
        raise DataError("试录片头片尾不得超过各 2 秒。")
    confirmed_at = _number(proof["confirmed_at"])
    if not observed_at <= confirmed_at <= now:
        raise DataError("试录观察、内容确认和收据时间顺序无效。")
    return payload, task_id, scope, signature, candidate_id, observed_at, confirmed_at


@dataclass(frozen=True)
class EnvironmentReceipt:
    schema: int
    nonce: str
    task_id: str
    record_id: str
    candidate_id: str
    environment: EnvironmentSnapshot
    scope: RecordingScope
    video_signature: FileSignature
    record_sha256: str
    content_confirmation_sha256: str
    trial_observed_at: float
    confirmed_at: float
    issued_at: float

    def to_dict(self):
        return output_json_value(self)

    @classmethod
    def decode(cls, raw):
        raw = _object(raw, cls.__dataclass_fields__)
        if type(raw["schema"]) is not int or raw["schema"] != 1:
            raise DataVersionError("环境验证收据版本不受支持；原文件保留。")
        environment = EnvironmentSnapshot.decode(raw["environment"])
        signature = _signature(raw["video_signature"], environment.output_directory)
        task_id = _identifier(raw["task_id"])
        candidate = _digest(raw["candidate_id"])
        if candidate_id_for(task_id, signature) != candidate:
            raise DataError("环境收据的任务与视频身份不一致。")
        record_id = raw["record_id"]
        if type(record_id) is not str or not record_id or len(record_id) > 128:
            raise DataError("环境收据缺少有效的录制记录。")
        observed, confirmed, issued = (_number(raw[key]) for key in ("trial_observed_at", "confirmed_at", "issued_at"))
        if not observed <= confirmed <= issued:
            raise DataError("环境收据的时间顺序无效。")
        return cls(1, _identifier(raw["nonce"]), task_id, record_id, candidate, environment,
            _scope(raw["scope"]), signature, _digest(raw["record_sha256"]),
            _digest(raw["content_confirmation_sha256"]), observed, confirmed, issued)

    def valid_for(self, current_environment, record_row, current_signature, *, now=None):
        try:
            receipt = self.decode(self.to_dict())
            if type(current_environment) is not EnvironmentSnapshot or type(current_signature) is not FileSignature:
                return False
            current = EnvironmentSnapshot.decode(current_environment.to_dict())
            at = _number(time.time() if now is None else now)
            if current != receipt.environment or at < receipt.issued_at or current_signature != receipt.video_signature:
                return False
            payload, task, scope, signature, candidate, observed, confirmed = _verified_record(current, record_row, now=at)
            return (record_row["id"] == receipt.record_id and task == receipt.task_id and scope == receipt.scope
                    and signature == receipt.video_signature and candidate == receipt.candidate_id
                    and observed == receipt.trial_observed_at and confirmed == receipt.confirmed_at
                    and _hash(payload) == receipt.record_sha256
                    and _hash(payload["content_confirmation"]) == receipt.content_confirmation_sha256)
        except (DataError, KeyError, TypeError, ValueError, OverflowError):
            return False


def issue_environment_receipt(current_environment, record_row, *, current_signature, now=None):
    if type(current_environment) is not EnvironmentSnapshot:
        raise DataError("当前环境未知，不能签发试录收据。")
    current = EnvironmentSnapshot.decode(current_environment.to_dict())
    at = _number(time.time() if now is None else now)
    payload, task, scope, signature, candidate, observed, confirmed = _verified_record(current, record_row, now=at)
    if type(current_signature) is not FileSignature or current_signature != signature:
        raise DataError("试录视频在内容确认后变化或当前文件身份未知，不能签发收据。")
    receipt = EnvironmentReceipt(1, uuid.uuid4().hex, task, record_row["id"], candidate, current,
        scope, signature, _hash(payload), _hash(payload["content_confirmation"]), observed, confirmed, at)
    return EnvironmentReceipt.decode(receipt.to_dict())


class ReceiptStore:
    def __init__(self, path):
        self.path = Path(path)
        local_path(str(self.path))
        no_redirection(self.path)

    def load(self):
        try:
            no_redirection(self.path)
            if not self.path.exists():
                return None
            if not self.path.is_file() or self.path.stat().st_size > MAX_RECEIPT_BYTES:
                raise DataError("环境验证收据文件或大小无效。")
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_RECEIPT_BYTES + 1)
            no_redirection(self.path)
            if len(raw) > MAX_RECEIPT_BYTES:
                raise DataError("环境验证收据超出大小限制。")
            return EnvironmentReceipt.decode(_json(raw))
        except (OSError, RuntimeError, UnicodeError) as error:
            raise DataError("无法读取环境验证收据；原文件保留。") from error

    def save(self, receipt):
        try:
            if type(receipt) is not EnvironmentReceipt:
                raise DataError("不能保存未经验证的环境收据。")
            receipt = EnvironmentReceipt.decode(receipt.to_dict())
            self.load()  # Never overwrite corrupt or newer evidence implicitly.
            raw = (json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
            if len(raw) > MAX_RECEIPT_BYTES:
                raise DataError("环境验证收据超出大小限制。")
            no_redirection(self.path)
            atomic_write(self.path, raw)
        except (OSError, RuntimeError, UnicodeError, TypeError, ValueError) as error:
            raise DataError("环境验证收据保存失败；原文件保留。") from error


def read_file_version(path):
    """Read the current executable's fixed Win32 file version, or unknown."""
    if os.name != "nt":
        return None
    from ctypes import wintypes
    class Fixed(ctypes.Structure):
        _fields_ = [(key, wintypes.DWORD) for key in ("signature", "structure_version", "file_ms", "file_ls",
            "product_ms", "product_ls", "flags_mask", "flags", "os", "file_type", "subtype", "date_ms", "date_ls")]
    try:
        path = Path(_path(str(path)))
        no_redirection(path)
        before = path.stat()
        if not path.is_file():
            return None
        api = ctypes.WinDLL("version", use_last_error=True)
        api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        api.GetFileVersionInfoW.restype = wintypes.BOOL
        api.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]
        api.VerQueryValueW.restype = wintypes.BOOL
        unused = wintypes.DWORD()
        length = api.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
        if not 0 < length <= 1_048_576:
            return None
        data = ctypes.create_string_buffer(length)
        if not api.GetFileVersionInfoW(str(path), 0, length, data):
            return None
        pointer, size = wintypes.LPVOID(), wintypes.UINT()
        if not api.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(size)) or size.value < ctypes.sizeof(Fixed) or not pointer.value:
            return None
        fixed = ctypes.cast(pointer, ctypes.POINTER(Fixed)).contents
        no_redirection(path)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return None
        if fixed.signature != 0xFEEF04BD:
            return None
        return _version(".".join(str(value) for value in (fixed.file_ms >> 16, fixed.file_ms & 65535,
                                                         fixed.file_ls >> 16, fixed.file_ls & 65535)))
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        return None


def _powershell_path():
    if os.name != "nt":
        return None
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True).GetWindowsDirectoryW
    api.argtypes, api.restype = [wintypes.LPWSTR, wintypes.UINT], wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = api(buffer, len(buffer))
    if not 0 < length < len(buffer):
        return None
    return Path(buffer.value) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def read_nvidia_driver_version(*, runner=subprocess.run):
    """Current single NVIDIA PCI controller only; ambiguous data is unknown."""
    try:
        executable = _powershell_path()
        if executable is None:
            return None
        no_redirection(executable)
        if not executable.is_file():
            return None
        command = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
                   "$ErrorActionPreference='Stop'; $taskControllers = @(Get-CimInstance Win32_VideoController | "
                   "Select-Object PNPDeviceID,DriverVersion); ConvertTo-Json -InputObject $taskControllers -Compress")
        result = runner([str(executable), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=False, timeout=3,
            check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        no_redirection(executable)
        if result.returncode != 0:
            return None
        raw = result.stdout
        if isinstance(raw, bytes):
            if len(raw) > 16_384:
                return None
            raw = raw.decode("utf-8-sig", errors="strict")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16_384:
            return None
        values = _json(raw)
        if type(values) is not list or len(values) > 16:
            return None
        candidates = []
        for item in values:
            item = _object(item, ("PNPDeviceID", "DriverVersion"))
            identity = item["PNPDeviceID"]
            if type(identity) is str and re.match(r"^PCI\\VEN_10DE(?:&|$)", identity, re.IGNORECASE):
                candidates.append(item)
        return _version(candidates[0]["DriverVersion"]) if len(candidates) == 1 else None
    except (OSError, ValueError, RuntimeError, TypeError, subprocess.TimeoutExpired, UnicodeError):
        return None


def observe_environment(installation, cfg_directory, output_directory, hud_resource, nvidia_executable,
                        *, installation_inspector=inspect_installation, file_version_reader=read_file_version,
                        driver_version_reader=read_nvidia_driver_version, nvidia_app_executable=None):
    """Read twice without input; changing/unknown facts cannot pass a trial."""
    try:
        main_executable = (Path(nvidia_executable).with_name("NVIDIA App.exe")
                           if nvidia_app_executable is None else Path(nvidia_app_executable))
        paths = [Path(_path(str(value))) for value in (installation, cfg_directory, output_directory,
                                                     hud_resource, nvidia_executable, main_executable)]
        for path in paths:
            no_redirection(path)
        if any(not path.is_dir() for path in paths[:3]) or any(not path.is_file() for path in paths[3:]):
            return None
        def read():
            inspected = installation_inspector(paths[0])
            if Path(inspected.root) != paths[0]:
                raise DataError("CS2 观察不属于指定安装路径。")
            return EnvironmentSnapshot.decode(dict(installation=str(paths[0]), cfg_directory=str(paths[1]),
                output_directory=str(paths[2]), hud_resource_path=str(paths[3]), nvidia_executable=str(paths[4]),
                cs2_version=inspected.patch_version, hud_sha256=hashlib.sha256(read_small(paths[3])).hexdigest(),
                nvidia_version=file_version_reader(paths[4]), driver_version=driver_version_reader(),
                nvidia_executable_sha256=hashlib.sha256(read_small(paths[4])).hexdigest(),
                nvidia_app_executable=str(paths[5]), nvidia_app_sha256=hashlib.sha256(read_small(paths[5])).hexdigest(),
                nvidia_app_component_version=file_version_reader(paths[5])))
        first, second = read(), read()
        for path in paths:
            no_redirection(path)
        return first if first == second else None
    except (OSError, DataError, ValueError, RuntimeError, TypeError, AttributeError, UnicodeError):
        return None
