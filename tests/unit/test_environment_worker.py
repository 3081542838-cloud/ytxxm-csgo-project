from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cs2pov.services import environment_worker as worker
from cs2pov.services.environment_receipt import EnvironmentSnapshot
from cs2pov.storage.settings import DataError, Settings


@pytest.fixture
def environment_request(tmp_path):
    settings = Settings(installation=str(tmp_path / "CS2"), cfg=str(tmp_path / "cfg"),
                        video_directory=str(tmp_path / "videos"))
    return worker.make_observation_request(settings, tmp_path / "pov.vpk", tmp_path / "NVIDIA Overlay.exe", request_id="a" * 32)


def snapshot_for(environment_request):
    return EnvironmentSnapshot.decode({key: getattr(environment_request, key) for key in
        ("installation", "cfg_directory", "output_directory", "hud_resource_path", "nvidia_executable", "nvidia_app_executable")} | {
        "cs2_version": "1.41.8.8", "hud_sha256": "a" * 64, "nvidia_version": "128.4.13.34",
        "nvidia_executable_sha256": "b" * 64, "nvidia_app_sha256": "c" * 64,
        "nvidia_app_component_version": "128.4.13.34", "driver_version": "32.0.15.8157"})


@pytest.mark.parametrize("raw", [b"[]", b"{", b'{"a":1,"a":2}', b'{"a":NaN}',
    b'{"a":1e309}', b" " * 65_537], ids=("array", "malformed", "duplicate", "nan", "overflow", "oversized"))
def test_environment_worker_rejects_ambiguous_nonfinite_or_oversized_json(raw):
    with pytest.raises(DataError): worker.strict_json(raw)


def test_request_roundtrip_contains_only_paths_not_self_reported_versions(environment_request):
    raw = environment_request.to_dict()
    assert raw["request_id"] == "a" * 32 and raw["schema"] == 1
    assert "snapshot" not in raw and "nvidia_version" not in raw
    assert worker.decode_observation_request(raw) == environment_request
    assert len(environment_request.configuration_sha256) == 64


@pytest.mark.parametrize("field,value", [("request_id", "bad"), ("schema", 2),
    ("output_directory", "relative"), ("cfg_directory", "\\\\server\\cfg")])
def test_request_rejects_wrong_scope_unknown_schema_or_unsafe_paths(environment_request, field, value):
    with pytest.raises(DataError): worker.decode_observation_request({**environment_request.to_dict(), field: value})


def test_current_observation_result_is_bound_to_request_and_configuration(environment_request):
    snapshot = snapshot_for(environment_request)
    clock = iter((30.0, 31.0))
    result = worker.run_request(environment_request.to_dict(), observer=lambda *args, **kw: snapshot, clock=lambda: next(clock))
    assert result.snapshot == snapshot and result.started_at == 30 and result.observed_at == 31
    assert result.configuration_sha256 == environment_request.configuration_sha256
    assert worker.decode_observation_result(result.to_dict(), request=environment_request) == result
    wrong = replace(environment_request, request_id="b" * 32)
    with pytest.raises(DataError): worker.decode_observation_result(result.to_dict(), request=wrong)


def test_unknown_observation_stays_unknown_and_cannot_be_decoded_as_pass(environment_request):
    result = worker.run_request(environment_request.to_dict(), observer=lambda *args, **kw: None, clock=lambda: 30.0)
    assert result.snapshot is None and result.reason
    raw = result.to_dict(); raw["snapshot"] = {"nvidia_version": "unknown"}
    with pytest.raises(DataError): worker.decode_observation_result(raw, request=environment_request)


def test_result_paths_must_match_requested_current_settings(environment_request):
    snapshot = snapshot_for(environment_request)
    wrong = replace(snapshot, output_directory=str(Path(environment_request.output_directory).parent / "foreign"))
    with pytest.raises(DataError): worker.run_request(environment_request.to_dict(), observer=lambda *args, **kw: wrong, clock=lambda: 30)


@pytest.mark.parametrize("mutation", ["configuration", "future_time", "extra", "wrong_hash", "unknown_no_reason"])
def test_result_strict_decoding_rejects_foreign_or_malformed_envelopes(environment_request, mutation):
    result = worker.run_request(environment_request.to_dict(), observer=lambda *args, **kw: None, clock=lambda: 30.0)
    raw = result.to_dict()
    if mutation == "configuration": raw["configuration_sha256"] = "b" * 64
    elif mutation == "future_time": raw["started_at"] = 31
    elif mutation == "extra": raw["extra"] = True
    elif mutation == "wrong_hash": raw["configuration_sha256"] = "invalid"
    else: raw["reason"] = ""
    with pytest.raises(DataError): worker.decode_observation_result(raw, request=environment_request)


def test_atomic_worker_result_refuses_to_overwrite_existing_evidence(tmp_path):
    path = tmp_path / "result.json"; path.write_bytes(b"original")
    with pytest.raises(DataError): worker.emit_result({"error": "new"}, path)
    assert path.read_bytes() == b"original"


def test_real_environment_child_keeps_missing_configuration_unknown_without_database_or_game(environment_request, tmp_path):
    path = tmp_path / "request.json"; path.write_text(json.dumps(environment_request.to_dict()), encoding="utf-8")
    result_path = tmp_path / "result.json"
    result = subprocess.run([sys.executable, "-m", "cs2pov.services.environment_worker", "--mode", "observe",
        "--request", str(path), "--result", str(result_path)], capture_output=True, timeout=15,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
    assert result.returncode == 0, (result.stdout, result.stderr)
    decoded = worker.decode_observation_result(worker.strict_json(result_path.read_bytes()), request=environment_request)
    assert decoded.snapshot is None and decoded.reason
    assert not list(tmp_path.rglob("*.sqlite")) and not list(tmp_path.rglob("*.dem"))


def test_real_environment_child_malformed_request_does_not_observe_or_overwrite(tmp_path):
    path = tmp_path / "request.json"; path.write_text('{"request_id":0}', encoding="utf-8")
    result_path = tmp_path / "result.json"
    result = subprocess.run([sys.executable, "-m", "cs2pov.services.environment_worker", "--mode", "observe",
        "--request", str(path), "--result", str(result_path)], capture_output=True, timeout=15,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
    assert result.returncode == 1
    assert set(worker.strict_json(result_path.read_bytes())) == {"error"}
    assert path.read_text(encoding="utf-8") == '{"request_id":0}'
