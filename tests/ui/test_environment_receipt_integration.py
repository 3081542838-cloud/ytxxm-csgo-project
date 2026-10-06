"""The real Workspace must revoke stale proof and bind asynchronous receipts.

Only the read-only environment observer and current-file-signature adapter are
injected. Settings, SQLite records, receipt validation and receipt persistence
are the actual product code. No game, NVIDIA, CIM or foreground UI is used.
"""
from dataclasses import asdict, replace
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from cs2pov.adapters.disk import DiskSnapshot, MIN_VIDEO_BYTES
from cs2pov.services.environment_receipt import EnvironmentSnapshot, ReceiptStore
from cs2pov.services.environment_worker import EnvironmentObservationResult, make_observation_request
from cs2pov.services.output_validation import candidate_id_for, decode_file_signature
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError, HudPreset, JsonFile, Settings


TASK = "a" * 32
TOKEN = "b" * 32


class DeferredEnvironmentCoordinator:
    """Retain old callbacks so integration guards can be tested deliberately."""

    def __init__(self):
        self.calls = []
        self.cancelled = 0
        self.busy = False
        self.current_result = None
        self.snapshot = None
        self.accept = True

    def start(self, settings, hud, overlay, main, on_result):
        if not self.accept or self.busy:
            return False
        request = make_observation_request(settings, hud, overlay,
            nvidia_app_executable=main, request_id=f"{len(self.calls) + 1:032x}")
        self.calls.append(SimpleNamespace(settings=settings, hud=hud, overlay=overlay,
            main=main, callback=on_result, request=request))
        self.busy = True
        return True

    def cancel(self):
        self.cancelled += 1
        self.busy = False
        self.current_result = self.snapshot = None

    def deliver(self, index, result, reason=""):
        self.busy = False
        if index == len(self.calls) - 1:
            self.current_result = result
            self.snapshot = None if result is None else result.snapshot
        self.calls[index].callback(result, reason)


@pytest.fixture
def workspace_environment(tmp_path, qapp, monkeypatch):
    model = Workspace(tmp_path / "应用数据", disk_check=lambda path:
        DiskSnapshot(path, MIN_VIDEO_BYTES + 100, MIN_VIDEO_BYTES))
    model.settings = Settings(installation=str(tmp_path / "CS2"), cfg=str(tmp_path / "cfg"),
        video_directory=str(tmp_path / "videos"), nvidia_path_confirmed=True)
    model.settings_file.save(asdict(model.settings))
    for key in ("installation", "cfg", "video_directory"):
        Path(getattr(model.settings, key)).mkdir()
    hud = [tmp_path / "pov.vpk"]
    hud[0].write_bytes(b"test observer substitutes audited HUD registration")
    overlay = tmp_path / "NVIDIA Overlay.exe"
    monkeypatch.setattr("cs2pov.services.recording_automation.overlay_executable", lambda: overlay)
    monkeypatch.setattr("cs2pov.services.workspace.inspect_installation", lambda path:
        SimpleNamespace(root=path, patch_version="1.41.8.8"))
    monkeypatch.setattr(model, "verified_local_resource", lambda role: hud[0])
    coordinator = DeferredEnvironmentCoordinator()
    model._environment_coordinator = coordinator

    class AuditedRegistry:
        def save(self, role, path):
            assert role == "hud"
            hud[0] = Path(path)
            return SimpleNamespace(path=str(path), sha256="c" * 64, bytes=100)

        def load(self):
            return {"hud": SimpleNamespace(path=str(hud[0]), sha256="c" * 64, bytes=100)}

    monkeypatch.setattr(model, "local_resources", AuditedRegistry())
    fixture = SimpleNamespace(model=model, coordinator=coordinator, hud=hud,
        overlay=overlay, tmp_path=tmp_path, current_signature=None, signature_reads=[])

    def current_signature(path, **_kwargs):
        fixture.signature_reads.append(Path(path))
        if fixture.current_signature is None:
            raise DataError("current file identity unavailable")
        assert Path(path) == fixture.current_signature.path
        return fixture.current_signature

    monkeypatch.setattr("cs2pov.adapters.video.current_signature", current_signature)
    yield fixture
    model._preview_timer.stop()
    model._recording_task = model._preview = None
    model.busy = False
    model.library.close()


def environment_for(fixture):
    # All 13 paths, hashes and versions are explicit; unknown never becomes pass.
    model = fixture.model
    return EnvironmentSnapshot.decode({"installation": model.settings.installation,
        "cfg_directory": model.settings.cfg, "output_directory": model.settings.video_directory,
        "hud_resource_path": str(fixture.hud[0]), "nvidia_executable": str(fixture.overlay),
        "nvidia_executable_sha256": "e" * 64,
        "nvidia_app_executable": str(fixture.overlay.with_name("NVIDIA App.exe")),
        "nvidia_app_sha256": "f" * 64, "nvidia_app_component_version": "128.4.13.34",
        "cs2_version": "1.41.8.8", "hud_sha256": "c" * 64,
        "nvidia_version": "128.4.13.34", "driver_version": "32.0.15.8157"})


