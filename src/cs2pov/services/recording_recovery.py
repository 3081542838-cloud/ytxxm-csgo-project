"""Independent, bounded NVIDIA recovery evidence; never replays input."""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from cs2pov.services.recording import RecordingScope
from cs2pov.storage.settings import DataError, local_path
from cs2pov.storage.transaction import RecoveryError, no_redirection, read_small, safe_path

MAX_SESSIONS = 1024
MAX_RECORD_BYTES = 1_048_576
MAX_SCAN_BYTES = 24 * 1024 * 1024
RECORDS = ("recording-session.json", "recording-task.json", "recording-workflow.json")
TASK_ID = re.compile(r"[0-9a-f]{32}")
DIGEST = re.compile(r"[0-9a-f]{64}")
SESSION_STATES = {"idle", "awaiting_not_recording", "start_ready", "start_pending",
                  "awaiting_started", "recording", "stop_ready", "stop_pending",
                  "awaiting_stopped", "blocked", "unknown", "cancelled", "stopped"}


class RecordingRecoveryError(DataError):
    pass


@dataclass(frozen=True)
class RecordingRecoveryItem:
    session_id: str | None
    directory: Path | None
    task_id: str | None
    state: str
    reason: str
    digest: str | None


def _block(session=None, *, task_id=None, reason, checkpoint_digest=None):
    return RecordingRecoveryItem(session.name if session is not None else None,
        session, task_id, "unknown", "NVIDIA 录制恢复未确认：" + reason, checkpoint_digest)


def _json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("重复 JSON 字段")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("非有限 JSON 数值")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError as error:
        raise ValueError("JSON 嵌套超过可核验结构限制") from error
    if not isinstance(value, dict):
        raise ValueError("检查点不是对象")
    return value


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _id(value):
    return isinstance(value, str) and TASK_ID.fullmatch(value) is not None


def _read(session, name, budget):
    path = safe_path(session, session / name)
    if not path.exists():
        return None
    before = path.stat()
    if not path.is_file() or before.st_size > MAX_RECORD_BYTES:
        raise RecordingRecoveryError(f"{name} 不是有界普通文件。")
    if budget[0] + before.st_size > MAX_SCAN_BYTES:
        raise RecordingRecoveryError("录制检查点扫描超过总大小上限。")
    data = read_small(path)
    budget[0] += len(data)
    after = safe_path(session, path).stat()
    if (len(data) > MAX_RECORD_BYTES or budget[0] > MAX_SCAN_BYTES
            or before.st_size != len(data) or after.st_size != len(data)
            or before.st_mtime_ns != after.st_mtime_ns):
        raise RecordingRecoveryError(f"{name} 读取期间变化或超过上限。")
    return data


def _read_set(session, budget):
    def version(path):
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    paths = {name: safe_path(session, session / name) for name in RECORDS}
    versions = {name: version(path) for name, path in paths.items()}
    raw = {name: _read(session, name, budget) for name in RECORDS}
    if any(version(safe_path(session, path)) != versions[name] for name, path in paths.items()):
        raise RecordingRecoveryError("录制检查点在扫描其他上下文时变化。")
    if all(data is None for data in raw.values()):
        return raw, None
    hasher = hashlib.sha256()
    for name, data in raw.items():
        hasher.update(name.encode("ascii") + b"\0")
        hasher.update(b"missing\0" if data is None else str(len(data)).encode("ascii") + b"\0" + data)
    return raw, hasher.hexdigest()


def _validate_checkpoint(value):
    if type(value.get('start_not_sent', False)) is not bool:
        raise ValueError('启动零输入凭据无效')
    fields = {"state", "reason", "triggered", "started_at", "stopped_at", "task_id",
              "scope", "expected_identity", "confirmation_token", "deadline",
              "start_attempted_at", "stop_attempted_at"}
    if not fields <= value.keys() or not _id(value["task_id"]):
        raise ValueError("录制检查点结构不完整，不能核验任务身份")
    if (value["state"] not in SESSION_STATES or value["triggered"] not in {"none", "start", "stop"}
            or not isinstance(value["reason"], str)):
        raise ValueError("录制状态或已消费输入标记无效")
    scope = value["scope"]
    if not isinstance(scope, dict):
        raise ValueError("录制范围无效")
    RecordingScope.freeze({"demo": scope["demo"], "selection": scope,
                           "fingerprint": {"sha256": scope["content_sha256"]}})
    identity = value["expected_identity"]
    if (not isinstance(identity, dict) or type(identity.get("pid")) is not int or identity["pid"] <= 0
            or type(identity.get("created")) is not int or identity["created"] <= 0
            or not isinstance(identity.get("executable"), str) or not identity["executable"]):
        raise ValueError("录制游戏身份无效")
    for field in ("started_at", "stopped_at", "deadline", "start_attempted_at", "stop_attempted_at"):
        if value[field] is not None and not _number(value[field]):
            raise ValueError("录制检查点时间无效")
    if value["confirmation_token"] is not None and not _id(value["confirmation_token"]):
        raise ValueError("录制确认标记无效")
    if value.get('start_not_sent', False) and not (
            value['state'] == 'cancelled' and value['triggered'] == 'none'
            and _number(value['start_attempted_at'])
            and all(value[field] is None for field in
                    ('started_at', 'stop_attempted_at', 'stopped_at', 'confirmation_token', 'deadline'))):
        raise ValueError('启动零输入凭据与录制状态矛盾')


