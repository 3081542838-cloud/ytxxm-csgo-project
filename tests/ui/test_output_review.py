from dataclasses import asdict
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from cs2pov.adapters.video import current_signature, VideoMetadata
from cs2pov.services.output_validation import OutputDiscoveryResult, OutputValidationResult
from cs2pov.services.recording_workflow import RecordingWorkflow
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError, HudPreset
from cs2pov.ui.window import MainWindow


class StoppedRecorder:
    task_id = 'a' * 32
    state = 'stopped'
    confirmation_token = None
    def poll(self): pass
    def cancel(self): raise AssertionError('output checking must never alter NVIDIA')


@pytest.fixture
def output_desktop(tmp_path, qtbot):
    model = Workspace(tmp_path / 'data')
    output = tmp_path / 'videos'; output.mkdir()
    draft = dict(demo=str(tmp_path / 'original.dem'), map='de_test', fingerprint={'sha256': 'b'*64},
                 selection=dict(player_id='101', start_tick=1, end_tick=2,
                                server_start_tick=100, server_end_tick=101,
                                content_sha256='b'*64, hud=asdict(HudPreset())))
    workflow = RecordingWorkflow(output, 'Alt+F9', nvidia_path_confirmed=True,
        disk_checker=lambda *args: None, session=StoppedRecorder(), draft=draft,
        record_store=model.library, started_wall=0)
    workflow._stopped_wall = 10**10
    workflow.state = 'awaiting_video'
    model._output_workflow = workflow
    window = MainWindow(model); qtbot.addWidget(window); window.show(); window.navigate(3)
    yield model, window, workflow, output
    model.cancel_output()
    if model._output_process:
        qtbot.waitUntil(lambda: model._output_process is None, timeout=3000)
    window.close(); model.library.close()


def metadata_ready(model, workflow, output):
    path = output / 'current.mp4'; path.write_bytes(b'video')
    sig = current_signature(path)
    request = workflow.begin_output_discovery()
    workflow.commit_output_discovery(request, OutputDiscoveryResult(workflow.task_id, request.request_id, (sig,)))
    request = workflow.begin_output_validation()
    workflow.commit_output_validation(request, OutputValidationResult(workflow.task_id, request.request_id,
        VideoMetadata(path, 1., 1920, 1080, 1, 1, sig)), restore_status='blocked')
    model.changed.emit()
    return path


def test_content_review_requires_every_checkbox_and_separate_measured_edges(output_desktop):
    model, window, workflow, output = output_desktop
    metadata_ready(model, workflow, output)
    assert window.content_box.isVisible() and not window.content_confirm.isEnabled()
    assert workflow.state == 'awaiting_content'
    for check in window.content_checks.values(): check.setChecked(True)
    window.content_head.setValue(2)
    assert not window.content_confirm.isEnabled()
    window.content_tail.setValue(2.001)
    assert not window.content_confirm.isEnabled()
    window.content_tail.setValue(2)
    assert window.content_confirm.isEnabled()
    window.content_confirm.click()
    row = model.library.record(workflow.record_id)
    assert workflow.state == 'verified' and row['restore_status'] == 'blocked'
    assert '"video_result": "verified"' in row['payload']
    assert not window.content_box.isVisible()


def test_late_content_button_cannot_confirm_cancelled_step(output_desktop):
    model, window, workflow, output = output_desktop
    metadata_ready(model, workflow, output)
    saved = window._content_confirmation_scope
    model.cancel_output()
    assert workflow.session.state == 'stopped' and workflow.state == 'output_cancelled'
    with pytest.raises(DataError):
        model.confirm_recording_content(task_id=saved[0], candidate_id=saved[1], step_token=saved[2],
            target_view_confirmed=True, hud_confirmed=True, audio_confirmed=True,
            range_confirmed=True, clean_picture_confirmed=True, head_seconds=0, tail_seconds=0)
    assert '"content_result": "unreviewed"' in model.library.record(workflow.record_id)['payload']


def test_actual_worker_does_not_freeze_event_loop_and_cancel_discards_late_results(output_desktop, qtbot):
    model, window, workflow, output = output_desktop
    path = output / 'empty.mp4'; path.write_bytes(b'')
    sig = current_signature(path)
    request = workflow.begin_output_discovery()
    workflow.commit_output_discovery(request, OutputDiscoveryResult(workflow.task_id, request.request_id, (sig,)))
    ticks = []
    timer = QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(1)); timer.start()
    model.validate_recording_output()
    qtbot.waitUntil(lambda: len(ticks) >= 5, timeout=2000)
    assert model.busy and model._output_process is not None
    window.output_cancel.click()
    qtbot.waitUntil(lambda: model._output_process is None, timeout=3000)
    timer.stop()
    assert not model.busy and workflow.state == 'output_cancelled'
    assert workflow.session.state == 'stopped' and model.library.records() == []
    assert path.read_bytes() == b''


def test_multiple_candidates_require_current_task_and_token(output_desktop):
    model, _, workflow, output = output_desktop
    paths = [output / 'one.mp4', output / 'two.mp4']
    for path in paths: path.write_bytes(b'candidate')
    request = workflow.begin_output_discovery()
    workflow.commit_output_discovery(request, OutputDiscoveryResult(workflow.task_id, request.request_id,
        tuple(current_signature(path) for path in paths)))
    with pytest.raises(DataError):
        model.choose_recording_output(paths[0], task_id='b'*32, step_token=workflow.discovery_token)
    assert workflow.state == 'awaiting_video_selection' and model._output_process is None