def result_for(fixture, index=-1, *, snapshot=None, observed_at=None):
    request = fixture.coordinator.calls[index].request
    now = time.time() if observed_at is None else observed_at
    return EnvironmentObservationResult(1, request.request_id, request.configuration_sha256,
        now - 0.01, now, snapshot or environment_for(fixture), "")


def save_verified_record(fixture, *, record_id="record1", task_id=TASK, metadata_only=False):
    model = fixture.model
    raw_signature = {"path": str(Path(model.settings.video_directory) / "Counter-strike 2" / (record_id + ".mp4")),
        "bytes": 128, "mtime_ns": 100, "ctime_ns": 80, "identity": ["windows", 9, 88], "change_ns": 100}
    signature = decode_file_signature(raw_signature)
    candidate = candidate_id_for(task_id, signature)
    at = time.time()
    payload = {"task_id": task_id, "candidate_id": candidate, "video_signature": raw_signature,
        "draft_snapshot": {"demo": str(fixture.tmp_path / "original.dem"), "map": "de_mirage",
            "fingerprint": {"sha256": "d" * 64, "bytes": 100, "mtime_ns": 5},
            "selection": {"player_id": "76561199198478034", "start_tick": 10, "end_tick": 20,
                "server_start_tick": 110, "server_end_tick": 120, "content_sha256": "d" * 64,
                "hud": asdict(HudPreset())}},
        "video": raw_signature["path"], "recording_state": "stopped",
        "metadata_result": "verified", "content_result": "unreviewed" if metadata_only else "verified",
        "video_result": "metadata_verified" if metadata_only else "verified", "duration": 12.0,
        "width": 1920, "height": 1080, "video_streams": 1, "audio_streams": 1,
        "trial_environment": {"task_id": task_id, "snapshot": environment_for(fixture).to_dict(),
            "observed_at": at - 30}}
    if not metadata_only:
        payload["content_confirmation"] = {"task_id": task_id, "candidate_id": candidate,
            "step_token": TOKEN, "target_view_confirmed": True, "hud_confirmed": True,
            "audio_confirmed": True, "range_confirmed": True, "clean_picture_confirmed": True,
            "head_seconds": .2, "tail_seconds": 1., "confirmed_at": at - 5}
    model.library.save_record(record_id, payload, "complete")
    workflow = SimpleNamespace(task_id=task_id, record_id=record_id,
        state="awaiting_content" if metadata_only else "verified", metadata=SimpleNamespace(path=signature.path))
    model._output_workflow = workflow
    fixture.current_signature = signature
    return workflow, payload, signature


def issue_current_receipt(fixture):
    save_verified_record(fixture)
    fixture.model.check_environment(issue_receipt=True)
    fixture.coordinator.deliver(len(fixture.coordinator.calls) - 1, result_for(fixture))
    receipt = ReceiptStore(fixture.model.directory / "environment-receipt.json").load()
    assert receipt is not None
    assert fixture.model.settings.output_verified
    return receipt


def test_historical_verified_checkbox_is_not_a_current_receipt(tmp_path, qapp):
    directory = tmp_path / "data"
    directory.mkdir()
    previous = Settings(video_directory=str(tmp_path / "videos"), nvidia_path_confirmed=True,
        output_verified=True)
    JsonFile(directory / "settings.json", Settings.decode).save(asdict(previous))
    model = Workspace(directory)
    try:
        assert not model.settings.output_verified
        assert model._environment_observation is None
        assert not (directory / "environment-receipt.json").exists()
    finally:
        model.library.close()


def test_current_verified_trial_and_fresh_file_issue_receipt_then_re_read(workspace_environment):
    fixture = workspace_environment
    receipt = issue_current_receipt(fixture)
    assert receipt.task_id == TASK and receipt.record_id == "record1"
    assert fixture.signature_reads == [receipt.video_signature.path, receipt.video_signature.path]
    raw = json.loads(fixture.model.settings_file.path.read_text(encoding="utf-8"))
    assert raw["output_verified"] is True
    # A new Workspace never accepts the stored checkbox; the actual same receipt
    # and current file identity must be checked again.
    fresh = Workspace(fixture.model.directory, disk_check=fixture.model.disk_check)
    try:
        assert not fresh.settings.output_verified
        fresh._environment_coordinator = fixture.coordinator
        fresh.verified_local_resource = lambda role: fixture.hud[0]
        fresh.check_environment()
        fixture.coordinator.deliver(len(fixture.coordinator.calls) - 1, result_for(fixture))
        assert fresh.settings.output_verified
        assert ReceiptStore(fresh.directory / "environment-receipt.json").load() == receipt
    finally:
        fresh.library.close()


