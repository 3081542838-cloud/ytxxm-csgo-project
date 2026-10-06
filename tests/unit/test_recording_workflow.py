from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.services.recording_workflow import RecordingWorkflow, RecordingWorkflowError
from cs2pov.adapters.video import FileSignature, VideoMetadata, current_signature
from cs2pov.services.output_validation import OutputDiscoveryResult, OutputValidationResult, validate_output
from cs2pov.services.recording import RecordingError
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.services.replay import ReplayEvidence
from cs2pov.storage.library import Library
from cs2pov.storage.settings import DataError


class FakeSession:
    def __init__(self):
        self.state = "idle"
        self.calls = []
        self.confirmation_token = None
        self.task_id = 'a' * 32

    def awaiting(self, state):
        self.state = state
        self.confirmation_token = 'token-'+state

    def confirm(self, state, token):
        if self.state != state or token != self.confirmation_token:
            raise RecordingWorkflowError('incorrect or stale confirmation')
        self.confirmation_token = None

    def arm(self, proof):
        self.calls.append(("arm", proof)); self.awaiting('awaiting_not_recording')

    def confirm_not_recording(self, *, step_token):
        self.confirm('awaiting_not_recording', step_token)
        self.calls.append(("confirm_not_recording",)); self.state = "start_ready"

    def send_start(self):
        self.calls.append(("send_start",)); self.awaiting('awaiting_started')

    def confirm_started(self, *, step_token):
        self.confirm('awaiting_started', step_token)
        self.calls.append(("confirm_started",)); self.state = "recording"

    def mark_end(self, proof):
        self.calls.append(("mark_end", proof)); self.state = "stop_ready"

    def send_stop(self):
        self.calls.append(("send_stop",)); self.awaiting('awaiting_stopped')

    def confirm_stopped(self, *, step_token):
        self.confirm('awaiting_stopped', step_token)
        self.calls.append(("confirm_stopped",)); self.state = "stopped"

    def cancel(self):
        self.calls.append(("cancel",)); self.state = "cancelled"; self.confirmation_token = None

    def poll(self):
        self.calls.append(("poll",))


def signature(path):
    path = Path(path)
    return FileSignature(path, 10, 123_000_000_000, 123_000_000_000,
                         ('stat', 1, int(hashlib.sha256(str(path).encode()).hexdigest()[:16], 16)), None)


def metadata(path, *, expected=None, **_kwargs):
    return VideoMetadata(Path(path).resolve(), 4.5, 1920, 1080, 1, 1, expected or signature(path))


def draft_fixture(tmp_path):
    sha = 'b' * 64
    return dict(demo=str(tmp_path / 'demo.dem'), fingerprint={'sha256': sha}, map='de_example',
        selection=dict(player_id='101', start_tick=10, end_tick=20, server_start_tick=100,
            server_end_tick=110, content_sha256=sha, hud={'id': 'h1', 'opacity': 1.}))


def confirm_content(workflow, **changes):
    fields = dict(task_id=workflow.task_id, candidate_id=workflow.candidate_id,
        step_token=workflow.content_token, target_view_confirmed=True, hud_confirmed=True,
        audio_confirmed=True, range_confirmed=True, clean_picture_confirmed=True,
        head_seconds=2., tail_seconds=2.)
    fields.update(changes)
    return workflow.confirm_content(**fields)


def setup(tmp_path, *, candidates=(), stabilize=None, probe=metadata, store=None):
    output = tmp_path / "videos"; output.mkdir(exist_ok=True)
    fake = FakeSession()
    snapshots = []
    def snapshot(path):
        snapshots.append(Path(path)); return {Path(path) / "old.mp4": signature(Path(path) / "old.mp4")}
    def find(path, before, *, started_wall=None, stopped_wall=None, cancel=None):
        assert before and snapshots == [Path(path)]
        return tuple(candidates)
    workflow = RecordingWorkflow(output, "Alt+F9", nvidia_path_confirmed=True,
        disk_checker=lambda *_: None, session=fake, snapshot=snapshot,
        find_candidates=find, stabilize=stabilize or (lambda path, **_kwargs: signature(path)),
        probe=probe, record_store=store, clock=lambda: 123.0, draft=draft_fixture(tmp_path),
        signature_reader=lambda path, **_kwargs: signature(path))
    return workflow, fake, output


def ready(workflow):
    workflow.arm("start"); workflow.confirm_not_recording(step_token=workflow.confirmation_token); workflow.send_start()
    workflow.confirm_started(step_token=workflow.confirmation_token); workflow.mark_end("end"); workflow.send_stop(); workflow.confirm_stopped(step_token=workflow.confirmation_token)


def test_metadata_success_does_not_prove_the_recorded_player_hud_or_audio(tmp_path):
    candidate = signature(tmp_path / 'videos' / 'new.mp4')
    workflow, _, _ = setup(tmp_path, candidates=(candidate,))
    ready(workflow)
    workflow.verify_output()
    assert workflow.state == 'awaiting_content'


