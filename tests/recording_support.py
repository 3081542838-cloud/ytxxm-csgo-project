from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.disk import DiskError, MIN_VIDEO_BYTES
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplaySnapshot
from cs2pov.services.hidden_playback import HiddenPlayback
from cs2pov.services.recording_task import RecordingTask, RecordingTaskError
from cs2pov.services.recording_workflow import RecordingWorkflow
from cs2pov.services.replay import ReplayController, ReplayEvidence
from cs2pov.storage.settings import Settings


class Preview:
    """Held preview using real replay and hidden playback validation."""

    def __init__(self, env, demo, output):
        self.env = env
        self.session = demo.parent / 'data' / 'sessions' / 'preview-owned'
        self.session.mkdir(parents=True)
        sha = fingerprint(demo)['sha256']
        self.draft = dict(demo=str(demo), fingerprint={'sha256': sha}, selection=dict(
            player_id='101', start_tick=10, end_tick=20, server_start_tick=100,
            server_end_tick=110, duration=1., content_sha256=sha))
        self.settings = Settings(video_directory=str(output), nvidia_path_confirmed=True)
        self.clock = lambda: env.now
        self.game = SimpleNamespace(argv=['cs2.exe', '-insecure'], verify=self.verify,
                                    launch=self.launch)
        self.console = SimpleNamespace(console_open_confirmed=False,
            foreground_pid=lambda: env.foreground, readback=lambda: env.proof,
            snapshot=lambda: env.movement, command=self.command,
            press_play_key=self.press_play_key)
        self.controller = ReplayController(self.game, self.console, clock=lambda: env.now)
        self.playback = HiddenPlayback(self.controller, self.session, self.draft,
            clock=lambda: env.now, checkpoint=self.persist, reader_factory=self.binding_reader)
        self.playback.state = 'ready'
        self.playback.binding_may_exist = True
        self.state = 'play_ready'
        self.console_step_token = None
        self.console_confirmed = False
        self.restoration = None
        self.error = ''
        self.close_polls = 0

    def verify(self):
        if self.env.closed:
            raise RuntimeError('owned process exited')
        return self.env.identity

    def launch(self, *_args, **_kwargs):
        self.env.launches += 1
        raise AssertionError('RecordingTask must never launch another game')

    def command(self, command):
        assert self.console.console_open_confirmed
        self.env.events.append(('query', command))
        self.env.queries.append(command)

    def binding_reader(self, _session, identity, nonce, *, expected, clock):
        proof = SimpleNamespace(process_identity=identity, key='F8', request_nonce=nonce,
                                value='', observed_at=clock())
        return SimpleNamespace(readback=lambda: proof)

    def persist(self):
        self.env.events.append(('preview_checkpoint', self.playback.state,
                                self.playback.triggered))

    def press_play_key(self):
        assert not self.console.console_open_confirmed
        assert self.env.foreground == self.env.identity.pid
        assert self.playback.triggered
        assert self.env.events[-1] == ('preview_checkpoint', 'playing', True)
        self.env.keys += 1
        self.env.events.append(('F8',))

    def play_clip(self):
        assert self.state == 'play_ready'
        self.playback.request_play()
        self.state = 'awaiting_play_foreground'
        self.persist()

    def check_playback_binding(self):
        assert self.state == 'play_ended'
        self.env.binding_checks += 1
        self.state = 'awaiting_end_console'
        self.console_step_token = 'final-console-step'
        self.console_confirmed = False

    def confirm_console(self, *, step_token):
        if (self.state != 'awaiting_end_console' or self.console_confirmed
                or step_token != self.console_step_token):
            raise ValueError('invalid console confirmation')
        self.console_confirmed = True

    def poll(self):
        if self.state == 'closing':
            self.close_polls -= 1
            if self.close_polls <= 0:
                self.finish_close()
            return None
        if self.state == 'awaiting_end_console':
            self.controller.verify_result(self.draft, since=self.playback.started_at,
                                           at_end=True, require_foreground=False)
            if self.console_confirmed and self.env.foreground == self.env.identity.pid:
                self.console.console_open_confirmed = True
                self.playback.confirm_end_console()
                self.console_confirmed = False
                self.state = 'checking_binding'
            return None
        if self.state in ('play_ended', 'play_verified'):
            return self.controller.verify_result(self.draft, since=self.playback.started_at,
                                                 at_end=True, require_foreground=False)
        proof = self.playback.poll()
        self.state = {'ready': 'play_ready', 'awaiting_foreground': 'awaiting_play_foreground',
                      'playing': 'playing', 'ended': 'play_ended',
                      'querying_unbound': 'checking_binding', 'verified': 'play_verified'}[self.playback.state]
        return proof

    def stop(self):
        self.env.closes += 1
        self.env.events.append(('close',))
        if self.close_polls:
            self.state = 'closing'
        else:
            self.finish_close()

    def finish_close(self):
        self.env.closed = True
        self.env.restores += 1
        self.restoration = {'gameinfo': 'restored'}
        self.state = 'complete'