def _validate_context(session, checkpoint, task, workflow):
    required = {"schema", "task_id", "session_id", "state", "error", "cleanup_requested", "terminal", "durable"}
    mirrors = ("triggered", "started_at", "stopped_at", "start_attempted_at", "stop_attempted_at",
               "confirmation_token", "deadline")
    required.update(("recording_state", *mirrors))
    if (not required <= task.keys() or type(task["schema"]) is not int or task["schema"] != 1
            or task["task_id"] != checkpoint["task_id"] or task["session_id"] != session.name
            or not isinstance(task["state"], str) or not isinstance(task["error"], str)
            or any(type(task[field]) is not bool for field in ("cleanup_requested", "terminal", "durable"))
            or task["recording_state"] != checkpoint["state"]
            or type(task.get('start_not_sent', False)) is not bool
            or task.get('start_not_sent', False) != checkpoint.get('start_not_sent', False)
            or any(task[field] is not None and not _number(task[field]) for field in
                   ("started_at", "stopped_at", "start_attempted_at", "stop_attempted_at", "deadline"))
            or any(task[field] != checkpoint[field] for field in mirrors)):
        raise ValueError("录制任务上下文缺失、损坏或身份不一致")
    required = {"state", "output_directory", "started_wall", "baseline"}
    if (not required <= workflow.keys() or not isinstance(workflow["state"], str)
            or workflow["state"] not in SESSION_STATES | {"awaiting_video", "awaiting_video_selection",
                "awaiting_video_stable", "discovering_video", "validating_video", "awaiting_content",
                "output_cancelled", "video_error", "record_error", "verified"}
            or not isinstance(workflow["output_directory"], str) or not workflow["output_directory"]
            or workflow["started_wall"] is not None and not _number(workflow["started_wall"])
            or not isinstance(workflow["baseline"], list)):
        raise ValueError("录制目录上下文缺失或损坏")
    local_path(workflow["output_directory"])
    if 'stopped_wall' in workflow and workflow['stopped_wall'] is not None:
        if (not _number(workflow['stopped_wall']) or not _number(workflow['started_wall'])
                or workflow['stopped_wall'] < workflow['started_wall']):
            raise ValueError('录制停止的墙上时间无效')
    for row in workflow["baseline"]:
        if (not isinstance(row, list) or len(row) != 4 or not isinstance(row[0], str) or not row[0]
                or any(type(value) is not int or value < 0 for value in row[1:])):
            raise ValueError("录制目录基线结构无效")
        local_path(row[0])


def _safe_external_state(checkpoint, task, workflow):
    if (task["state"] not in {"complete", "recovery_blocked"}
            or task["terminal"] is not True or task["durable"] is not True):
        return False
    times = tuple(checkpoint[field] for field in
                  ("start_attempted_at", "started_at", "stop_attempted_at", "stopped_at"))
    if checkpoint["triggered"] == "none":
        if checkpoint.get('start_not_sent', False) is True:
            return (task.get('start_not_sent', False) is True
                    and checkpoint['state'] == 'cancelled'
                    and _number(checkpoint['start_attempted_at'])
                    and all(checkpoint[field] is None for field in
                            ('started_at', 'stop_attempted_at', 'stopped_at', 'confirmation_token', 'deadline')))
        return (checkpoint["state"] in {"idle", "blocked", "cancelled"}
                and all(value is None for value in times)
                and checkpoint["confirmation_token"] is None and checkpoint["deadline"] is None)
    return (checkpoint["state"] == "stopped" and checkpoint["triggered"] == "stop"
            and all(_number(value) for value in times) and tuple(sorted(times)) == times
            and checkpoint["confirmation_token"] is None and checkpoint["deadline"] is None
            and _number(workflow["started_wall"]))


def _resolution_path(session, checkpoint_digest):
    return safe_path(session, session / f"recording-resolution-{checkpoint_digest}.json")


