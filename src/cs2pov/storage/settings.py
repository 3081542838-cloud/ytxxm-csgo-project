"""Versioned local settings, bounded JSON reads and verified fallback copies."""
from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
import re
import uuid
from cs2pov.storage.transaction import atomic_write, no_redirection


class DataError(ValueError):
    pass


class DataVersionError(DataError):
    pass


class JsonFile:
    def __init__(self, path: Path, decode):
        self.path, self.decode = path, decode
        self.backup = path.with_suffix(path.suffix + ".bak")
        self.warning = ""

    def _read(self, path):
        no_redirection(path)
        if path.stat().st_size > 1_048_576:
            raise DataError("数据文件超过大小限制。")
        return self.decode(json.loads(path.read_text(encoding="utf-8")))

    def load(self, default):
        self.warning = ""
        if not self.path.exists() and not self.backup.exists():
            return default
        try:
            return self._read(self.path)
        except DataVersionError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as error:
            try:
                value = self._read(self.backup)
            except (OSError, ValueError, TypeError, KeyError):
                raise DataError(f"{self.path.name} 和有效副本都无法读取；保留原文件，请检查数据。") from error
            self.warning = f"{self.path.name} 损坏或缺失，已读取最近有效副本；原文件保留。"
            return value

    def save(self, raw):
        self.decode(raw)  # Validate before any write.
        no_redirection(self.path)
        no_redirection(self.backup)
        content = (json.dumps(raw, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if self.path.exists():
            try:
                self._read(self.path)
            except DataVersionError:
                raise
            except (OSError, ValueError, TypeError, KeyError):
                # Explicit saving may replace corrupt settings, but keep evidence.
                atomic_write(self.path.with_name(self.path.name + ".corrupt-" + uuid.uuid4().hex),
                             self.path.read_bytes())
            else:
                atomic_write(self.backup, self.path.read_bytes())
        else:
            atomic_write(self.backup, content)
        atomic_write(self.path, content)


def local_path(value):
    if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise DataError("路径格式无效。")
    if value and (not Path(value).is_absolute() or value.startswith(("\\\\", "//"))):
        raise DataError("请选择本机磁盘的绝对路径。")
    return value


@dataclass(frozen=True)
class Settings:
    schema: int = 2
    installation: str = ""
    cfg: str = ""
    video_directory: str = ""
    hotkey: str = "Alt+F9"
    console_key: str = "Backtick"
    nvidia_path_confirmed: bool = False
    output_verified: bool = False

    @classmethod
    def decode(cls, raw):
        if not isinstance(raw, dict) or type(raw.get("schema")) is not int:
            raise DataError("设置版本或结构无效。")
        if raw["schema"] > 2:
            raise DataVersionError("设置版本不受支持，不能重置覆盖。")
        if raw["schema"] == 1:
            if set(raw) != set(cls.__dataclass_fields__) - {"console_key"}:
                raise DataError("旧设置结构不完整或包含未知字段。")
            raw = {**raw, "schema": 2, "console_key": "Backtick"}
        elif raw["schema"] != 2 or set(raw) != set(cls.__dataclass_fields__):
            raise DataError("设置结构不完整或包含未知字段。")
        for key in ("installation", "cfg", "video_directory"):
            local_path(raw[key])
        hotkey = raw["hotkey"]
        if not isinstance(hotkey, str) or not re.fullmatch(r"(?:(?:Ctrl|Alt|Shift)\+){1,3}F(?:[1-9]|1[0-2])", hotkey):
            raise DataError("热键请使用 Ctrl / Alt / Shift 加 F1–F12，例如 Alt+F9。")
        modifiers = hotkey.split("+")[:-1]
        if len(set(modifiers)) != len(modifiers):
            raise DataError("热键修饰键不能重复。")
        if raw["console_key"] not in ("Backtick", "Slash"):
            raise DataError("控制台按键只支持反引号或斜杠，请先在游戏中配置对应按键。")
        for key in ("nvidia_path_confirmed", "output_verified"):
            if type(raw[key]) is not bool:
                raise DataError("路径验证状态无效。")
        if raw["output_verified"] and (not raw["video_directory"] or not raw["nvidia_path_confirmed"]):
            raise DataError("未确认目录不能标为试录通过。")
        return cls(**raw)

    def updated(self, **changes):
        result = replace(self, **changes)
        same_path = (bool(self.video_directory) and bool(result.video_directory)
                     and Path(self.video_directory) == Path(result.video_directory))
        if not same_path or self.installation != result.installation:
            result = replace(result, nvidia_path_confirmed=False, output_verified=False)
        if not result.nvidia_path_confirmed:
            result = replace(result, output_verified=False)
        return self.decode(asdict(result))


@dataclass(frozen=True)
class HudPreset:
    id: str = "builtin"
    name: str = "默认仿实战"
    crosshair: str = ""
    hud_scale: float = 1.0
    viewmodel_fov: float = 68.0
    viewmodel_x: float = 2.5
    viewmodel_y: float = 0.0
    viewmodel_z: float = -1.5
    show_radar: bool = False

    @classmethod
    def decode(cls, raw):
        # Existing presets and frozen tasks predate the optional native radar.
        if isinstance(raw, dict) and set(raw) == set(cls.__dataclass_fields__) - {"show_radar"}:
            raw = {**raw, "show_radar": False}
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise DataError("HUD 预设结构无效。")
        if type(raw["show_radar"]) is not bool:
            raise DataError("雷达显示选项必须为布尔值。")
        if not isinstance(raw["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", raw["id"]):
            raise DataError("HUD 标识无效。")
        if not isinstance(raw["name"], str) or not raw["name"].strip() or len(raw["name"]) > 40:
            raise DataError("预设名称需要 1–40 个字符。")
        code = raw["crosshair"]
        if not isinstance(code, str) or (code and not re.fullmatch(r"CSGO(?:-[A-Za-z0-9]{5}){5}", code)):
            raise DataError("准星请输入完整 CSGO 分享码，留空使用游戏当前准星。")
        for key, low, high in (("hud_scale", .5, 1.0), ("viewmodel_fov", 54, 68),
                               ("viewmodel_x", -2, 2.5), ("viewmodel_y", -2, 2), ("viewmodel_z", -2, 2)):
            value = raw[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise DataError(f"{key} 不在允许范围内。")
        return cls(**raw)


class Presets:
    def __init__(self, directory):
        self.file = JsonFile(directory / "hud-presets.json", self.decode)
        self.items, self.default = self.file.load(([HudPreset()], "builtin"))

    @staticmethod
    def decode(raw):
        if isinstance(raw, dict) and type(raw.get("schema")) is int and raw["schema"] > 2:
            raise DataVersionError("HUD 文件来自更新版本，不能降级覆盖。")
        if not isinstance(raw, dict) or set(raw) != {"schema", "default", "items"} or type(raw["schema"]) is not int or raw["schema"] not in (1, 2):
            raise DataError("HUD 文件版本或结构无效。")
        if not isinstance(raw["items"], list) or not 1 <= len(raw["items"]) <= 30:
            raise DataError("预设数量无效。")
        if raw['schema'] == 2 and any(not isinstance(item, dict) or 'show_radar' not in item for item in raw['items']):
            raise DataError('新版 HUD 预设缺少雷达选项。')
        items = [HudPreset.decode(item) for item in raw["items"]]
        ids = [item.id for item in items]
        if len(set(ids)) != len(ids) or "builtin" not in ids or raw["default"] not in ids:
            raise DataError("缺少默认预设或标识重复。")
        return items, raw["default"]

    def commit(self, items, default):
        raw = {"schema": 2, "items": [asdict(item) for item in items], "default": default}
        self.file.save(raw)
        self.items, self.default = items, default

    def save(self, preset):
        HudPreset.decode(asdict(preset))
        self.commit([item for item in self.items if item.id != preset.id] + [preset], self.default)

    def copy(self, id):
        original = next(item for item in self.items if item.id == id)
        clone = replace(original, id=uuid.uuid4().hex, name=(original.name[:35] + " 副本"))
        self.save(clone)
        return clone

    def remove(self, id):
        if id == "builtin":
            raise DataError("内置默认预设不能删除。")
        self.commit([item for item in self.items if item.id != id], "builtin" if self.default == id else self.default)

    def set_default(self, id):
        self.commit(self.items, id)
