"""Derive a small session archive from the exact audited private HUD resource.

Approval is byte-for-byte reconstruction, not trusting a caller supplied hash.
This does not resolve the reference assets' distribution license.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import secrets
import json

from cs2pov.adapters.vpk import build_vpk, validate_resource
from cs2pov.storage.transaction import atomic_write, no_redirection, read_small
from cs2pov.storage.settings import DataError
from cs2pov.adapters.compiled_script import replace_script_slot
from cs2pov.services.radar_payload import decode_payload

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
    radar: str | None = None
    native_radar: bool = False


def resource_bytes(base, nonce, *, radar=None, native_radar=False):
    if type(native_radar) is not bool or native_radar and radar is not None:
        raise DataError('官方雷达选项无效或与实验轨迹冲突。')
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
    if native_radar:
        # Leave the entire official panel under engine control. No marker
        # filtering, map extraction, or coordinate data is used by the app.
        names = b'"HudRadar", "CSGOHudRadar", '
        if visibility.count(names) != 1:
            raise DataError('HUD 雷达隐藏列表结构改变。')
        replacement = visibility.replace(names, b'', 1)+b'\n'+template.replace(MARKER,nonce.encode('ascii'))
    if radar is not None:
        data = decode_payload(radar)
        # Authenticate the unmodified base slot first. The session visibility
        # loop must not race the radar renderer by rehiding its native parent.
        names = b'"HudRadar", "CSGOHudRadar", '
        if visibility.count(names) != 1:
            raise DataError('HUD 雷达隐藏列表结构改变。')
        replacement = visibility.replace(names, b'', 1) + b'\n' + template.replace(MARKER,nonce.encode('ascii'))
        for name in ('radar_settings.js', 'pov_radar.js'):
            replacement += b'\n' + read_small(scripts/name).replace(b'\r\n', b'\n')
        runtime = read_small(scripts/'radar_runtime.js').replace(b'\r\n', b'\n')
        if runtime.count(b'__CS2POV_RADAR_DATA__') != 1 or runtime.count(MARKER) != 1:
            raise DataError('HUD 雷达运行脚本标记结构改变。')
        canonical = json.dumps(data,ensure_ascii=True,separators=(',', ':'),allow_nan=False).encode('ascii')
        replacement += b'\n' + runtime.replace(b'__CS2POV_RADAR_DATA__', canonical).replace(MARKER,nonce.encode('ascii'))
    if len(visibility)>SLOT_LENGTH or radar is None and len(replacement)>SLOT_LENGTH:
        raise DataError("回读脚本超出已验证资源槽位。")
    original = visibility.ljust(SLOT_LENGTH,b' ')
    if files[SCRIPT_PATH].count(original)!=1:
        raise DataError("HUD 资源不匹配已验证脚本槽位。")
    files[SCRIPT_PATH] = replace_script_slot(files[SCRIPT_PATH],original,replacement)
    return build_vpk(files)


def build_session_resource(base, path, *, nonce=None, radar=None, native_radar=False):
    nonce = secrets.token_hex(16) if nonce is None else nonce
    no_redirection(path)
    if path.exists():
        raise DataError("会话资源已经存在，禁止覆盖。")
    raw = resource_bytes(base,nonce,radar=radar,native_radar=native_radar)
    atomic_write(path,raw)
    result = TelemetryResource(Path(base),Path(path),nonce,hashlib.sha256(raw).hexdigest(),radar,native_radar)
    verify_session_resource(result,path)
    return result


def verify_session_resource(approval, path):
    if not isinstance(approval,TelemetryResource) or Path(path)!=approval.path:
        raise DataError("回读资源没有匹配的会话来源。")
    raw = resource_bytes(approval.base,approval.nonce,radar=approval.radar,native_radar=approval.native_radar)
    expected = hashlib.sha256(raw).hexdigest()
    if expected != approval.sha256 or read_small(Path(path)) != raw:
        raise DataError("回读资源与已审查来源、会话标识或完整内容不一致。")
    validate_resource(Path(path),expected,set(PATHS))
    return expected
