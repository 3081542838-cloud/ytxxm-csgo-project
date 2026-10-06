"""Format the locally verified console form for Unicode Demo paths."""
from pathlib import Path


def playdemo_command(path: Path) -> str:
    value = path.as_posix()
    if not path.is_absolute() or path.suffix.casefold() != ".dem":
        raise ValueError("Demo 路径必须是完整 .dem 路径。")
    if any(ord(c) < 32 or c in ('"', ';') for c in value):
        raise ValueError("Demo 路径包含不支持的控制字符。")
    return f'playdemo "{value}"'