def test_stop_wall_is_frozen_and_persisted_before_the_stop_hotkey(tmp_path):
    workflow, fake, _ = setup(tmp_path)
    contexts = []
    workflow._workflow_checkpoint = contexts.append
    workflow.arm('start'); workflow.confirm_not_recording(step_token=workflow.confirmation_token)
    workflow.send_start(); workflow.confirm_started(step_token=workflow.confirmation_token)
    workflow.mark_end('end')
    workflow._clock = lambda: 130.
    def stop():
        assert workflow.stopped_wall == contexts[-1].stopped_wall == 130.
        assert contexts[-1].state == 'stop_ready'
        fake.awaiting('awaiting_stopped')
    fake.send_stop = stop
    workflow.send_stop()


def test_full_workflow_requires_content_confirmations_and_saves_independent_restore_result(tmp_path):
    output = tmp_path / "videos"; output.mkdir()
    path = output / "new.mp4"; path.write_bytes(b"video")
    candidate = signature(path)
    library = Library(tmp_path / "library.sqlite")
    workflow, fake, _ = setup(tmp_path, candidates=(candidate,), store=library)
    ready(workflow)
    assert workflow.discover_outputs() == (candidate,)
    assert workflow.state == "awaiting_video_stable"
    result = workflow.verify_output(record_id="r1", payload={"player": "101"}, restore_status="complete")
    row = library.record("r1")
    assert result.duration == 4.5 and workflow.state == 'awaiting_content'
    assert json.loads(row['payload'])['video_result'] == 'metadata_verified'
    confirm_content(workflow)
    row = library.record('r1')
    assert workflow.state == 'verified' and json.loads(row['payload'])['video_result'] == 'verified'
    assert row["restore_status"] == "complete" and '"player": "101"' in row["payload"]
    assert path.read_bytes() == b"video"
    assert [call[0] for call in fake.calls] == ["arm", "confirm_not_recording", "send_start",
        "confirm_started", "mark_end", "send_stop", "confirm_stopped"]
    library.close()


def test_multiple_candidates_require_explicit_selection_and_reject_unknown_path(tmp_path):
    first = signature(tmp_path / "videos" / "one.mp4")
    second = signature(tmp_path / "videos" / "two.mp4")
    workflow, _, output = setup(tmp_path, candidates=(first, second))
    ready(workflow); workflow.discover_outputs()
    assert workflow.state == "awaiting_video_selection"
    with pytest.raises(RecordingWorkflowError): workflow.choose_output(output / "other.mp4")
    workflow.choose_output(second.path)
    assert workflow.selected == second and workflow.state == "awaiting_video_stable"
    workflow.verify_output()


def test_no_new_video_does_not_guess_an_old_file(tmp_path):
    workflow, _, _ = setup(tmp_path, candidates=())
    ready(workflow)
    with pytest.raises(RecordingWorkflowError, match="没有发现"):
        workflow.discover_outputs()
    assert workflow.state == "awaiting_video" and workflow.selected is None


def test_stability_or_probe_failure_keeps_record_unwritten(tmp_path):
    candidate = signature(tmp_path / "videos" / "new.mp4")
    library = Library(tmp_path / "library.sqlite")
    def fail(_, **_kwargs): raise RuntimeError("still writing")
    workflow, _, _ = setup(tmp_path, candidates=(candidate,), stabilize=fail, store=library)
    ready(workflow); workflow.discover_outputs()
    with pytest.raises(RecordingWorkflowError, match="检查失败"):
        workflow.verify_output(record_id="bad")
    assert workflow.state == "video_error" and library.records() == []
    library.close()


def test_record_save_failure_does_not_claim_verified(tmp_path):
    candidate = signature(tmp_path / "videos" / "new.mp4")
    class BrokenStore:
        def save_record(self, *args): raise OSError("database is read-only")
    workflow, _, _ = setup(tmp_path, candidates=(candidate,), store=BrokenStore())
    ready(workflow); workflow.discover_outputs()
    with pytest.raises(RecordingWorkflowError, match="记录保存失败"):
        workflow.verify_output()
    assert workflow.state == "record_error"


def test_unknown_nvidia_state_cannot_be_reinterpreted_as_a_video_result(tmp_path):
    candidate = signature(tmp_path / "videos" / "new.mp4")
    workflow, fake, _ = setup(tmp_path, candidates=(candidate,))
    ready(workflow)
    fake.state = "unknown"
    workflow.poll()
    with pytest.raises(RecordingWorkflowError, match="先确认 NVIDIA"):
        workflow.discover_outputs()
    assert workflow.state == "unknown"