@pytest.mark.parametrize("cause", ["metadata_only", "content_not_verified", "environment_changed",
    "current_file_replaced", "current_file_unknown"])
def test_unverified_or_wrong_environment_file_cannot_issue_receipt(workspace_environment, cause):
    fixture = workspace_environment
    workflow, payload, signature = save_verified_record(fixture, metadata_only=cause == "metadata_only")
    if cause == "content_not_verified":
        payload["content_result"] = "unreviewed"
        fixture.model.library.save_record(workflow.record_id, payload, "complete")
    if cause == "current_file_replaced":
        fixture.current_signature = replace(signature, identity=("windows", 9, 89))
    if cause == "current_file_unknown":
        fixture.current_signature = None
    fixture.model.check_environment(issue_receipt=True)
    current = environment_for(fixture)
    if cause == "environment_changed":
        current = replace(current, driver_version="32.0.15.9000")
    fixture.coordinator.deliver(0, result_for(fixture, snapshot=current))
    assert not fixture.model.settings.output_verified
    assert not (fixture.model.directory / "environment-receipt.json").exists()
    assert fixture.model.library.record(workflow.record_id) is not None


@pytest.mark.parametrize("change", ["installation", "cfg", "video_directory", "hotkey", "console_key"])
def test_settings_change_revokes_cached_environment_and_receipt_permission(workspace_environment, change):
    fixture = workspace_environment
    receipt = issue_current_receipt(fixture)
    values = {"installation": str(fixture.tmp_path / "CS2-new"), "cfg": str(fixture.tmp_path / "cfg-new"),
        "video_directory": str(fixture.tmp_path / "videos-new"), "hotkey": "Alt+F8", "console_key": "Slash"}
    fixture.model.save_settings(replace(fixture.model.settings, **{change: values[change]}))
    assert not fixture.model.settings.output_verified
    assert fixture.model._environment_observation is None
    assert fixture.coordinator.cancelled >= 1
    assert ReceiptStore(fixture.model.directory / "environment-receipt.json").load() == receipt


def test_hud_registration_revokes_proof_and_cancels_pending_observation(workspace_environment):
    fixture = workspace_environment
    receipt = issue_current_receipt(fixture)
    fixture.model.check_environment()
    fixture.model.register_local_resource("hud", fixture.tmp_path / "new-pov.vpk")
    assert not fixture.model.settings.output_verified
    assert fixture.model._environment_observation is None
    assert fixture.coordinator.cancelled >= 1
    assert ReceiptStore(fixture.model.directory / "environment-receipt.json").load() == receipt


def test_late_callback_after_settings_change_cannot_issue_or_restore_cached_proof(workspace_environment):
    fixture = workspace_environment
    save_verified_record(fixture)
    fixture.model.check_environment(issue_receipt=True)
    old_result = result_for(fixture)
    fixture.model.save_settings(replace(fixture.model.settings, cfg=str(fixture.tmp_path / "new-cfg")))
    before = fixture.model.settings_file.path.read_bytes()
    fixture.coordinator.deliver(0, old_result)
    assert not fixture.model.settings.output_verified
    assert fixture.model._environment_observation is None
    assert fixture.model.settings_file.path.read_bytes() == before
    assert not (fixture.model.directory / "environment-receipt.json").exists()


def test_late_callback_after_hud_registration_cannot_sign_old_environment(workspace_environment):
    fixture = workspace_environment
    save_verified_record(fixture)
    fixture.model.check_environment(issue_receipt=True)
    old_result = result_for(fixture)
    fixture.model.register_local_resource("hud", fixture.tmp_path / "new-pov.vpk")
    before = fixture.model.settings_file.path.read_bytes()
    fixture.coordinator.deliver(0, old_result)
    assert not fixture.model.settings.output_verified
    assert fixture.model._environment_observation is None
    assert fixture.model.settings_file.path.read_bytes() == before
    assert not (fixture.model.directory / "environment-receipt.json").exists()


