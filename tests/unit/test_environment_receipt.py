from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cs2pov.services import environment_receipt as service
from cs2pov.services.output_validation import candidate_id_for, decode_file_signature
from cs2pov.storage.settings import DataError, HudPreset


TASK = "a" * 32
TOKEN = "b" * 32


@pytest.fixture
def sample(tmp_path):
    environment = {"installation": str(tmp_path / "CS2"), "cfg_directory": str(tmp_path / "cfg"),
        "output_directory": str(tmp_path / "videos"), "hud_resource_path": str(tmp_path / "pov.vpk"),
        "nvidia_executable": str(tmp_path / "NVIDIA Overlay.exe"), "cs2_version": "1.41.8.8",
        "hud_sha256": "c" * 64, "nvidia_version": "128.4.13.34", "driver_version": "32.0.15.8157",
        "nvidia_executable_sha256": "e" * 64, "nvidia_app_executable": str(tmp_path / "NVIDIA App.exe"),
        "nvidia_app_sha256": "f" * 64, "nvidia_app_component_version": "128.4.13.34"}
    signature = {"path": str(tmp_path / "videos" / "Counter-strike 2" / "clip.mp4"),
        "bytes": 128, "mtime_ns": 100, "ctime_ns": 80, "identity": ["windows", 9, 88], "change_ns": 100}
    candidate_id = candidate_id_for(TASK, decode_file_signature(signature))
    draft = {"demo": str(tmp_path / "original.dem"), "map": "de_mirage",
        "fingerprint": {"sha256": "d" * 64, "bytes": 100, "mtime_ns": 5},
        "selection": {"player_id": "76561199198478034", "start_tick": 10, "end_tick": 20,
            "server_start_tick": 110, "server_end_tick": 120, "content_sha256": "d" * 64,
            "hud": asdict(HudPreset())}}
    confirmation = {"task_id": TASK, "candidate_id": candidate_id, "step_token": TOKEN,
        "target_view_confirmed": True, "hud_confirmed": True, "audio_confirmed": True,
        "range_confirmed": True, "clean_picture_confirmed": True, "head_seconds": 0.2,
        "tail_seconds": 1.0, "confirmed_at": 50.0}
    payload = {"task_id": TASK, "candidate_id": candidate_id, "video_signature": signature,
        "draft_snapshot": draft, "video": signature["path"], "recording_state": "stopped",
        "metadata_result": "verified", "content_result": "verified", "video_result": "verified",
        "duration": 12.0, "width": 1920, "height": 1080, "video_streams": 1, "audio_streams": 1,
        "content_confirmation": confirmation,
        "trial_environment": {"task_id": TASK, "snapshot": environment, "observed_at": 30.0}}
    row = {"id": "record1", "payload": json.dumps(payload), "created": 51.0, "restore_status": "complete"}
    return environment, payload, row, decode_file_signature(signature)


def row_for(row, payload):
    return {**row, "payload": json.dumps(payload)}


def issue(sample):
    environment, _, row, signature = sample
    return service.issue_environment_receipt(service.EnvironmentSnapshot.decode(environment), row, current_signature=signature, now=60.0)


def test_verified_receipt_roundtrips_and_is_valid_only_for_same_current_environment_and_file(sample, tmp_path):
    environment, _, row, signature = sample
    receipt = issue(sample)
    assert len(receipt.nonce) == 32 and receipt.task_id == TASK and receipt.record_id == "record1"
    store = service.ReceiptStore(tmp_path / "receipt.json")
    assert store.load() is None
    store.save(receipt)
    loaded = store.load()
    assert loaded == receipt
    assert loaded.valid_for(service.EnvironmentSnapshot.decode(environment), row, signature, now=61)


@pytest.mark.parametrize("field,value", [
    ("installation", "changed"), ("cfg_directory", "changed"), ("output_directory", "changed"),
    ("hud_resource_path", "changed"), ("nvidia_executable", "changed"),
    ("nvidia_app_executable", "changed"), ("nvidia_executable_sha256", "a" * 64),
    ("nvidia_app_sha256", "a" * 64), ("nvidia_app_component_version", "128.5.14.0"),
    ("cs2_version", "1.41.9.0"), ("hud_sha256", "e" * 64),
    ("nvidia_version", "11.0.6.100"), ("driver_version", "32.0.15.9000"),
])
def test_every_environment_path_version_or_hud_change_invalidates_existing_receipt(sample, tmp_path, field, value):
    environment, _, row, signature = sample
    receipt = issue(sample)
    changed = {**environment, field: str(tmp_path / field / value) if value == "changed" else value}
    assert not receipt.valid_for(service.EnvironmentSnapshot.decode(changed), row, signature, now=61)