@pytest.mark.parametrize('stage', RecordingWorkflow.OUTPUT_STATES)
def test_poll_preserves_each_stopped_output_substep_and_existing_result(tmp_path, stage):
    first = signature(tmp_path/'videos'/'one.mp4')
    second = signature(tmp_path/'videos'/'two.mp4')
    workflow, fake, _ = setup(tmp_path, candidates=(first, second))
    ready(workflow)
    if stage == 'discovering_video':
        workflow.begin_output_discovery()
    elif stage == 'output_cancelled':
        workflow.cancel_output_validation()
    elif stage != 'awaiting_video':
        workflow.discover_outputs()
    if stage not in ('awaiting_video', 'discovering_video', 'awaiting_video_selection', 'output_cancelled'):
        workflow.choose_output(first.path)
    if stage == 'validating_video': workflow.begin_output_validation()
    elif stage in ('verified', 'awaiting_content'):
        workflow.verify_output()
        if stage == 'verified': confirm_content(workflow)
    elif stage == 'video_error':
        def bad_probe(_, **_kwargs): raise RuntimeError('cannot decode')
        workflow._probe = bad_probe
        with pytest.raises(RecordingWorkflowError): workflow.verify_output()
    elif stage == 'record_error':
        class BrokenStore:
            def save_record(self, *_): raise OSError('cannot save')
        workflow.record_store = BrokenStore()
        with pytest.raises(RecordingWorkflowError): workflow.verify_output()
    assert workflow.state == stage and fake.state == 'stopped'
    saved = workflow.candidates, workflow.selected, workflow.metadata, workflow.error
    for _ in range(3):
        assert workflow.poll() == stage
        assert (workflow.candidates, workflow.selected, workflow.metadata, workflow.error) == saved


@pytest.mark.parametrize('action', ['discover', 'choose', 'verify'])
def test_output_entry_rechecks_underlying_unknown_state_without_prior_poll(tmp_path, action):
    candidate = signature(tmp_path/'videos'/'new.mp4')
    workflow, fake, _ = setup(tmp_path, candidates=(candidate, signature(tmp_path/'videos'/'two.mp4')))
    ready(workflow)
    if action != 'discover': workflow.discover_outputs()
    if action == 'verify': workflow.choose_output(candidate.path)
    fake.state = 'unknown'
    operation = {'discover': workflow.discover_outputs,
                 'choose': lambda: workflow.choose_output(candidate.path),
                 'verify': workflow.verify_output}[action]
    with pytest.raises(RecordingWorkflowError, match='先确认 NVIDIA'): operation()
    assert workflow.state == 'unknown' and workflow.metadata is None


@pytest.mark.parametrize('method', ['arm', 'confirm_not_recording', 'send_start',
    'confirm_started', 'mark_end', 'send_stop', 'confirm_stopped', 'cancel', 'poll'])
def test_every_delegated_exception_synchronizes_unknown_before_another_action(tmp_path, method):
    workflow, fake, _ = setup(tmp_path)
    if method not in ('arm', 'cancel', 'poll'): workflow.arm('start')
    if method in ('send_start', 'confirm_started', 'mark_end', 'send_stop', 'confirm_stopped'):
        workflow.confirm_not_recording(step_token=workflow.confirmation_token)
    if method in ('confirm_started', 'mark_end', 'send_stop', 'confirm_stopped'): workflow.send_start()
    if method in ('mark_end', 'send_stop', 'confirm_stopped'):
        workflow.confirm_started(step_token=workflow.confirmation_token)
    if method in ('send_stop', 'confirm_stopped'): workflow.mark_end('end')
    if method == 'confirm_stopped': workflow.send_stop()
    def fail(*_args, **_kwargs):
        fake.state = 'unknown'
        raise RecordingWorkflowError('external state uncertain')
    setattr(fake, method, fail)
    arguments = {'arm': ('start',), 'mark_end': ('end',)}.get(method, ())
    keywords = {'step_token': workflow.confirmation_token} if method.startswith('confirm_') else {}
    with pytest.raises(RecordingWorkflowError, match='uncertain'):
        getattr(workflow, method)(*arguments, **keywords)
    assert workflow.state == 'unknown'
    with pytest.raises(RecordingWorkflowError): workflow.discover_outputs()


def test_stop_confirmation_returning_without_known_stopped_does_not_enable_file_verification(tmp_path):
    workflow, fake, _ = setup(tmp_path)
    workflow.arm('start'); workflow.confirm_not_recording(step_token=workflow.confirmation_token)
    workflow.send_start(); workflow.confirm_started(step_token=workflow.confirmation_token)
    workflow.mark_end('end'); workflow.send_stop()
    fake.confirm_stopped = lambda **_kwargs: setattr(fake, 'state', 'unknown')
    with pytest.raises(RecordingWorkflowError, match='先确认 NVIDIA'):
        workflow.confirm_stopped(step_token=workflow.confirmation_token)
    assert workflow.state == 'unknown'


def test_token_is_forwarded_unchanged_and_old_steps_cannot_authorize_the_next_step(tmp_path):
    workflow, fake, _ = setup(tmp_path)
    workflow.arm('start')
    first = workflow.confirmation_token
    with pytest.raises(RecordingWorkflowError): workflow.confirm_not_recording(step_token='foreign')
    assert fake.state == workflow.state == 'awaiting_not_recording'
    workflow.confirm_not_recording(step_token=first)
    workflow.send_start()
    second = workflow.confirmation_token
    assert second != first
    with pytest.raises(RecordingWorkflowError): workflow.confirm_started(step_token=first)
    assert fake.state == workflow.state == 'awaiting_started'
    workflow.confirm_started(step_token=second)
    assert workflow.state == 'recording'


