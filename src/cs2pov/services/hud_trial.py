"""Stage 3 trial preparation; no game launch or recording in this module."""
from dataclasses import dataclass
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from cs2pov.adapters.disk import check_directory
from cs2pov.adapters.gameinfo import add_pov_search_path
from cs2pov.adapters.installation import inspect_installation
from cs2pov.adapters.game_compatibility import supports_patch, unsupported_patch_message
from cs2pov.adapters.processes import cs2_closed
from cs2pov.adapters.vpk import validate_resource
from cs2pov.storage.transaction import FileTransaction, RecoveryError, read_small, safe_path, atomic_write
from cs2pov.services.telemetry_resource import TelemetryResource, verify_session_resource

TRIAL_SHA256 = "98cffb9688c227a5f8edba50508e25cef0568a9e541ba6024d9d0eac24828af9"
TRIAL_PATHS = frozenset({
    "panorama/styles/hud/hudteamcounter.vcss_c",
    "panorama/styles/hud/hudteamcounter-equipmentinfo.vcss_c",
    "panorama/scripts/hud/huddemocontroller.vts_c",
})
CONFIG_NAMES = (
    "cs2_machine_convars.vcfg", "cs2_user_convars_0_slot0.vcfg",
    "cs2_user_keys_0_slot0.vcfg", "cs2_user_keys_0_slot1.vcfg",
    "cs2_user_keys_0_slot2.vcfg", "cs2_user_keys_0_slot3.vcfg", "cs2_video.txt",
)


@dataclass(frozen=True)
class TrialCheck:
    installation: Path
    gameinfo_sha256: str
    demo: Path
    output: Path
    output_available_bytes: int
    protected_configs: tuple[Path, ...]
    patch_version: str | None = None
    telemetry: TelemetryResource | None = None


def check_trial(installation: Path, demo: Path, output: Path, resource: Path, cfg: Path,
                *, telemetry=None) -> TrialCheck:
    if not cs2_closed():
        raise RecoveryError("已有 CS2 或进程状态未知，不能准备实机试录。")
    snapshot = inspect_installation(installation)
    if snapshot.has_pov_search_path or snapshot.has_pov_file:
        raise RecoveryError("检测到已有 POV 文件或路径；先确认恢复状态。")
    if telemetry is None:
        validate_resource(resource, TRIAL_SHA256, set(TRIAL_PATHS))
    else:
        if not supports_patch(snapshot.patch_version):
            raise RecoveryError(unsupported_patch_message(snapshot.patch_version))
        verify_session_resource(telemetry, resource)
    add_pov_search_path(snapshot.gameinfo.read_bytes())
    demo = demo.resolve(strict=True)
    if demo.suffix.casefold() != ".dem" or not demo.is_file():
        raise RecoveryError("请选择有效 Demo 文件。")
    with demo.open("rb") as stream:
        if stream.read(8) != b"PBDEMS2\x00":
            raise RecoveryError("Demo 文件头不属于 Source 2。")
    if any(c in str(demo) for c in ('"', ';', '\r', '\n')):
        raise RecoveryError("Demo 路径含不支持的控制字符。")
    disk = check_directory(output)
    cfg = cfg.absolute()
    configs = tuple(safe_path(cfg, cfg / name) for name in CONFIG_NAMES if (cfg / name).exists())
    if not configs or not (cfg / "cs2_machine_convars.vcfg").is_file():
        raise RecoveryError("无法确认当前用户的 CS2 配置目录。")
    for config in configs:
        read_small(config)
    return TrialCheck(snapshot.root, snapshot.gameinfo_sha256, demo, disk.directory,
                      disk.available_bytes, configs, snapshot.patch_version, telemetry)


def trial_transaction(check: TrialCheck, session: Path, *, checkpoint=lambda _: None) -> FileTransaction:
    targets = {"gameinfo": check.installation / "game/csgo/gameinfo.gi",
               "pov": check.installation / "game/csgo/pov.vpk"}
    targets.update({f"config_{index}": config for index, config in enumerate(check.protected_configs)})
    root = Path(os.path.commonpath([str(p.absolute()) for p in targets.values()]))
    return FileTransaction(session, root, targets, can_modify=cs2_closed, checkpoint=checkpoint)


def prepare_trial(check: TrialCheck, session: Path, resource: Path) -> FileTransaction:
    # Repeat checks directly before creating any backup/deployment intent.
    check_directory(check.output)
    if check.telemetry is None:
        validate_resource(resource, TRIAL_SHA256, set(TRIAL_PATHS))
    else:
        if not supports_patch(check.patch_version):
            raise RecoveryError(unsupported_patch_message(check.patch_version))
        if inspect_installation(check.installation).patch_version != check.patch_version:
            raise RecoveryError("检查后游戏版本发生变化，重新核验兼容性。")
        verify_session_resource(check.telemetry, resource)
    def save_context(_point):
        # prepare creates the transaction directory. Its first durable intent
        # is still before any game mutation; persist trusted restore targets
        # here so a failed prepare can be recovered by the desktop.
        context = session / "trial-context.json"
        if _point.startswith('intent:') and not context.exists():
            atomic_write(context,json.dumps(asdict(check),default=str).encode("utf-8"))
    transaction = trial_transaction(check, session, checkpoint=save_context)
    original = read_small(transaction.targets["gameinfo"])
    if hashlib.sha256(original).hexdigest() != check.gameinfo_sha256:
        raise RecoveryError("实机检查后 gameinfo.gi 已变化，重新检查。")
    contents = {"gameinfo": add_pov_search_path(original), "pov": read_small(resource)}
    protected = frozenset(key for key in transaction.targets if key.startswith("config_"))
    contents.update({key: read_small(transaction.targets[key]) for key in protected})
    transaction.prepare(contents, protect_readonly=protected)
    return transaction  # Deployment is a separate explicit step.
