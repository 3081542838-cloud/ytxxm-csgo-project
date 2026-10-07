"""Derive a small session archive from the exact audited private HUD resource.

Approval is byte-for-byte reconstruction, not trusting a caller supplied hash.
This does not resolve the reference assets' distribution license.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import secrets

from cs2pov.adapters.vpk import build_vpk, validate_resource
from cs2pov.storage.transaction import atomic_write, no_redirection, read_small
from cs2pov.storage.settings import DataError

BASE_SHA256 = "98cffb9688c227a5f8edba50508e25cef0568a9e541ba6024d9d0eac24828af9"
PATHS = frozenset({"panorama/styles/hud/hudteamcounter.vcss_c",
                  "panorama/styles/hud/hudteamcounter-equipmentinfo.vcss_c",
                  "panorama/scripts/hud/huddemocontroller.vts_c"})
SCRIPT_PATH = "panorama/scripts/hud/huddemocontroller.vts_c"
SLOT_LENGTH = 339773
MARKER = b"__CS2POV_SESSION_NONCE__"


@dataclass(frozen=True)
class TelemetryResource:
    base: Path
    path: Path
    nonce: str
    sha256: str


def resource_bytes(base, nonce):
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_]{16,64}", nonce):
        raise DataError("回读会话标识无效。")
    archive = validate_resource(base, BASE_SHA256, set(PATHS))
    files = {e.path:e.data for e in archive.entries}
    scripts = Path(__file__).resolve().parents[1]/"resources"
    # The audited VPK slot uses LF. Windows Git checkouts can package CRLF
    # assets; canonicalize only newlines, retaining the exact slot comparison.
    visibility = read_small(scripts/"pov_visibility.js").replace(b'\r\n', b'\n')
    template = read_small(scripts/"replay_telemetry.js").replace(b'\r\n', b'\n')
    if template.count(MARKER)!=1:
        raise DataError("回读脚本会话标记结构无效。")
    replacement = visibility+b'\n'+template.replace(MARKER,nonce.encode('ascii'))
    if len(visibility)>SLOT_LENGTH or len(replacement)>SLOT_LENGTH:
        raise DataError("回读脚本超出已验证资源槽位。")
    original = visibility.ljust(SLOT_LENGTH,b' ')
    if files[SCRIPT_PATH].count(original)!=1:
        raise DataError("HUD 资源不匹配已验证脚本槽位。")
    files[SCRIPT_PATH] = files[SCRIPT_PATH].replace(original,replacement.ljust(SLOT_LENGTH,b' '),1)
    return build_vpk(files)


def build_session_resource(base, path, *, nonce=None):
    nonce = secrets.token_hex(16) if nonce is None else nonce
    no_redirection(path)
    if path.exists():
        raise DataError("会话资源已经存在，禁止覆盖。")
    raw = resource_bytes(base,nonce)
    atomic_write(path,raw)
    result = TelemetryResource(Path(base),Path(path),nonce,hashlib.sha256(raw).hexdigest())
    verify_session_resource(result,path)
    return result


def verify_session_resource(approval, path):
    if not isinstance(approval,TelemetryResource) or Path(path)!=approval.path:
        raise DataError("回读资源没有匹配的会话来源。")
    raw = resource_bytes(approval.base,approval.nonce)
    expected = hashlib.sha256(raw).hexdigest()
    if expected != approval.sha256 or read_small(Path(path)) != raw:
        raise DataError("回读资源与已审查来源、会话标识或完整内容不一致。")
    validate_resource(Path(path),expected,set(PATHS))
    return expected