@pytest.mark.parametrize("field,value", [
    ("cs2_version", None), ("cs2_version", "unknown"), ("nvidia_version", "未知"),
    ("driver_version", ""), ("driver_version", "0.0.0.0"), ("hud_sha256", None),
    ("installation", "relative"), ("output_directory", "\\\\server\\videos"),
])
def test_unknown_or_invalid_environment_is_not_a_passable_snapshot(sample, field, value):
    environment, _, _, _ = sample
    with pytest.raises(DataError):
        service.EnvironmentSnapshot.decode({**environment, field: value})


@pytest.mark.parametrize("field,value", [
    ("metadata_result", "unknown"), ("content_result", "unreviewed"),
    ("video_result", "metadata_verified"), ("recording_state", "unknown"),
    ("content_confirmation", None), ("trial_environment", None),
])
def test_metadata_alone_unknown_state_or_missing_actual_content_trial_proof_cannot_issue(sample, field, value):
    environment, payload, row, signature = sample
    payload = deepcopy(payload); payload[field] = value
    with pytest.raises(DataError):
        service.issue_environment_receipt(service.EnvironmentSnapshot.decode(environment), row_for(row, payload), current_signature=signature, now=60)


@pytest.mark.parametrize("field,value", [
    ("task_id", "e" * 32), ("candidate_id", "e" * 64), ("step_token", "invalid"),
    ("target_view_confirmed", 1), ("hud_confirmed", False), ("audio_confirmed", "yes"),
    ("range_confirmed", False), ("clean_picture_confirmed", False),
    ("head_seconds", 2.001), ("tail_seconds", -1), ("confirmed_at", 61),
])
def test_confirmation_is_bound_and_every_requirement_remains_strict(sample, field, value):
    environment, payload, row, signature = sample
    payload = deepcopy(payload); payload["content_confirmation"][field] = value
    with pytest.raises(DataError):
        service.issue_environment_receipt(service.EnvironmentSnapshot.decode(environment), row_for(row, payload), current_signature=signature, now=60)


def test_old_environment_trial_cannot_certify_a_new_version(sample):
    environment, _, row, signature = sample
    changed = {**environment, "cs2_version": "1.42.0.0"}
    with pytest.raises(DataError):
        service.issue_environment_receipt(service.EnvironmentSnapshot.decode(changed), row, current_signature=signature, now=60)


@pytest.mark.parametrize("mutation", ["wrong_task", "after_content", "bad_scope", "outside_directory",
    "path_mismatch", "forged_candidate", "missing_identity", "stat_identity"])
def test_trial_scope_and_output_file_binding_prevent_cross_task_or_foreign_file_receipt(sample, tmp_path, mutation):
    environment, payload, row, signature = sample
    payload = deepcopy(payload)
    if mutation == "wrong_task": payload["trial_environment"]["task_id"] = "e" * 32
    elif mutation == "after_content": payload["trial_environment"]["observed_at"] = 55
    elif mutation == "bad_scope": payload["draft_snapshot"]["selection"]["content_sha256"] = "e" * 64
    elif mutation == "outside_directory": payload["video_signature"]["path"] = payload["video"] = str(tmp_path / "outside.mp4")
    elif mutation == "path_mismatch": payload["video"] = str(tmp_path / "videos" / "other.mp4")
    elif mutation == "forged_candidate": payload["video_signature"]["bytes"] += 1
    elif mutation == "missing_identity": payload["video_signature"]["identity"] = None
    else: payload["video_signature"]["identity"][0] = "stat"
    with pytest.raises(DataError):
        service.issue_environment_receipt(service.EnvironmentSnapshot.decode(environment), row_for(row, payload), current_signature=signature, now=60)


