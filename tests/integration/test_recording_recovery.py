"""Disk recovery evidence is independent of journal and SQLite task status."""
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import subprocess

import pytest

from cs2pov.services.recording_recovery import (
    RecordingRecoveryError, scan_recording_recovery, resolve_recording_recovery,
)
from cs2pov.services import recording_recovery


def write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def session_at(root, name="preview-recording"):
    session = root / "sessions" / name
    session.mkdir(parents=True)
    write_json(session / "journal.json", {"complete": True, "entries": {}})
    return session


def evidence(session):
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns,
                        path.stat().st_file_attributes)
            for path in session.iterdir() if path.is_file()}


def complete_records(session, state="stopped", *, task_state="complete"):
    stopped = state == "stopped"
    checkpoint = dict(state=state, reason="", triggered="stop" if stopped else "none",
        started_at=2.0 if stopped else None, stopped_at=4.0 if stopped else None,
        task_id="a" * 32, scope=dict(demo=str(session.parent.parent / "original.dem"),
            player_id="101", start_tick=10, end_tick=20, server_start_tick=100,
            server_end_tick=110, content_sha256="b" * 64),
        expected_identity=dict(pid=42, created=123, executable="C:/game/cs2.exe"),
        confirmation_token=None, deadline=None,
        start_attempted_at=1.0 if stopped else None, stop_attempted_at=3.0 if stopped else None)
    task = dict(schema=1, task_id=checkpoint["task_id"], session_id=session.name,
        state=task_state, error="", cleanup_requested=True, terminal=True, durable=True,
        recording_state=state, preview_state="complete", restore_status={"gameinfo": "restored"})
    for field in ("triggered", "started_at", "stopped_at", "start_attempted_at", "stop_attempted_at",
                  "confirmation_token", "deadline"):
        task[field] = checkpoint[field]
    workflow = dict(state="start_ready" if stopped else "idle",
        output_directory=str(session.parent.parent / "videos"),
        started_wall=1000.0 if stopped else None, baseline=[])
    for name, value in (("recording-session.json", checkpoint), ("recording-task.json", task),
                        ("recording-workflow.json", workflow)):
        write_json(session / name, value)
    return checkpoint, task, workflow


def test_no_recording_files_is_normal_even_with_completed_journal(tmp_path):
    assert scan_recording_recovery(tmp_path) == []
    session = session_at(tmp_path)
    before = evidence(session)
    assert scan_recording_recovery(tmp_path) == []
    assert evidence(session) == before


def test_durable_proven_zero_start_delivery_does_not_require_manual_recovery(tmp_path):
    session = session_at(tmp_path)
    checkpoint, task, workflow = complete_records(session, 'cancelled')
    for record in (checkpoint, task):
        record['start_attempted_at'] = 1.0
        record['start_not_sent'] = True
    workflow['state'] = 'cancelled'
    workflow['started_wall'] = 1000.0
    for name, value in zip(recording_recovery.RECORDS, (checkpoint, task, workflow)):
        write_json(session / name, value)
    before = evidence(session)
    assert scan_recording_recovery(tmp_path) == []
    assert evidence(session) == before


@pytest.mark.parametrize('change', ['missing_receipt', 'mirror_mismatch', 'started', 'stop_attempt', 'bad_type'])
def test_zero_start_receipt_cannot_clear_uncertain_or_inconsistent_recording(tmp_path, change):
    session = session_at(tmp_path)
    checkpoint, task, workflow = complete_records(session, 'cancelled')
    for record in (checkpoint, task):
        record['start_attempted_at'] = 1.0
        record['start_not_sent'] = True
    if change == 'missing_receipt':
        checkpoint.pop('start_not_sent'); task.pop('start_not_sent')
    elif change == 'mirror_mismatch':
        task['start_not_sent'] = False
    else:
        key, value = {'started': ('started_at', 2.0), 'stop_attempt': ('stop_attempted_at', 3.0),
                      'bad_type': ('start_not_sent', 1)}[change]
        checkpoint[key] = task[key] = value
    for name, value in zip(recording_recovery.RECORDS, (checkpoint, task, workflow)):
        write_json(session / name, value)
    assert len(scan_recording_recovery(tmp_path)) == 1