def test_start_wall_and_baseline_are_persisted_before_input_and_fast_output_is_found(tmp_path):
    output = tmp_path/'videos'; output.mkdir()
    old = output/'old.mp4'; old.write_bytes(b'old'); os.utime(old, (90, 90))
    fresh = output/'fresh.mp4'
    contexts, wall_reads = [], []
    wall = [100.]
    fake = FakeSession()
    original_send = fake.send_start
    workflow = RecordingWorkflow(output, 'Alt+F9', nvidia_path_confirmed=True,
        disk_checker=lambda *_: None, session=fake, workflow_checkpoint=contexts.append,
        clock=lambda: wall_reads.append(wall[0]) or wall[0],
        stabilize=lambda path, **_kwargs: current_signature(path), probe=metadata,
        draft=draft_fixture(tmp_path))
    def send():
        assert workflow.started_wall == contexts[-1].started_wall == 100.
        assert contexts[-1].state == 'start_ready'
        assert contexts[-1].baseline[0][0] == str(old)
        original_send()
        fresh.write_bytes(b'a fast first frame')
        os.utime(fresh, (100.1, 100.1))
        wall[0] = 150.
    fake.send_start = send
    ready(workflow)
    assert workflow.started_wall == 100. and workflow.stopped_wall == 150. and wall_reads == [100., 150.]
    assert [item.path for item in workflow.discover_outputs()] == [fresh]
    assert workflow.verify_output().path == fresh.resolve()
    assert json.loads(json.dumps(asdict(contexts[-1]), allow_nan=False))['started_wall'] == 100.
    assert old.read_bytes() == b'old' and fresh.read_bytes() == b'a fast first frame'
    with pytest.raises(AttributeError): workflow.started_wall = 200.
    with pytest.raises(AttributeError): workflow.output_directory = tmp_path


@pytest.mark.parametrize('phase', ['baseline', 'before_input'])
def test_workflow_checkpoint_failure_consumes_task_and_prevents_input_retry(tmp_path, phase):
    workflow, fake, _ = setup(tmp_path)
    calls = []
    def fail_context(value):
        calls.append(value)
        if (phase == 'baseline' and value.started_wall is None) or value.started_wall is not None:
            raise OSError('context disk full')
    workflow._workflow_checkpoint = fail_context
    if phase == 'baseline':
        with pytest.raises(RecordingWorkflowError, match='检查点'): workflow.arm('start')
        assert workflow.before == {} and not any(call[0] == 'arm' for call in fake.calls)
    else:
        workflow.arm('start'); workflow.confirm_not_recording(step_token=workflow.confirmation_token)
        with pytest.raises(RecordingWorkflowError, match='检查点'): workflow.send_start()
    assert fake.state == workflow.state == 'cancelled'
    assert not any(call[0] == 'send_start' for call in fake.calls)
    workflow.poll()
    with pytest.raises(RecordingWorkflowError): workflow.send_start()
    assert not any(call[0] == 'send_start' for call in fake.calls)


@pytest.mark.parametrize('value', [None, True, float('nan'), float('inf'), -1.])
def test_invalid_wall_clock_never_reaches_start_input(tmp_path, value):
    workflow, fake, _ = setup(tmp_path)
    workflow.arm('start'); workflow.confirm_not_recording(step_token=workflow.confirmation_token)
    workflow._clock = lambda: value
    with pytest.raises(RecordingWorkflowError, match='时间无效'): workflow.send_start()
    assert not any(call[0] == 'send_start' for call in fake.calls)


def test_default_session_factory_receives_strict_task_readers_checkpoint_and_separate_clock(tmp_path):
    output = tmp_path/'videos'; output.mkdir()
    captured = {}
    draft, identity, game = object(), object(), object()
    start_reader, end_reader, checkpoint, monotonic = (lambda: None, lambda: None, lambda _: None, lambda: 10.)
    fake = FakeSession()
    def factory(directory, hotkey, **kwargs):
        captured.update(kwargs)
        assert directory == output and hotkey == 'Alt+F9'
        return fake
    workflow = RecordingWorkflow(output, 'Alt+F9', nvidia_path_confirmed=True,
        disk_checker=lambda *_: None, draft=draft, expected_identity=identity, game=game,
        preview_reader=start_reader, end_reader=end_reader, checkpoint=checkpoint,
        session_factory=factory, clock=lambda: 123., session_clock=monotonic, confirmation_timeout=15)
    assert workflow.session is fake
    assert captured['draft'] is draft and captured['expected_identity'] is identity and captured['game'] is game
    assert captured['preview_reader'] is start_reader and captured['end_reader'] is end_reader
    assert captured['checkpoint'] is checkpoint and captured['clock'] is monotonic
    assert captured['confirmation_timeout'] == 15


@pytest.mark.parametrize('directory', ['', 'relative/videos', '//server/share/videos'])
def test_ambiguous_output_directory_is_rejected_before_session_factory(tmp_path, directory):
    calls = []
    with pytest.raises(DataError):
        RecordingWorkflow(directory, 'Alt+F9', nvidia_path_confirmed=True,
            disk_checker=lambda *_: None, session_factory=lambda *_args, **_kwargs: calls.append(True))
    assert calls == []


def test_baseline_read_failure_never_arms_or_persists_a_partial_baseline(tmp_path):
    workflow, fake, _ = setup(tmp_path)
    contexts = []
    workflow._workflow_checkpoint = contexts.append
    def unreadable(_): raise OSError('cannot read video directory')
    workflow._snapshot = unreadable
    with pytest.raises(OSError, match='cannot read'): workflow.arm('start')
    assert workflow.state == fake.state == 'idle'
    assert workflow.before == {} and fake.calls == [] and contexts == []