def _scan_session(session, budget):
    raw, checkpoint_digest = _read_set(session, budget)

    def unchanged():
        _, current_digest = _read_set(session, budget)
        if current_digest != checkpoint_digest:
            raise RecordingRecoveryError("录制检查点在解除 NVIDIA 阻断前变化。")

    if checkpoint_digest is None:
        unchanged()
        return None
    values, errors = {}, []
    for name, data in raw.items():
        try:
            if data is None:
                raise ValueError("文件缺失")
            values[name] = _json(data)
        except (ValueError, TypeError, UnicodeError) as error:
            errors.append(f"{name}：{error}")
    checkpoint = values.get(RECORDS[0], {})
    task_id = checkpoint.get("task_id")
    task_id = task_id if _id(task_id) else None
    if not errors:
        try:
            _validate_checkpoint(checkpoint)
            _validate_context(session, checkpoint, values[RECORDS[1]], values[RECORDS[2]])
            if _safe_external_state(checkpoint, values[RECORDS[1]], values[RECORDS[2]]):
                unchanged()
                return None
            errors.append("上次录制状态未确认或任务上下文不同步；请人工检查并停止 NVIDIA")
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            errors.append(str(error))
    name = f"recording-resolution-{checkpoint_digest}.json"
    resolution = _read(session, name, budget)
    if resolution is not None:
        try:
            value = _json(resolution)
            if (value.get("schema") == 1 and type(value.get("schema")) is int
                    and value.get("session_id") == session.name and value.get("task_id") == task_id
                    and value.get("checkpoint_digest") == checkpoint_digest
                    and value.get("state") == "manually_confirmed_stopped"
                    and _number(value.get("confirmed_at"))):
                unchanged()
                return None
            errors.append("人工确认记录与当前检查点不一致")
        except (ValueError, TypeError, UnicodeError) as error:
            errors.append("人工确认记录损坏：" + str(error))
    return _block(session, task_id=task_id, reason="；".join(errors), checkpoint_digest=checkpoint_digest)


def scan_recording_recovery(root: Path) -> list[RecordingRecoveryItem]:
    """Scan every immediate trusted session, including completed file journals.

    ``root`` is the Workspace directory. Digests bind the presence and exact
    bytes of all three recording records. Unreadable or redirected evidence
    remains blocking and deliberately has no actionable digest.
    """
    root = Path(root).absolute()
    items, budget = [], [0]
    try:
        no_redirection(root)
        if not root.exists():
            return []
        sessions = safe_path(root, root / "sessions")
        if not sessions.exists():
            return []
        if not sessions.is_dir():
            raise RecordingRecoveryError("会话根不是目录。")
        with os.scandir(sessions) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SESSIONS:
                    items.append(_block(reason="会话扫描超过数量上限，仍有未核验目录。"))
                    break
                session = sessions / entry.name
                try:
                    safe_path(sessions, session)
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    item = _scan_session(session, budget)
                    if item is not None:
                        items.append(item)
                except (OSError, ValueError, RuntimeError) as error:
                    items.append(_block(session, reason=str(error)))
    except (OSError, ValueError, RuntimeError) as error:
        items.append(_block(reason=str(error)))
    return sorted(items, key=lambda item: item.session_id or "")


def resolve_recording_recovery(root: Path, item: RecordingRecoveryItem, *,
                               task_id: str | None, checkpoint_digest: str) -> Path:
    """Record an explicit user's stopped-state confirmation, without input.

    Never rewrites an old checkpoint, file journal, task index or resolution.
    The caller must obtain the human confirmation before invoking this API.
    """
    if (not isinstance(item, RecordingRecoveryItem) or not isinstance(item.directory, Path)
            or not isinstance(item.session_id, str) or not item.session_id
            or not isinstance(checkpoint_digest, str) or DIGEST.fullmatch(checkpoint_digest) is None
            or checkpoint_digest != item.digest or task_id != item.task_id
            or task_id is not None and not _id(task_id)
            or item.session_id in {".", ".."} or any(char in item.session_id for char in "/\\:\0")):
        raise RecordingRecoveryError("人工确认未绑定有效的当前会话、任务和检查点。")
    root = Path(root).absolute()
    try:
        sessions = safe_path(root, root / "sessions")
        session = safe_path(sessions, sessions / item.session_id)
        if session != item.directory or item not in scan_recording_recovery(root):
            raise RecordingRecoveryError("录制恢复条目已经变化或不属于当前可信目录。")
        path = _resolution_path(session, checkpoint_digest)
        payload = {"schema": 1, "session_id": item.session_id, "task_id": task_id,
                   "checkpoint_digest": checkpoint_digest, "state": "manually_confirmed_stopped",
                   "confirmed_at": time.time()}
        with path.open("xb") as stream:
            stream.write((json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(session, path)
        _, current_digest = _read_set(session, [0])
        if current_digest != checkpoint_digest:
            raise RecordingRecoveryError("确认写入期间检查点变化；原确认保留，当前录制仍被阻断。")
        return path
    except (OSError, RecoveryError) as error:
        raise RecordingRecoveryError("录制人工确认保存失败；原记录保留：" + str(error)) from error