@pytest.mark.parametrize("state,triggered", [
    ("unknown", "start"), ("start_pending", "start"),
    ("stop_pending", "stop"), ("recording", "start"),
    ("awaiting_started", "start"), ("awaiting_stopped", "stop"),
    ("stopped", "stop"), ("cancelled", "none"),
])
def test_legacy_incomplete_checkpoint_blocks_independently_of_completed_journal(
        tmp_path, state, triggered):
    session = session_at(tmp_path)
    write_json(session / "recording-session.json", {"state": state, "triggered": triggered})
    before = evidence(session)
    items = scan_recording_recovery(tmp_path)
    assert len(items) == 1
    item = items[0]
    assert item.session_id == session.name and item.directory == session
    assert item.task_id is None and item.state == "unknown"
    assert "NVIDIA" in item.reason and len(item.digest) == 64
    with pytest.raises(FrozenInstanceError):
        item.state = "stopped"
    assert evidence(session) == before


@pytest.mark.parametrize("content", [b"{bad JSON", b"[]", b"null",
    b'{"state":"unknown","state":"stopped"}', b'{"deadline":NaN}'])
def test_readable_corrupt_checkpoint_keeps_original_and_manual_confirmation_is_separate(
        tmp_path, content):
    session = session_at(tmp_path)
    checkpoint = session / "recording-session.json"
    checkpoint.write_bytes(content)
    before = evidence(session)
    [item] = scan_recording_recovery(tmp_path)
    assert item.state == "unknown" and item.digest is not None
    result = resolve_recording_recovery(tmp_path, item, task_id=item.task_id,
                                       checkpoint_digest=item.digest)
    assert result.is_file() and result.parent == session
    raw = json.loads(result.read_text(encoding="utf-8"))
    assert raw["checkpoint_digest"] == item.digest
    assert raw["task_id"] == item.task_id
    assert raw["state"] == "manually_confirmed_stopped"
    assert all(evidence(session)[key] == value for key, value in before.items())
    assert scan_recording_recovery(tmp_path) == []
    assert not any(path.name in {"record.json", "video.json"} for path in session.iterdir())


def test_legacy_unknown_can_be_manually_confirmed_without_rewriting_checkpoint(tmp_path):
    session = session_at(tmp_path)
    write_json(session / "recording-session.json", {"state": "unknown", "triggered": "start"})
    before = evidence(session)
    [item] = scan_recording_recovery(tmp_path)
    resolution = resolve_recording_recovery(tmp_path, item, task_id=None, checkpoint_digest=item.digest)
    assert scan_recording_recovery(tmp_path) == []
    assert all(evidence(session)[key] == value for key, value in before.items())
    assert json.loads((session / "recording-session.json").read_text())["state"] == "unknown"
    assert resolution.name == f"recording-resolution-{item.digest}.json"


@pytest.mark.parametrize("change", ["checkpoint", "task", "workflow"])
def test_confirmation_is_bound_to_all_current_recording_evidence(tmp_path, change):
    session = session_at(tmp_path)
    write_json(session / "recording-session.json", {"state": "unknown", "triggered": "start"})
    [item] = scan_recording_recovery(tmp_path)
    filename = {"checkpoint": "recording-session.json", "task": "recording-task.json",
                "workflow": "recording-workflow.json"}[change]
    write_json(session / filename, {"state": "unknown", "triggered": "stop"})
    before = evidence(session)
    with pytest.raises(RecordingRecoveryError):
        resolve_recording_recovery(tmp_path, item, task_id=item.task_id, checkpoint_digest=item.digest)
    assert evidence(session) == before
    [new_item] = scan_recording_recovery(tmp_path)
    assert new_item.digest != item.digest
    result = resolve_recording_recovery(tmp_path, new_item, task_id=new_item.task_id,
                                       checkpoint_digest=new_item.digest)
    assert result.exists() and scan_recording_recovery(tmp_path) == []
    write_json(session / filename, {"state": "unknown", "triggered": "start", "later": True})
    assert scan_recording_recovery(tmp_path)


@pytest.mark.parametrize("bad", ["digest", "task", "directory", "session_id"])
def test_manual_resolution_rejects_stale_or_forged_item_without_writes(tmp_path, bad):
    session = session_at(tmp_path)
    write_json(session / "recording-session.json", {"state": "unknown", "triggered": "start"})
    [item] = scan_recording_recovery(tmp_path)
    before = evidence(session)
    kwargs = {"task_id": item.task_id, "checkpoint_digest": item.digest}
    if bad == "digest":
        kwargs["checkpoint_digest"] = "f" * 64
    elif bad == "task":
        kwargs["task_id"] = "a" * 32
    elif bad == "directory":
        item = replace(item, directory=tmp_path / "outside")
    else:
        item = replace(item, session_id="../outside")
    with pytest.raises(RecordingRecoveryError):
        resolve_recording_recovery(tmp_path, item, **kwargs)
    assert evidence(session) == before
    assert not (tmp_path / "outside").exists()