def test_output_reader_failure_preserves_error_across_polling(tmp_path):
    workflow, fake, _ = setup(tmp_path)
    ready(workflow)
    def unreadable(*_args, **_kwargs): raise OSError('directory removed')
    workflow._find_candidates = unreadable
    with pytest.raises(RecordingWorkflowError, match='目录失败'): workflow.discover_outputs()
    assert fake.state == 'stopped' and workflow.state == 'video_error'
    assert 'directory removed' in workflow.error
    assert workflow.poll() == 'video_error'


def test_unknown_session_discovered_during_probe_cannot_write_a_verified_record(tmp_path):
    candidate = signature(tmp_path/'videos'/'new.mp4')
    library = Library(tmp_path/'library.sqlite')
    workflow, fake, _ = setup(tmp_path, candidates=(candidate,), store=library)
    ready(workflow)
    def probe(path, **kwargs):
        fake.state = 'unknown'
        return metadata(path, **kwargs)
    workflow._probe = probe
    with pytest.raises(RecordingWorkflowError, match='先确认 NVIDIA'):
        workflow.verify_output(record_id='must-not-exist')
    assert workflow.state == 'unknown' and library.records() == []
    library.close()


@pytest.mark.parametrize('bad_start', [False, True])
def test_real_strict_recording_session_is_connected_without_weaker_proof(tmp_path, bad_start):
    output = tmp_path/'videos'; output.mkdir()
    demo = tmp_path/'demo.dem'; demo.write_bytes(b'readonly fixture')
    identity = ProcessIdentity(42, 123, 'C:/game/cs2.exe')
    game = SimpleNamespace(argv=['cs2.exe', '-insecure'], verify=lambda: identity)
    clock = [100.]
    start = ReplayEvidence(identity, True, demo, 10, 100, True, '101', True, 100.)
    end = replace(start, tick=20, server_tick=110)
    sha = hashlib.sha256(demo.read_bytes()).hexdigest()
    draft = dict(demo=str(demo), fingerprint={'sha256': sha}, selection=dict(
        player_id='101', start_tick=10, end_tick=20, server_start_tick=100,
        server_end_tick=110, content_sha256=sha))
    checkpoints, inputs, contexts = [], [], []
    def send_once(key, target, *, expected_identity):
        assert expected_identity == target.verify() == identity
        assert checkpoints[-1].state in ('start_pending', 'stop_pending')
        assert contexts[-1].started_wall == 1000.
        inputs.append(key)
    workflow = RecordingWorkflow(output, 'Alt+F9', nvidia_path_confirmed=True,
        disk_checker=lambda *_: None, draft=draft, expected_identity=identity, game=game,
        preview_reader=lambda: replace(start, local_demo=not bad_start, observed_at=clock[0]),
        end_reader=lambda: replace(end, observed_at=clock[0]),
        checkpoint=checkpoints.append, workflow_checkpoint=contexts.append,
        session_clock=lambda: clock[0], clock=lambda: 1000.,
        hotkey_adapter=SimpleNamespace(send_once=send_once))
    workflow.arm(start)
    first = workflow.confirmation_token
    workflow.confirm_not_recording(step_token=first)
    if bad_start:
        with pytest.raises(RecordingError, match='证据'): workflow.send_start()
        assert inputs == [] and workflow.state == 'start_ready'
    else:
        workflow.send_start()
        with pytest.raises(RecordingError): workflow.confirm_started(step_token=first)
        workflow.confirm_started(step_token=workflow.confirmation_token)
        clock[0] += 1
        workflow.mark_end(replace(end, observed_at=clock[0]))
        workflow.send_stop(); workflow.confirm_stopped(step_token=workflow.confirmation_token)
        assert workflow.poll() == 'awaiting_video'
        assert inputs == ['Alt+F9', 'Alt+F9']


def pending_validation(tmp_path, *, store=None):
    workflow, fake, _ = setup(tmp_path, candidates=(signature(tmp_path / 'videos' / 'new.mp4'),), store=store)
    ready(workflow); workflow.discover_outputs()
    request = workflow.begin_output_validation()
    result = validate_output(request, stabilize=workflow._stabilize, probe=workflow._probe)
    return workflow, fake, request, result


@pytest.mark.parametrize('field', ['head_seconds', 'tail_seconds'])
@pytest.mark.parametrize('value', [2.001, -.001, float('nan'), float('inf'), True, '2', 10**1000],
                         ids=['over_limit', 'negative', 'nan', 'inf', 'bool', 'string', 'huge_int'])
def test_content_head_and_tail_each_enforce_two_seconds_without_rounding(tmp_path, field, value):
    workflow, _, request, result = pending_validation(tmp_path)
    workflow.commit_output_validation(request, result)
    with pytest.raises(RecordingWorkflowError, match='2 秒'):
        confirm_content(workflow, **{field: value})
    assert workflow.state == 'awaiting_content'
    confirm_content(workflow)
    assert workflow.state == 'verified'