def test_current_file_identity_version_and_current_record_cannot_change_after_restart(sample):
    environment, payload, row, signature = sample
    receipt = issue(sample)
    current = service.EnvironmentSnapshot.decode(environment)
    for changed in (replace(signature, identity=("windows", 9, 89)), replace(signature, bytes=129),
                    replace(signature, mtime_ns=101), replace(signature, change_ns=101)):
        assert not receipt.valid_for(current, row, changed, now=61)
    changed_payload = {**payload, "content_result": "unreviewed"}
    assert not receipt.valid_for(current, row_for(row, changed_payload), signature, now=61)
    assert not receipt.valid_for(current, {**row, "id": "other"}, signature, now=61)
    assert not receipt.valid_for(None, row, signature, now=61)
    assert not receipt.valid_for(current, row, signature, now=59)


@pytest.mark.parametrize("mutation", ["schema", "nonce", "task", "identity", "extra", "nonfinite"])
def test_corrupt_or_future_receipt_is_preserved_and_never_falls_back_to_success(sample, tmp_path, mutation):
    receipt = issue(sample)
    value = receipt.to_dict()
    if mutation == "schema": value["schema"] = 2
    elif mutation == "nonce": value["nonce"] = "bad"
    elif mutation == "task": value["task_id"] = "bad"
    elif mutation == "identity": value["video_signature"]["identity"] = None
    elif mutation == "extra": value["unexpected"] = True
    else: value["issued_at"] = float("nan")
    path = tmp_path / "receipt.json"; path.write_text(json.dumps(value), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(DataError): service.ReceiptStore(path).load()
    with pytest.raises(DataError): service.ReceiptStore(path).save(receipt)
    assert path.read_bytes() == before


def test_duplicate_fields_and_bad_json_do_not_create_verified_receipt(tmp_path):
    path = tmp_path / "receipt.json"
    for raw in ('{"schema":1,"schema":1}', "[]", "{", '{"issued_at":1e309}'):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(DataError): service.ReceiptStore(path).load()
        assert path.read_text(encoding="utf-8") == raw


def test_atomic_write_failure_preserves_previous_valid_receipt(sample, tmp_path, monkeypatch):
    receipt = issue(sample); path = tmp_path / "receipt.json"
    store = service.ReceiptStore(path); store.save(receipt); before = path.read_bytes()
    def fail(*_): raise OSError("disk write failed")
    monkeypatch.setattr(service, "atomic_write", fail)
    with pytest.raises(DataError): store.save(receipt)
    assert path.read_bytes() == before


def test_observer_only_uses_current_readonly_sources_and_unknown_version_stays_unknown(tmp_path):
    install = tmp_path / "CS2"; install.mkdir()
    cfg = tmp_path / "cfg"; cfg.mkdir()
    output = tmp_path / "videos"; output.mkdir()
    resource = tmp_path / "pov.vpk"; resource.write_bytes(b"reviewed local HUD")
    executable = tmp_path / "NVIDIA Overlay.exe"; executable.write_bytes(b"exe")
    (tmp_path / "NVIDIA App.exe").write_bytes(b"main app")
    inspection = SimpleNamespace(root=install, patch_version="1.41.8.8")
    kwargs = {"installation_inspector": lambda _: inspection,
        "file_version_reader": lambda _: "11.0.5.451", "driver_version_reader": lambda: "32.0.15.8157"}
    value = service.observe_environment(install, cfg, output, resource, executable, **kwargs)
    assert value.hud_sha256 == hashlib.sha256(resource.read_bytes()).hexdigest()
    assert value.nvidia_version == "11.0.5.451"
    kwargs["driver_version_reader"] = lambda: None
    assert service.observe_environment(install, cfg, output, resource, executable, **kwargs) is None


@pytest.mark.parametrize("controllers", [[], [{"PNPDeviceID": "PCI\\VEN_8086", "DriverVersion": "32.0.1.1"}],
    [{"PNPDeviceID": "PCI\\VEN_10DE&DEV_1234", "DriverVersion": "unknown"}],
    [{"PNPDeviceID": "PCI\\VEN_10DE&DEV_1", "DriverVersion": "32.0.15.8157"}] * 2])
def test_nvidia_driver_version_is_unknown_without_exact_single_current_controller(controllers):
    runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(controllers))
    assert service.read_nvidia_driver_version(runner=runner) is None