def test_existing_resolution_is_never_overwritten(tmp_path):
    session = session_at(tmp_path)
    write_json(session / "recording-session.json", {"state": "unknown", "triggered": "start"})
    [item] = scan_recording_recovery(tmp_path)
    target = session / f"recording-resolution-{item.digest}.json"
    target.write_bytes(b"damaged confirmation")
    before = evidence(session)
    assert scan_recording_recovery(tmp_path)
    with pytest.raises(RecordingRecoveryError):
        resolve_recording_recovery(tmp_path, item, task_id=None, checkpoint_digest=item.digest)
    assert evidence(session) == before


@pytest.mark.parametrize("redirect", ["sessions", "session", "checkpoint"])
def test_redirected_directory_or_checkpoint_is_not_followed_and_stays_blocked(tmp_path, redirect):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    write_json(outside / "recording-session.json", {"state": "unknown", "triggered": "start"})
    if redirect == "sessions":
        link = root / "sessions"
    else:
        (root / "sessions").mkdir()
        if redirect == "session":
            link = root / "sessions" / "redirected"
        else:
            (root / "sessions" / "preview").mkdir()
            # A directory redirect at the fixed checkpoint filename is invalid too.
            link = root / "sessions" / "preview" / "recording-session.json"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                   check=True, capture_output=True)
    before = evidence(outside)
    try:
        [item] = scan_recording_recovery(root)
        assert item.state == "unknown" and item.digest is None
        with pytest.raises(RecordingRecoveryError):
            resolve_recording_recovery(root, item, task_id=item.task_id, checkpoint_digest=item.digest)
        assert evidence(outside) == before
    finally:
        link.rmdir()


def test_oversized_recording_evidence_is_blocked_without_confirmation_digest(tmp_path):
    session = session_at(tmp_path)
    (session / "recording-session.json").write_bytes(b"x" * (1_048_576 + 1))
    [item] = scan_recording_recovery(tmp_path)
    assert item.state == "unknown" and item.digest is None


def test_context_without_checkpoint_is_not_silently_lost(tmp_path):
    session = session_at(tmp_path)
    write_json(session / "recording-task.json", {"task_id": "a" * 32})
    [item] = scan_recording_recovery(tmp_path)
    assert item.state == "unknown" and item.digest is not None


@pytest.mark.parametrize("state", ["idle", "blocked", "cancelled", "stopped"])
@pytest.mark.parametrize("task_state", ["complete", "recovery_blocked"])
def test_complete_safe_nvidia_state_clears_only_its_independent_block(tmp_path, state, task_state):
    session = session_at(tmp_path)
    complete_records(session, state, task_state=task_state)
    if task_state == "recovery_blocked":
        write_json(session / "journal.json", {"complete": False, "entries": {"gameinfo": {"state": "conflict"}}})
    before = evidence(session)
    assert scan_recording_recovery(tmp_path) == []
    assert evidence(session) == before


@pytest.mark.parametrize("field,value", [
    ("terminal", False), ("durable", False), ("state", "closing"),
    ("state", "awaiting_end_console"), ("recording_state", "unknown"),
    ("triggered", "start"), ("stop_attempted_at", 999),
    ("start_attempted_at", True),
    ("task_id", "c" * 32), ("session_id", "elsewhere"),
    ("schema", True), ("schema", 2),
])
def test_task_context_cannot_claim_completion_while_mismatched_or_unsettled(tmp_path, field, value):
    session = session_at(tmp_path)
    _checkpoint, task, _workflow = complete_records(session)
    task[field] = value
    write_json(session / "recording-task.json", task)
    before = evidence(session)
    [item] = scan_recording_recovery(tmp_path)
    assert item.state == "unknown" and item.task_id == "a" * 32
    assert evidence(session) == before