@pytest.mark.parametrize('field', ['target_view_confirmed', 'hud_confirmed', 'audio_confirmed',
                                  'range_confirmed', 'clean_picture_confirmed'])
@pytest.mark.parametrize('value', [False, 1, None])
def test_content_requires_each_real_boolean_confirmation(tmp_path, field, value):
    workflow, _, request, result = pending_validation(tmp_path)
    workflow.commit_output_validation(request, result)
    with pytest.raises(RecordingWorkflowError, match='人工确认'):
        confirm_content(workflow, **{field: value})
    assert workflow.state == 'awaiting_content'


@pytest.mark.parametrize('field,value', [('task_id', 'c' * 32), ('candidate_id', 'd' * 64), ('step_token', 'e' * 32)])
def test_content_token_is_bound_to_exact_task_and_candidate_and_is_single_use(tmp_path, field, value):
    library = Library(tmp_path / 'library.sqlite')
    workflow, _, request, result = pending_validation(tmp_path, store=library)
    workflow.commit_output_validation(request, result, record_id='bound', restore_status='blocked')
    old_token = workflow.content_token
    with pytest.raises(RecordingWorkflowError): confirm_content(workflow, **{field: value})
    assert json.loads(library.record('bound')['payload'])['video_result'] == 'metadata_verified'
    confirm_content(workflow)
    assert library.record('bound')['restore_status'] == 'blocked'
    with pytest.raises(RecordingWorkflowError): confirm_content(workflow, step_token=old_token)
    assert workflow.state == 'verified' and workflow.content_token is None
    library.close()


@pytest.mark.parametrize('phase', ['discovery', 'validation', 'content'])
def test_output_cancel_rejects_late_results_and_never_changes_confirmed_nvidia_state(tmp_path, phase):
    candidate = signature(tmp_path / 'videos' / 'new.mp4')
    workflow, fake, _ = setup(tmp_path, candidates=(candidate,))
    ready(workflow)
    if phase == 'discovery':
        request = workflow.begin_output_discovery()
        late = lambda: workflow.commit_output_discovery(request, OutputDiscoveryResult(request.task_id, request.request_id, (candidate,)))
        late_failure = lambda: workflow.fail_output_discovery(request, 'late error')
    else:
        workflow.discover_outputs(); request = workflow.begin_output_validation()
        result = validate_output(request, stabilize=workflow._stabilize, probe=workflow._probe)
        if phase == 'validation':
            late = lambda: workflow.commit_output_validation(request, result)
            late_failure = lambda: workflow.fail_output_validation(request, 'late error')
        else:
            workflow.commit_output_validation(request, result)
            token = workflow.content_token
            late = lambda: confirm_content(workflow, step_token=token)
            late_failure = late
    calls = list(fake.calls)
    workflow.cancel_output_validation()
    with pytest.raises(RecordingWorkflowError): late()
    with pytest.raises(RecordingWorkflowError): late_failure()
    assert workflow.state == 'output_cancelled' and fake.state == 'stopped' and fake.calls == calls
    assert workflow.content_token is None


def test_metadata_commit_is_separate_from_worker_and_rejects_duplicate_or_foreign_result(tmp_path):
    library = Library(tmp_path / 'library.sqlite')
    workflow, _, request, result = pending_validation(tmp_path, store=library)
    assert workflow.state == 'validating_video' and workflow.metadata is None and library.records() == []
    with pytest.raises(RecordingWorkflowError): workflow.commit_output_validation(request, replace(result, task_id='c' * 32))
    with pytest.raises(RecordingWorkflowError): workflow.commit_output_validation(request, replace(result, request_id='d' * 32))
    assert library.records() == [] and workflow.state == 'validating_video'
    workflow.commit_output_validation(request, result, record_id='once')
    assert workflow.state == 'awaiting_content'
    with pytest.raises(RecordingWorkflowError): workflow.commit_output_validation(request, result, record_id='twice')
    assert [row['id'] for row in library.records()] == ['once']
    library.close()


@pytest.mark.parametrize('field,value', [('bytes', 11), ('mtime_ns', 124_000_000_000),
                                       ('identity', ('stat', 1, 999)), ('change_ns', 77)])
def test_metadata_commit_rechecks_complete_signature_including_identity_and_change_time(tmp_path, field, value):
    library = Library(tmp_path / 'library.sqlite')
    workflow, fake, request, result = pending_validation(tmp_path, store=library)
    workflow._signature_reader = lambda *_args, **_kwargs: replace(result.metadata.signature, **{field: value})
    with pytest.raises(RecordingWorkflowError, match='变化'): workflow.commit_output_validation(request, result)
    assert library.records() == [] and workflow.state == 'video_error' and fake.state == 'stopped'
    library.close()


def test_content_rechecks_file_after_human_review_and_keeps_metadata_only_record_on_change(tmp_path):
    library = Library(tmp_path / 'library.sqlite')
    workflow, _, request, result = pending_validation(tmp_path, store=library)
    workflow.commit_output_validation(request, result, record_id='changed')
    workflow._signature_reader = lambda *_args, **_kwargs: replace(result.metadata.signature, bytes=11)
    with pytest.raises(RecordingWorkflowError, match='变化'): confirm_content(workflow)
    record = json.loads(library.record('changed')['payload'])
    assert record['video_result'] == 'metadata_verified' and 'content_confirmation' not in record
    assert workflow.state == 'video_error' and workflow.content_token is None
    library.close()