def test_driver_reader_is_bounded_and_never_uses_nvidia_history_or_a_foreign_binary():
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            {"PNPDeviceID": "PCI\\VEN_10DE&DEV_1234", "DriverVersion": "32.0.15.8157"}]))
    assert service.read_nvidia_driver_version(runner=runner) == "32.0.15.8157"
    assert len(calls) == 1
    argv, options = calls[0]
    assert Path(argv[0]).is_absolute() and Path(argv[0]).name.casefold() == "powershell.exe"
    assert "Get-CimInstance Win32_VideoController" in argv[-1]
    assert options["timeout"] == 3 and options["creationflags"] != 0


def test_issue_requires_fresh_matching_file_identity_after_content_confirmation(sample):
    environment, _, row, signature = sample
    for current in (None, replace(signature, identity=("windows", 9, 99)), replace(signature, change_ns=101)):
        with pytest.raises(DataError):
            service.issue_environment_receipt(service.EnvironmentSnapshot.decode(environment), row, current_signature=current, now=60)


def test_trial_freeze_validates_snapshot_and_cannot_accept_unknown(sample):
    environment, _, _, _ = sample
    snapshot = service.EnvironmentSnapshot.decode(environment)
    assert service.freeze_trial_environment(TASK, snapshot, observed_at=30) == {
        "task_id": TASK, "snapshot": snapshot.to_dict(), "observed_at": 30.0}
    with pytest.raises(DataError): service.freeze_trial_environment(TASK, None, observed_at=30)
    with pytest.raises(DataError): service.freeze_trial_environment("invalid", snapshot, observed_at=30)


def test_driver_query_failure_timeout_and_non_utf8_errors_are_unknown_not_thread_decode_failures():
    def failure(*args, **kwargs):
        assert kwargs["text"] is False
        return SimpleNamespace(returncode=1, stdout=b"", stderr="拒绝访问".encode("gbk"))
    assert service.read_nvidia_driver_version(runner=failure) is None
    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    assert service.read_nvidia_driver_version(runner=timeout) is None
    malformed = lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"\xff")
    assert service.read_nvidia_driver_version(runner=malformed) is None


def test_current_driver_response_bytes_decode_without_history_fallback():
    raw = json.dumps([{"PNPDeviceID": "PCI\\VEN_10DE&DEV_1234", "DriverVersion": "32.0.15.8157"}]).encode()
    assert service.read_nvidia_driver_version(runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw)) == "32.0.15.8157"


def test_win32_file_version_reads_actual_python_resource_and_non_binary_stays_unknown(tmp_path):
    version = service.read_file_version(Path(sys.executable))
    assert version is not None and all(part.isdigit() for part in version.split("."))
    text = tmp_path / "not-executable.exe"; text.write_bytes(b"no version resource")
    assert service.read_file_version(text) is None


@pytest.mark.parametrize("changed", ["version", "hud", "main_app"])
def test_observer_rejects_environment_changing_during_current_observation(tmp_path, changed):
    paths = [tmp_path / "CS2", tmp_path / "cfg", tmp_path / "videos"]
    for path in paths: path.mkdir()
    resource = tmp_path / "pov.vpk"; resource.write_bytes(b"hud")
    overlay = tmp_path / "NVIDIA Overlay.exe"; overlay.write_bytes(b"overlay")
    main = tmp_path / "NVIDIA App.exe"; main.write_bytes(b"main")
    calls = [0]
    def driver():
        calls[0] += 1
        if calls[0] == 1:
            if changed == "hud": resource.write_bytes(b"changed hud")
            if changed == "main_app": main.write_bytes(b"changed app")
        return "32.0.15.8157" if changed != "version" or calls[0] == 1 else "32.0.15.9000"
    value = service.observe_environment(*paths, resource, overlay,
        installation_inspector=lambda _: SimpleNamespace(root=paths[0], patch_version="1.41.8.8"),
        file_version_reader=lambda _: "128.4.13.34", driver_version_reader=driver)
    # The main app hash is read after the first driver callback; explicitly
    # mutate it during its version observation to exercise two complete reads.
    if changed == "main_app":
        def main_version(path):
            if Path(path) == main:
                main.write_bytes(main.read_bytes() + b"x")
            return "128.4.13.34"
        value = service.observe_environment(*paths, resource, overlay,
            installation_inspector=lambda _: SimpleNamespace(root=paths[0], patch_version="1.41.8.8"),
            file_version_reader=main_version, driver_version_reader=lambda: "32.0.15.8157")
    assert value is None
