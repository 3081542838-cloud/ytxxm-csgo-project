"""Read-only installation inspection. Never deploys or repairs files."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re


class InstallationError(ValueError):
    """The selected location cannot be confirmed as a CS2 installation."""


@dataclass(frozen=True)
class InstallationSnapshot:
    root: Path
    executable: Path
    gameinfo: Path
    gameinfo_sha256: str
    patch_version: str | None
    has_pov_search_path: bool
    has_pov_file: bool


def inspect_installation(root: Path) -> InstallationSnapshot:
    """Inspect an explicit user-selected installation without launching CS2."""
    try:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise InstallationError("请选择 CS2 安装文件夹。")
        executable = root / "game/bin/win64/cs2.exe"
        gameinfo = root / "game/csgo/gameinfo.gi"
        steam_info = root / "game/csgo/steam.inf"
        for target in (executable, gameinfo, steam_info):
            if not target.is_file():
                raise InstallationError(f"CS2 必要文件不存在：{target}")
        metadata = {}
        for line in steam_info.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                metadata[key.strip()] = value.strip()
        if metadata.get("appID") != "730":
            raise InstallationError("steam.inf 的 appID 不属于 CS2。")
        raw = gameinfo.read_bytes()
        text = raw.decode("utf-8-sig")
        if not re.search(r"\bSearchPaths\b", text):
            raise InstallationError("gameinfo.gi 缺少 SearchPaths，无法确认配置。")
        pointer = re.compile(
            r'^\s*"?Game"?\s+"?csgo[\\/]pov\.vpk"?\s*(?://.*)?$',
            re.IGNORECASE | re.MULTILINE,
        )
        return InstallationSnapshot(
            root=root,
            executable=executable,
            gameinfo=gameinfo,
            gameinfo_sha256=hashlib.sha256(raw).hexdigest(),
            patch_version=metadata.get("PatchVersion") or None,
            has_pov_search_path=bool(pointer.search(text)),
            has_pov_file=(root / "game/csgo/pov.vpk").exists(),
        )
    except InstallationError:
        raise
    except (OSError, UnicodeError) as error:
        raise InstallationError(f"无法读取 CS2 安装文件：{error}") from error