@pytest.mark.parametrize('operation', ['cancel', 'expire'])
def test_content_authority_is_rechecked_after_file_reader_returns(tmp_path, operation):
    library = Library(tmp_path / 'library.sqlite')
    workflow, _, request, result = pending_validation(tmp_path, store=library)
    now = [workflow._output_issued]
    workflow._content_clock = lambda: now[0]
    workflow.commit_output_validation(request, result, record_id='delayed')
    def reader(*_args, **_kwargs):
        if operation == 'cancel': workflow.cancel_output_validation()
        else: now[0] += workflow._content_timeout
        return result.metadata.signature
    workflow._signature_reader = reader
    with pytest.raises(RecordingWorkflowError): confirm_content(workflow)
    assert json.loads(library.record('delayed')['payload'])['video_result'] == 'metadata_verified'
    library.close()


@pytest.mark.parametrize('phase', ['discovery', 'validation'])
def test_output_job_deadline_rejects_late_commit_without_any_database_write(tmp_path, phase):
    workflow, fake, _ = setup(tmp_path, candidates=(signature(tmp_path / 'videos' / 'new.mp4'),))
    now = [0.]
    workflow._content_clock = lambda: now[0]
    ready(workflow)
    if phase == 'discovery':
        request = workflow.begin_output_discovery()
        result = OutputDiscoveryResult(request.task_id, request.request_id, (signature(tmp_path / 'videos' / 'new.mp4'),))
        commit = lambda: workflow.commit_output_discovery(request, result)
    else:
        workflow.discover_outputs(); request = workflow.begin_output_validation()
        result = validate_output(request, stabilize=workflow._stabilize, probe=workflow._probe)
        commit = lambda: workflow.commit_output_validation(request, result)
    now[0] = 120.
    with pytest.raises(RecordingWorkflowError, match='过期'): commit()
    assert workflow.state == 'video_error' and fake.state == 'stopped'


@pytest.mark.parametrize('payload', [{'video_result': 'verified'}, {'draft_snapshot': {}}, {'player': '999'},
                                   {'content_confirmation': {'hud_confirmed': True}}, {'task_id': 'c' * 32}])
def test_caller_payload_cannot_replace_frozen_scope_or_claim_content(tmp_path, payload):
    library = Library(tmp_path / 'library.sqlite')
    workflow, _, request, result = pending_validation(tmp_path, store=library)
    with pytest.raises(RecordingWorkflowError, match='覆盖'):
        workflow.commit_output_validation(request, result, payload=payload)
    assert library.records() == []
    workflow.commit_output_validation(request, result, payload={'note': 'keep this annotation'})
    record = json.loads(library.records()[0]['payload'])
    assert record['note'] == 'keep this annotation' and record['player'] == '101'
    assert record['video_result'] == 'metadata_verified' and 'content_confirmation' not in record
    library.close()


def test_frozen_draft_summary_cannot_be_mutated_through_source_or_returned_snapshot(tmp_path):
    workflow, _, request, result = pending_validation(tmp_path)
    snapshot = workflow.draft_snapshot
    snapshot['selection']['player_id'] = '999'
    snapshot['fingerprint']['sha256'] = 'c' * 64
    workflow.commit_output_validation(request, result)
    assert workflow.draft_snapshot['selection']['player_id'] == '101'
    assert workflow.draft_snapshot['fingerprint']['sha256'] == 'b' * 64


def test_content_save_failure_never_claims_verified_or_leaves_a_reusable_token(tmp_path):
    class BrokenOnSecondSave:
        def __init__(self): self.records = []
        def save_record(self, _id, payload, _restore):
            if self.records: raise OSError('disk full')
            self.records.append(json.loads(json.dumps(payload)))
    store = BrokenOnSecondSave()
    workflow, fake, request, result = pending_validation(tmp_path, store=store)
    workflow.commit_output_validation(request, result)
    token = workflow.content_token
    with pytest.raises(RecordingWorkflowError, match='记录保存失败'): confirm_content(workflow)
    assert workflow.state == 'record_error' and workflow.content_token is None and fake.state == 'stopped'
    assert store.records[0]['video_result'] == 'metadata_verified'
    with pytest.raises(RecordingWorkflowError): confirm_content(workflow, step_token=token)


def test_output_checkpoint_failure_preserves_stopped_state_and_revokes_request(tmp_path):
    workflow, fake, _ = setup(tmp_path, candidates=(signature(tmp_path / 'videos' / 'new.mp4'),))
    ready(workflow)
    inputs = list(fake.calls)
    def fail(_): raise OSError('checkpoint disk full')
    workflow._workflow_checkpoint = fail
    with pytest.raises(RecordingWorkflowError, match='检查点'): workflow.begin_output_discovery()
    assert fake.state == 'stopped' and fake.calls == inputs and workflow.state == 'record_error'
    assert workflow._discovery_request is None and workflow.discovery_token is None