@pytest.mark.parametrize("field,value", [
    ("start_attempted_at", None), ("started_at", True), ("stopped_at", 0),
    ("stop_attempted_at", 1), ("triggered", "start"),
    ("confirmation_token", "d" * 32), ("deadline", 9),
])
def test_stopped_label_requires_complete_ordered_consumed_attempt_evidence(tmp_path, field, value):
    session = session_at(tmp_path)
    checkpoint, task, _workflow = complete_records(session)
    checkpoint[field] = value
    task[field] = value
    write_json(session / "recording-session.json", checkpoint)
    write_json(session / "recording-task.json", task)
    assert scan_recording_recovery(tmp_path)


def test_unknown_checkpoint_stays_blocked_even_with_complete_task(tmp_path):
    session = session_at(tmp_path)
    checkpoint, task, _workflow = complete_records(session)
    checkpoint["state"] = task["recording_state"] = "unknown"
    write_json(session / "recording-session.json", checkpoint)
    write_json(session / "recording-task.json", task)
    [item] = scan_recording_recovery(tmp_path)
    assert item.task_id == "a" * 32
    resolve_recording_recovery(tmp_path, item, task_id=item.task_id, checkpoint_digest=item.digest)
    assert scan_recording_recovery(tmp_path) == []
    assert json.loads((session / "recording-task.json").read_text())["recording_state"] == "unknown"


@pytest.mark.parametrize("name", ["recording-session.json", "recording-task.json", "recording-workflow.json"])
def test_missing_context_from_a_full_completed_task_blocks(tmp_path, name):
    session = session_at(tmp_path)
    complete_records(session)
    (session / name).unlink()
    assert scan_recording_recovery(tmp_path)


def test_additional_context_fields_are_preserved_and_do_not_invalidate_known_schema(tmp_path):
    session = session_at(tmp_path)
    checkpoint, task, workflow = complete_records(session)
    for name, value in (("recording-session.json", checkpoint), ("recording-task.json", task),
                        ("recording-workflow.json", workflow)):
        value["future_extension"] = {"some": "extra metadata"}
        write_json(session / name, value)
    before = evidence(session)
    assert scan_recording_recovery(tmp_path) == []
    assert evidence(session) == before


def test_count_limit_keeps_an_unscanned_recovery_block(tmp_path, monkeypatch):
    session_at(tmp_path, "first")
    second = session_at(tmp_path, "second")
    write_json(second / "recording-session.json", {"state": "unknown", "triggered": "start"})
    monkeypatch.setattr(recording_recovery, "MAX_SESSIONS", 1)
    items = scan_recording_recovery(tmp_path)
    assert any(item.directory is None and item.digest is None for item in items)


def test_scan_total_byte_limit_does_not_silently_skip_later_evidence(tmp_path, monkeypatch):
    session = session_at(tmp_path)
    write_json(session / "recording-session.json", {"state": "unknown", "triggered": "start"})
    monkeypatch.setattr(recording_recovery, "MAX_SCAN_BYTES", 10)
    [item] = scan_recording_recovery(tmp_path)
    assert item.digest is None and "上限" in item.reason


def test_workflow_timestamp_saved_before_input_does_not_invent_a_consumed_toggle(tmp_path):
    session = session_at(tmp_path)
    _checkpoint, _task, workflow = complete_records(session, "cancelled")
    workflow["started_wall"] = 1000.0
    write_json(session / "recording-workflow.json", workflow)
    assert scan_recording_recovery(tmp_path) == []


@pytest.mark.parametrize("field,value", [
    ("state", "future_unknown_state"), ("output_directory", "../outside"),
    ("started_wall", -1), ("started_wall", True), ("baseline", [["C:/video.mp4", True, 1, 1]]),
    ("baseline", [["C:/video.mp4", 1, 1]]), ("baseline", None),
])
def test_invalid_workflow_context_blocks_without_following_recorded_output_paths(tmp_path, field, value):
    session = session_at(tmp_path)
    _checkpoint, _task, workflow = complete_records(session)
    workflow[field] = value
    write_json(session / "recording-workflow.json", workflow)
    assert scan_recording_recovery(tmp_path)
    assert not (tmp_path / "videos").exists() and not (tmp_path / "outside").exists()


