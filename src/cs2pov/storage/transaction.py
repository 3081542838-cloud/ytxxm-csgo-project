"""Small, explicit file transactions. Recovery never trusts journal target paths.

The caller supplies the same trusted whitelist when loading a transaction. It
also supplies a live guard (all CS2 processes must be closed in production).
No target is changed until every original is durably backed up and verified.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Callable
from cs2pov.adapters.disk import check_directory

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024


class RecoveryError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def no_redirection(path: Path) -> None:
    for component in reversed((path, *path.parents)):
        if component.is_symlink() or component.is_junction():
            raise RecoveryError(f"拒绝重定向目录或文件：{component}")


def safe_path(root: Path, path: Path) -> Path:
    """Reject links/junctions instead of following a redirected whitelist."""
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RecoveryError("文件不在允许修改的目录内。") from error
    if ".." in relative.parts:
        raise RecoveryError("文件路径包含上级目录。")
    no_redirection(path)
    if not root.is_dir() or not path.parent.is_dir():
        raise RecoveryError("允许目录或目标父目录不存在。")
    return path


def read_small(path: Path) -> bytes:
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise RecoveryError(f"备份目标不是小型普通文件：{path}")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise RecoveryError("文件超过备份大小限制。")
    return data


def attributes(path: Path) -> int:
    return path.stat().st_file_attributes


def set_attributes(path: Path, value: int) -> None:
    api = ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW
    api.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    api.restype = wintypes.BOOL
    if not api(str(path), value):
        raise ctypes.WinError(ctypes.get_last_error())


def atomic_write(path: Path, data: bytes) -> None:
    """Same-directory flushed replacement, preserving existing Windows ACLs."""
    stage = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    old_attrs = attributes(path) if path.exists() else None
    try:
        with stage.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        if old_attrs is not None:
            # ReplaceFile preserves the replaced file's security descriptor.
            if old_attrs & stat.FILE_ATTRIBUTE_READONLY:
                set_attributes(path, old_attrs & ~stat.FILE_ATTRIBUTE_READONLY)
            api = kernel.ReplaceFileW
            api.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
            api.restype = wintypes.BOOL
            if not api(str(path), str(stage), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            set_attributes(path, old_attrs)
        else:
            api = kernel.MoveFileExW
            api.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            api.restype = wintypes.BOOL
            # No REPLACE_EXISTING: do not overwrite a file created meanwhile.
            if not api(str(stage), str(path), 8):  # MOVEFILE_WRITE_THROUGH
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if old_attrs is not None and path.exists():
            set_attributes(path, old_attrs)
        stage.unlink(missing_ok=True)


class FileTransaction:
    def __init__(self, session: Path, root: Path, targets: dict[str, Path], *,
                 can_modify: Callable[[], bool], checkpoint: Callable[[str], None] = lambda _: None):
        self.root = root.absolute()
        self.session = session.absolute()
        self.targets = dict(targets)
        self.can_modify = can_modify
        self.checkpoint = checkpoint
        if not targets or len(targets) > 16:
            raise RecoveryError("必须提供有限的修改白名单。")
        for key, path in targets.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", key):
                raise RecoveryError("无效的白名单标识。")
            safe_path(self.root, path)
        if len({str(p.absolute()).casefold() for p in targets.values()}) != len(targets):
            raise RecoveryError("白名单目标重复。")
        self.data: dict = {}

    def _session_path(self, name: str) -> Path:
        return safe_path(self.session, self.session / name)

    def _target(self, key: str) -> Path:
        return safe_path(self.root, self.targets[key])

    def _guard(self) -> None:
        if not self.can_modify():
            raise RecoveryError("CS2 未关闭或进程状态无法确认，禁止修改和恢复。")

    def _save(self, point: str) -> None:
        atomic_write(self._session_path("journal.json"),
                     json.dumps(self.data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        self.checkpoint(point)

    def prepare(self, replacements: dict[str, bytes], *, protect_readonly: frozenset[str] = frozenset()) -> None:
        no_redirection(self.session)
        if pending_sessions(self.session.parent):
            raise RecoveryError("存在未恢复或损坏事务，禁止新任务。")
        if self.session.exists():
            raise RecoveryError("事务目录已存在；先检查恢复状态。")
        if set(replacements) != set(self.targets) or any(type(v) is not bytes for v in replacements.values()):
            raise RecoveryError("替换内容必须与白名单完全一致。")
        if not protect_readonly <= set(self.targets):
            raise RecoveryError("只读保护必须属于白名单。")
        if any(len(v) > MAX_FILE_BYTES for v in replacements.values()) or sum(map(len, replacements.values())) > MAX_TOTAL_BYTES:
            raise RecoveryError("替换内容超过小文件预算。")
        self._guard()
        originals = {key: read_small(self._target(key)) if self._target(key).exists() else None
                     for key in self.targets}
        if any(originals[key] is None or replacements[key] != originals[key] for key in protect_readonly):
            raise RecoveryError("只读保护只允许现有文件保持原内容。")
        original_size = sum(len(v) for v in originals.values() if v is not None)
        if original_size > MAX_TOTAL_BYTES:
            raise RecoveryError("原文件备份超过小文件预算。")
        ancestor = self.session.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        # Journal margin plus originals/replacements; target staging is checked
        # separately on its actual volume, including when it differs from backup.
        check_directory(ancestor, original_size + sum(map(len, replacements.values())) + 1_048_576)
        for parent in {self._target(key).parent for key in self.targets}:
            required = sum(len(replacements[k]) for k in self.targets if self._target(k).parent == parent)
            check_directory(parent, required + 1_048_576)
        self.session.mkdir(parents=True)
        self.data = {"version": 1, "prepared": False, "complete": False, "entries": {}}
        # Capture intent before any game mutation; backups are immutable.
        for key, content in replacements.items():
            target = self._target(key)
            original = originals[key]
            if (digest(read_small(target)) if target.exists() else None) != (digest(original) if original is not None else None):
                raise RecoveryError("备份前原文件发生变化。")
            self.data["entries"][key] = {
                "original": digest(original) if original is not None else None,
                "deployed": digest(content),
                "attributes": attributes(target) if original is not None else None,
                "state": "planned",
                "write_intent": False,
                "protect_readonly": key in protect_readonly,
            }
            self._save(f"intent:{key}")
            if original is not None:
                atomic_write(self._session_path(f"{key}.backup"), original)
                if digest(read_small(self._session_path(f"{key}.backup"))) != digest(original):
                    raise RecoveryError("原文件备份校验失败。")
            atomic_write(self._session_path(f"{key}.replacement"), content)
            self.data["entries"][key]["state"] = "backed_up"
            self._save(f"backup:{key}")
        self.data["prepared"] = True
        self._save("prepared")

    def load(self) -> None:
        try:
            data = json.loads(read_small(self._session_path("journal.json")))
            if not isinstance(data, dict) or set(data) != {"version", "prepared", "complete", "entries"} or data["version"] != 1:
                raise ValueError("journal schema")
            if type(data["prepared"]) is not bool or type(data["complete"]) is not bool:
                raise ValueError("journal flags")
            if not isinstance(data["entries"], dict) or not set(data["entries"]) <= set(self.targets):
                raise ValueError("unknown target")
            if data["prepared"] and set(data["entries"]) != set(self.targets):
                raise ValueError("incomplete targets")
            for entry in data["entries"].values():
                if not isinstance(entry, dict) or set(entry) != {"original", "deployed", "attributes", "state", "write_intent", "protect_readonly"}:
                    raise ValueError("entry schema")
                if type(entry["write_intent"]) is not bool:
                    raise ValueError("invalid intent")
                if type(entry["protect_readonly"]) is not bool or (entry["protect_readonly"] and (entry["original"] is None or entry["deployed"] != entry["original"])):
                    raise ValueError("invalid protection")
                for field in ("original", "deployed"):
                    if field == "original" and entry[field] is None:
                        continue
                    if not isinstance(entry[field], str) or not re.fullmatch("[0-9a-f]{64}", entry[field]):
                        raise ValueError("invalid hash")
                if entry["state"] not in {"planned", "backed_up", "applying", "applied", "restoring", "restored", "conflict"}:
                    raise ValueError("invalid state")
                attr = entry["attributes"]
                if (entry["original"] is None and attr is not None) or (entry["original"] is not None and (type(attr) is not int or not 0 < attr < 2**32)):
                    raise ValueError("invalid attributes")
            self.data = data
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise RecoveryError(f"事务日志无法读取；禁止新任务：{error}") from error

    def _current(self, key: str) -> str | None:
        target = self._target(key)
        return digest(read_small(target)) if target.exists() else None

    def deploy(self) -> None:
        if not self.data.get("prepared") or self.data.get("complete"):
            raise RecoveryError("备份未完整完成或事务已结束。")
        for key, entry in self.data["entries"].items():
            self._guard()
            if self._current(key) != entry["original"]:
                raise RecoveryError(f"原文件已发生外部变化：{key}")
            content = read_small(self._session_path(f"{key}.replacement"))
            if digest(content) != entry["deployed"]:
                raise RecoveryError("部署暂存内容校验失败。")
            if entry["original"] is not None:
                self._backup(key, entry)
            entry["state"] = "applying"
            entry["write_intent"] = True
            self._save(f"before_deploy:{key}")
            self._guard()
            if self._current(key) != entry["original"]:
                raise RecoveryError("部署前文件变化，已阻断。")
            atomic_write(self._target(key), content)
            self.checkpoint(f"after_deploy:{key}")
            if entry["protect_readonly"]:
                self._guard()
                set_attributes(self._target(key), entry["attributes"] | stat.FILE_ATTRIBUTE_READONLY)
                self.checkpoint(f"after_protect:{key}")
            if self._current(key) != entry["deployed"]:
                raise RecoveryError("部署结果校验失败。")
            entry["state"] = "applied"
            self._save(f"applied:{key}")

    def _backup(self, key: str, entry: dict) -> bytes:
        original = read_small(self._session_path(f"{key}.backup"))
        if digest(original) != entry["original"]:
            raise RecoveryError(f"备份损坏：{key}")
        return original

    def restore(self) -> dict[str, str]:
        if not self.data:
            self.load()
        results = {}
        for key, entry in self.data["entries"].items():
            try:
                current = self._current(key)
                if current == entry["original"]:
                    # Already original, including crashes before backups completed.
                    self._guard()
                    if current is not None:
                        set_attributes(self._target(key), entry["attributes"])
                elif current == entry["deployed"] and entry["write_intent"] and entry["state"] != "restored":
                    original = self._backup(key, entry) if entry["original"] is not None else None
                    self._guard()
                    entry["state"] = "restoring"
                    self._save(f"before_restore:{key}")
                    self._guard()
                    if self._current(key) != current:
                        raise RecoveryError("恢复前检测到外部修改。")
                    target = self._target(key)
                    if original is None:
                        target.unlink()
                    else:
                        atomic_write(target, original)
                        set_attributes(target, entry["attributes"])
                    self.checkpoint(f"after_restore:{key}")
                else:
                    raise RecoveryError("当前内容既非原文件也非本会话文件，保留双方并阻断。")
                if self._current(key) != entry["original"]:
                    raise RecoveryError("恢复结果校验失败。")
                entry["state"] = "restored"
                results[key] = "restored"
                self._save(f"restored:{key}")
            except (OSError, RecoveryError) as error:
                entry["state"] = "conflict"
                results[key] = str(error)
                self._save(f"conflict:{key}")
        # A later file restore or an external writer can invalidate a target
        # already checked earlier in this loop. Verify the whole whitelist at
        # the completion boundary; preserve changed content and attributes.
        for key, entry in self.data["entries"].items():
            if entry["state"] != "restored":
                continue
            try:
                self._guard()
                if self._current(key) != entry["original"]:
                    raise RecoveryError("恢复收尾时文件再次变化，保留当前内容及备份并阻断。")
                if entry["original"] is not None and attributes(self._target(key)) != entry["attributes"]:
                    raise RecoveryError("恢复收尾时文件属性再次变化，保留当前属性并阻断。")
            except (OSError, RecoveryError) as error:
                entry["state"] = "conflict"
                results[key] = str(error)
        self.data["complete"] = all(e["state"] == "restored" for e in self.data["entries"].values())
        self._save("restore_finished")
        return results


def pending_sessions(directory: Path) -> list[Path]:
    """Malformed/orphaned directories also block: never mistake them for success."""
    no_redirection(directory)
    if not directory.exists():
        return []
    pending = []
    for session in directory.iterdir():
        if not session.is_dir() or session.is_symlink() or session.is_junction():
            pending.append(session)
            continue
        try:
            data = json.loads(read_small(session / "journal.json"))
            entries = data["entries"]
            if (data.get("version") != 1 or data.get("complete") is not True
                    or set(data) != {"version", "prepared", "complete", "entries"}
                    or type(data.get("prepared")) is not bool
                    or not isinstance(entries, dict) or not entries
                    or any(not isinstance(e, dict) or set(e) != {"original", "deployed", "attributes", "state", "write_intent", "protect_readonly"}
                           or e.get("state") != "restored" or type(e.get("write_intent")) is not bool
                           or type(e.get("protect_readonly")) is not bool
                           or not isinstance(e.get("deployed"), str) or not re.fullmatch("[0-9a-f]{64}", e["deployed"])
                           or (e.get("original") is not None and (not isinstance(e["original"], str) or not re.fullmatch("[0-9a-f]{64}", e["original"])))
                           for e in entries.values())):
                pending.append(session)
        except (OSError, RecoveryError, ValueError, KeyError, TypeError, AttributeError):
            pending.append(session)
    return pending