@pytest.mark.parametrize('stop', [None, True, float('nan'), float('inf'), -1., 122.])
def test_invalid_or_backwards_stop_wall_does_not_send_stop_input(tmp_path, stop):
    workflow, fake, _ = setup(tmp_path)
    workflow.arm('start'); workflow.confirm_not_recording(step_token=workflow.confirmation_token)
    workflow.send_start(); workflow.confirm_started(step_token=workflow.confirmation_token); workflow.mark_end('end')
    workflow._clock = lambda: stop
    with pytest.raises(RecordingWorkflowError, match='时间无效'): workflow.send_stop()
    assert workflow.state == 'stop_ready' and not any(call[0] == 'send_stop' for call in fake.calls)


def test_discovery_commit_and_selection_are_bound_to_current_task_and_nonce(tmp_path):
    candidates = tuple(signature(tmp_path / 'videos' / name) for name in ('one.mp4', 'two.mp4'))
    workflow, _, _ = setup(tmp_path, candidates=candidates)
    ready(workflow); request = workflow.begin_output_discovery()
    result = OutputDiscoveryResult(request.task_id, request.request_id, candidates)
    for wrong in (replace(result, task_id='c' * 32), replace(result, request_id='d' * 32)):
        with pytest.raises(RecordingWorkflowError): workflow.commit_output_discovery(request, wrong)
    workflow.commit_output_discovery(request, result)
    with pytest.raises(RecordingWorkflowError): workflow.commit_output_discovery(request, result)
    for wrong in (dict(task_id='c' * 32, step_token=request.request_id),
                  dict(task_id=request.task_id, step_token='d' * 32)):
        with pytest.raises(RecordingWorkflowError): workflow.choose_output(candidates[1].path, **wrong)
    assert workflow.selected is None and workflow.state == 'awaiting_video_selection'
    workflow.choose_output(candidates[1].path, task_id=request.task_id, step_token=request.request_id)
    assert workflow.selected == candidates[1]


@pytest.mark.parametrize('offset,allowed', [(0, True), (2, True), (2.001, False), (-.001, False)])
def test_discovery_commit_enforces_frozen_wall_window_and_exact_stop_grace(tmp_path, offset, allowed):
    workflow, _, _ = setup(tmp_path)
    ready(workflow); request = workflow.begin_output_discovery()
    timestamp = int((request.stopped_wall + offset) * 1_000_000_000)
    candidate = replace(signature(tmp_path / 'videos' / 'new.mp4'), mtime_ns=timestamp, ctime_ns=timestamp)
    result = OutputDiscoveryResult(request.task_id, request.request_id, (candidate,))
    if allowed: assert workflow.commit_output_discovery(request, result) == (candidate,)
    else:
        with pytest.raises(RecordingWorkflowError): workflow.commit_output_discovery(request, result)
        assert workflow.candidates == () and workflow.selected is None


@pytest.mark.parametrize('candidate_kind', ['old', 'outside', 'deep', 'nonvideo'])
def test_discovery_commit_rejects_old_or_unscoped_candidate_even_if_worker_claims_it(tmp_path, candidate_kind):
    workflow, _, output = setup(tmp_path)
    ready(workflow); request = workflow.begin_output_discovery()
    paths = dict(old=output / 'old.mp4', outside=tmp_path / 'outside.mp4',
                 deep=output / 'one' / 'two' / 'new.mp4', nonvideo=output / 'note.txt')
    candidate = signature(paths[candidate_kind])
    with pytest.raises(RecordingWorkflowError):
        workflow.commit_output_discovery(request, OutputDiscoveryResult(request.task_id, request.request_id, (candidate,)))
    assert workflow.candidates == ()


def test_candidate_selection_does_not_resolve_an_unlisted_parent_traversal_alias(tmp_path):
    candidates = tuple(signature(tmp_path / 'videos' / name) for name in ('one.mp4', 'two.mp4'))
    workflow, _, output = setup(tmp_path, candidates=candidates)
    ready(workflow); workflow.discover_outputs()
    with pytest.raises(RecordingWorkflowError): workflow.choose_output(output / 'folder' / '..' / 'one.mp4')
    assert workflow.selected is None


def test_workflow_freezes_original_draft_before_the_caller_mutates_it(tmp_path):
    output = tmp_path / 'videos'; output.mkdir()
    original = draft_fixture(tmp_path)
    workflow = RecordingWorkflow(output, 'Alt+F9', nvidia_path_confirmed=True,
        disk_checker=lambda *_: None, session=FakeSession(), draft=original)
    original['demo'] = str(tmp_path / 'wrong.dem')
    original['selection']['player_id'] = '999'
    original['selection']['hud']['opacity'] = 0.
    original['fingerprint']['sha256'] = 'c' * 64
    assert workflow.draft_snapshot == draft_fixture(tmp_path)


def test_injected_session_scope_cannot_disagree_with_the_frozen_output_draft(tmp_path):
    from cs2pov.services.recording import RecordingScope
    workflow, fake, _ = setup(tmp_path)
    other = draft_fixture(tmp_path); other['selection']['player_id'] = '999'
    fake.scope = RecordingScope.freeze(other)
    ready(workflow)
    with pytest.raises(RecordingWorkflowError, match='范围'): workflow.begin_output_discovery()
    assert workflow.candidates == () and fake.state == 'stopped'