def test_scan_and_manual_confirmation_cannot_send_hotkeys_or_inspect_the_game(tmp_path, monkeypatch):
    from cs2pov.services.recording import RecordingSession
    from cs2pov.adapters.owned_process import OwnedGame

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Recovery must never resume game or NVIDIA input")

    monkeypatch.setattr(RecordingSession, "send_start", forbidden)
    monkeypatch.setattr(RecordingSession, "send_stop", forbidden)
    monkeypatch.setattr(OwnedGame, "verify", forbidden)
    session = session_at(tmp_path)
    write_json(session / "recording-session.json", {"state": "unknown", "triggered": "start"})
    [item] = scan_recording_recovery(tmp_path)
    resolve_recording_recovery(tmp_path, item, task_id=None, checkpoint_digest=item.digest)
    assert scan_recording_recovery(tmp_path) == []


@pytest.mark.parametrize("field,value", [("confirmation_token", "d" * 32), ("deadline", 9),
                                         ("start_attempted_at", 0)])
def test_unconsumed_cancelled_label_requires_cleared_authorization_and_attempt_times(tmp_path, field, value):
    session = session_at(tmp_path)
    checkpoint, task, _workflow = complete_records(session, "cancelled")
    checkpoint[field] = task[field] = value
    write_json(session / "recording-session.json", checkpoint)
    write_json(session / "recording-task.json", task)
    assert scan_recording_recovery(tmp_path)


@pytest.mark.parametrize("session_id", [None, "", [], 1])
def test_malformed_structured_session_id_is_a_safe_recovery_error(tmp_path, session_id):
    session = session_at(tmp_path)
    write_json(session / "recording-session.json", {"state": "unknown", "triggered": "start"})
    [item] = scan_recording_recovery(tmp_path)
    before = evidence(session)
    with pytest.raises(RecordingRecoveryError):
        resolve_recording_recovery(tmp_path, replace(item, session_id=session_id),
                                   task_id=None, checkpoint_digest=item.digest)
    assert evidence(session) == before


def test_a_previous_checkpoint_changed_while_reading_context_never_clears_recovery(tmp_path, monkeypatch):
    session = session_at(tmp_path)
    checkpoint, _task, _workflow = complete_records(session)
    original_reader = recording_recovery.read_small
    changed = []

    def racing_read(path):
        result = original_reader(path)
        if path.name == "recording-task.json" and not changed:
            changed.append(True)
            checkpoint["state"] = "unknown"
            write_json(session / "recording-session.json", checkpoint)
        return result

    monkeypatch.setattr(recording_recovery, "read_small", racing_read)
    assert scan_recording_recovery(tmp_path)
    assert changed and json.loads((session / "recording-session.json").read_text())["state"] == "unknown"


def test_checkpoint_changed_while_reading_manual_resolution_cannot_reuse_it(tmp_path, monkeypatch):
    session = session_at(tmp_path)
    checkpoint = {"state": "unknown", "triggered": "start"}
    write_json(session / "recording-session.json", checkpoint)
    [item] = scan_recording_recovery(tmp_path)
    marker = resolve_recording_recovery(tmp_path, item, task_id=None, checkpoint_digest=item.digest)
    before_marker = marker.read_bytes()
    original_reader = recording_recovery.read_small
    changed = []

    def racing_read(path):
        result = original_reader(path)
        if path.name == marker.name and not changed:
            changed.append(True)
            checkpoint["triggered"] = "stop"
            write_json(session / "recording-session.json", checkpoint)
        return result

    monkeypatch.setattr(recording_recovery, "read_small", racing_read)
    assert scan_recording_recovery(tmp_path)
    assert changed and marker.read_bytes() == before_marker


@pytest.mark.parametrize("path", ["../outside", "C:/bad\nvideo.mp4", "//server/video.mp4"])
def test_baseline_paths_must_have_the_same_local_absolute_boundary_as_output_directory(tmp_path, path):
    session = session_at(tmp_path)
    _checkpoint, _task, workflow = complete_records(session)
    workflow["baseline"] = [[path, 1, 1, 1]]
    write_json(session / "recording-workflow.json", workflow)
    assert scan_recording_recovery(tmp_path)


def test_readable_deeply_malformed_json_keeps_digest_bound_manual_recovery(tmp_path):
    session = session_at(tmp_path)
    checkpoint = session / "recording-session.json"
    checkpoint.write_bytes(b"[" * 10_000 + b"0" + b"]" * 10_000)
    before = checkpoint.read_bytes()
    [item] = scan_recording_recovery(tmp_path)
    assert item.digest is not None
    resolve_recording_recovery(tmp_path, item, task_id=item.task_id, checkpoint_digest=item.digest)
    assert checkpoint.read_bytes() == before and scan_recording_recovery(tmp_path) == []