@pytest.mark.parametrize("failure", ["inspection", "hud_resource", "observer_rejected", "unknown_result"])
def test_recheck_revokes_previous_true_before_unknown_or_failed_check(workspace_environment, monkeypatch, failure):
    fixture = workspace_environment
    receipt = issue_current_receipt(fixture)
    if failure == "inspection":
        def unavailable(_path):
            raise DataError("installation unreadable")
        monkeypatch.setattr("cs2pov.services.workspace.inspect_installation", unavailable)
    elif failure == "hud_resource":
        def unavailable(_role):
            raise DataError("HUD resource changed")
        monkeypatch.setattr(fixture.model, "verified_local_resource", unavailable)
    elif failure == "observer_rejected":
        fixture.coordinator.accept = False
    fixture.model.check_environment()
    # This must hold while asynchronous work is pending as well as on failure.
    assert not fixture.model.settings.output_verified
    assert fixture.model._environment_observation is None
    if failure == "unknown_result":
        fixture.coordinator.deliver(len(fixture.coordinator.calls) - 1, None, "driver version unknown")
        assert not fixture.model.settings.output_verified
        assert fixture.model._environment_observation is None
    assert ReceiptStore(fixture.model.directory / "environment-receipt.json").load() == receipt


def test_late_receipt_callback_cannot_certify_a_different_current_output_task(workspace_environment):
    fixture = workspace_environment
    save_verified_record(fixture)
    fixture.model.check_environment(issue_receipt=True)
    old_result = result_for(fixture)
    second, _, _ = save_verified_record(fixture, record_id="record2", task_id="e" * 32)
    fixture.coordinator.deliver(0, old_result)
    assert not fixture.model.settings.output_verified
    assert fixture.model._output_workflow is second
    assert not (fixture.model.directory / "environment-receipt.json").exists()


def test_settings_persistence_failure_never_leaves_runtime_success(workspace_environment, monkeypatch):
    fixture = workspace_environment
    save_verified_record(fixture)
    fixture.model.check_environment(issue_receipt=True)
    def unavailable(_raw):
        raise OSError("disk full while saving verified settings")
    monkeypatch.setattr(fixture.model.settings_file, "save", unavailable)
    try:
        fixture.coordinator.deliver(0, result_for(fixture))
    except OSError:
        # The real coordinator also catches callback exceptions; Workspace must
        # revoke its state even when its outer event dispatcher catches one.
        pass
    assert not fixture.model.settings.output_verified
    assert fixture.model._environment_observation is None


def test_settings_change_then_return_does_not_reactivate_old_callback(workspace_environment):
    fixture = workspace_environment
    save_verified_record(fixture)
    original = fixture.model.settings
    fixture.model.check_environment(issue_receipt=True)
    old_result = result_for(fixture)
    fixture.model.save_settings(replace(original, console_key="Slash"))
    fixture.model.save_settings(original)
    assert fixture.model.settings == original
    fixture.coordinator.deliver(0, old_result)
    assert not fixture.model.settings.output_verified
    assert fixture.model._environment_observation is None
    assert not (fixture.model.directory / "environment-receipt.json").exists()


@pytest.mark.parametrize("observation", ["current", "none", "expired", "future",
    "wrong_output", "wrong_hud", "wrong_preview_hud"])
def test_recording_arm_only_freezes_fresh_environment_bound_to_current_settings_and_hud(
        workspace_environment, monkeypatch, observation):
    fixture = workspace_environment
    model = fixture.model
    # Use the real check entry to establish the configuration a coordinator was
    # asked to observe; arm and polling below are instrumented, never send input.
    model.check_environment()
    result = result_for(fixture)
    if observation == "expired":
        result = replace(result, observed_at=time.time() - 301)
    elif observation == "future":
        result = replace(result, observed_at=time.time() + 30)
    elif observation == "wrong_output":
        result = replace(result, snapshot=replace(result.snapshot,
            output_directory=str(fixture.tmp_path / "other-videos")))
    elif observation == "wrong_hud":
        result = replace(result, snapshot=replace(result.snapshot,
            hud_resource_path=str(fixture.tmp_path / "other-pov.vpk")))
    model._environment_observation = None if observation == "none" else result
    model._preview = SimpleNamespace(state="play_ready", settings=model.settings,
        base=fixture.tmp_path / "other-pov.vpk" if observation == "wrong_preview_hud" else fixture.hud[0])
    model.busy = True
    arm_proof = []

    def arm():
        from copy import deepcopy
        arm_proof.append(deepcopy(model._trial_environment))

    task = SimpleNamespace(task_id=TASK, arm=arm)
    monkeypatch.setattr(model, "_recording_task_factory", lambda preview: task)
    monkeypatch.setattr(model, "poll_recording", lambda: None)
    model.start_recording()
    assert model._recording_task is task
    assert len(arm_proof) == 1
    if observation == "current":
        assert arm_proof[0] == {"task_id": TASK, "snapshot": result.snapshot.to_dict(),
            "observed_at": result.observed_at}
        assert model._trial_environment == arm_proof[0]
    else:
        # Unknown compatibility prevents proof issuance, not the user's already
        # authorized local recording itself; no false trial is written.
        assert arm_proof == [None]
        assert model._trial_environment is None
    assert not model.settings.output_verified
