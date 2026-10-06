"""Transactional references only; never copies or removes Demo/video files."""
import json
import math
from pathlib import Path
import sqlite3
import time
from cs2pov.storage.settings import DataError, local_path
from cs2pov.storage.transaction import no_redirection


MAX_RECORD_BYTES = 1_048_576


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("录制记录包含重复字段。")
        result[key] = value
    return result


def _reject_constant(_value):
    raise DataError("录制记录包含无效数字。")


def _decode_payload(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
        raise DataError("录制记录内容或大小无效。")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError) as error:
        raise DataError("录制记录不是有效 JSON 对象。") from error
    if not isinstance(payload, dict):
        raise DataError("录制记录必须是对象。")
    # Unknown future fields remain readable, but cannot hide non-finite numbers
    # or pathological nesting from the bounded decoder.
    pending = [(payload, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 20_000:
            raise DataError("录制记录结构超出限制。")
        if type(item) is float and not math.isfinite(item):
            raise DataError("录制记录包含无效数字。")
        values = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        pending.extend((value, depth + 1) for value in values)
    for field in ("player", "map", "video_result", "metadata_result", "content_result", "result", "recording_state"):
        if field in payload and (not isinstance(payload[field], str) or len(payload[field]) > 1024):
            raise DataError("录制记录摘要字段无效。")
    for field in ("video", "demo"):
        if field in payload:
            local_path(payload[field])
            if not payload[field]:
                raise DataError("录制记录文件路径为空。")
    if "duration" in payload and (type(payload["duration"]) not in (int, float)
            or not math.isfinite(payload["duration"]) or payload["duration"] <= 0):
        raise DataError("录制记录视频时长无效。")
    for field in ("width", "height", "video_streams", "audio_streams"):
        if field in payload and (type(payload[field]) is not int
                or payload[field] < (0 if field == "audio_streams" else 1)):
            raise DataError("录制记录视频元数据无效。")
    if "draft_snapshot" in payload and not isinstance(payload["draft_snapshot"], dict):
        raise DataError("录制记录草稿快照无效。")
    return payload


def decode_record_payload(row):
    """Validate one row without editing it or touching its referenced files."""
    try:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or len(row["id"]) > 128
                or row.get("restore_status") not in ("unknown", "complete", "blocked")
                or type(row.get("created")) not in (int, float)
                or not math.isfinite(row["created"]) or row["created"] < 0):
            raise DataError("录制记录索引无效。")
        return _decode_payload(row["payload"])
    except (KeyError, TypeError, UnicodeError, OverflowError) as error:
        raise DataError("录制记录内容无效。") from error


class Library:
    def __init__(self, path: Path):
        no_redirection(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version > 2:
                    raise DataError("数据库来自更新版本，不能降级或重置。")
                if version == 0:
                    if self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                        raise DataError("数据库缺少版本信息，保留文件并停止迁移。")
                    self.db.execute("CREATE TABLE drafts (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL)")
                    self.db.execute("CREATE TABLE records (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL)")
                    self.db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
                    self.db.execute("PRAGMA user_version=1")
                    version = 1
                if version == 1:
                    self.db.execute("ALTER TABLE records ADD COLUMN restore_status TEXT NOT NULL DEFAULT 'unknown'")
                    self.db.execute("PRAGMA user_version=2")
        except Exception:
            self.db.close()
            raise

    def save_draft(self, payload):
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.db:
            self.db.execute("INSERT INTO drafts VALUES ('current', ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated=excluded.updated", (raw, time.time()))

    def draft(self):
        row = self.db.execute("SELECT payload FROM drafts WHERE id='current'").fetchone()
        return json.loads(row[0]) if row else None

    def records(self, limit=100):
        return [dict(row) for row in self.db.execute("SELECT * FROM records ORDER BY created DESC LIMIT ?", (limit,))]

    def save_record(self, record_id, payload, restore_status="unknown"):
        if not isinstance(record_id, str) or not record_id or len(record_id) > 128:
            raise ValueError("record id is invalid")
        if restore_status not in ("unknown", "complete", "blocked"):
            raise ValueError("restore status is invalid")
        try:
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            _decode_payload(raw)
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as error:
            raise DataError("录制记录内容无效，原记录保留。") from error
        with self.db:
            self.db.execute("INSERT INTO records VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, restore_status=excluded.restore_status",
                            (record_id, raw, time.time(), restore_status))

    def remove_record(self, record_id):
        with self.db:
            self.db.execute("DELETE FROM records WHERE id=?", (record_id,))

    def record(self, record_id):
        row = self.db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row else None

    def unfinished(self):
        return [row[0] for row in self.db.execute("SELECT id FROM sessions WHERE state != 'complete'")]

    def close(self):
        self.db.close()