def recording_setup(tmp_path):
    demo = tmp_path / 'original.dem'
    demo.write_bytes(b'readonly original fixture')
    output = tmp_path / 'videos'
    output.mkdir()
    env = SimpleNamespace(now=100., identity=ProcessIdentity(42, 123, 'C:/game/cs2.exe'),
        foreground=999, closed=False, launches=0, keys=0, closes=0, restores=0,
        binding_checks=0, queries=[], inputs=[], events=[], task_checkpoints=[],
        recording_checkpoints=[], contexts=[], disk_free=MIN_VIDEO_BYTES + 1,
        checkpoint_failure=None, recording_failure=None, input_failure=False)
    env.proof = ReplayEvidence(env.identity, True, demo, 10, 100, True, '101', True, env.now)
    env.movement = ReplaySnapshot(env.identity, demo, True, 10, True, '101', True, env.now)
    preview = Preview(env, demo, output)

    def disk(_directory, required=MIN_VIDEO_BYTES):
        env.events.append(('disk', env.disk_free))
        if env.disk_free < required:
            raise DiskError('below 10GB')

    def recording_checkpoint(value):
        env.recording_checkpoints.append(value)
        env.events.append(('recording_checkpoint', value.state))
        if env.recording_failure is not None:
            env.recording_failure(value)

    def input_once(key, game, *, expected_identity):
        assert game is preview.game and game.verify() == expected_identity
        assert env.foreground == expected_identity.pid
        assert env.recording_checkpoints[-1].state in ('start_pending', 'stop_pending')
        env.inputs.append(key)
        env.events.append(('NVIDIA', key))
        if env.input_failure:
            raise RuntimeError('input outcome unknown')

    workflow = RecordingWorkflow(output, preview.settings.hotkey,
        nvidia_path_confirmed=True, disk_checker=disk, game=preview.game,
        expected_identity=env.identity, draft=preview.draft,
        preview_reader=lambda: preview.controller.verify_result(preview.draft,
            since=env.now-5, require_foreground=False),
        end_reader=lambda: preview.controller.verify_result(preview.draft,
            since=workflow.session.started_at, at_end=True, require_foreground=False),
        checkpoint=recording_checkpoint, workflow_checkpoint=env.contexts.append,
        hotkey_adapter=SimpleNamespace(send_once=input_once),
        session_clock=lambda: env.now, clock=lambda: 1000.+env.now-100.)

    def task_checkpoint(value):
        env.task_checkpoints.append(deepcopy(value))
        env.events.append(('task_checkpoint', value['state']))
        if env.checkpoint_failure is not None:
            env.checkpoint_failure(value)

    task = RecordingTask(preview, workflow, checkpoint=task_checkpoint, clock=lambda: env.now)
    return task, preview, workflow, env


def fresh(env, *, end=False, moving=False, change=None):
    env.now += .1
    env.proof = replace(env.proof, tick=20 if end else 10,
        server_tick=110 if end else 100, observed_at=env.now, **(change or {}))
    env.movement = replace(env.movement, tick=20 if end else 15 if moving else 10,
                           paused=not moving, observed_at=env.now)


def authorize_start(task, env):
    task.arm()
    task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
    fresh(env)
    env.foreground = env.identity.pid
    task.poll()
    assert task.state == 'awaiting_started' and len(env.inputs) == 1


def playing(task, env):
    authorize_start(task, env)
    env.foreground = 999
    task.confirm_started(task_id=task.task_id, step_token=task.confirmation_token)
    assert env.keys == 0
    fresh(env)
    env.foreground = env.identity.pid
    task.poll()
    assert env.keys == 1
    fresh(env, moving=True)
    task.poll()


def stopped_wait(task, env):
    playing(task, env)
    fresh(env, end=True)
    task.poll()
    assert task.state == 'awaiting_stopped' and len(env.inputs) == 2


def finish(task, preview, env):
    task.confirm_stopped(task_id=task.task_id, step_token=task.confirmation_token)
    assert task.state == 'awaiting_end_console' and env.queries == []
    task.confirm_console(task_id=task.task_id, session_id=preview.session.name,
                         step_token=preview.console_step_token)
    task.poll()
    task.poll()
