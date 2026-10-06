"""Workspace must retain actual failed pipe resources until public cleanup succeeds.

The held PreviewSession and CommandPipes run their real cleanup paths. Only
the game, filesystem transaction and Win32 backend are injected; no native
input or game launch takes place.
"""
from types import SimpleNamespace

import pytest

from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError
from test_preview_session import automatic_setup


@pytest.fixture
def pending_workspace(tmp_path, qapp):
    preview, env, _proof = automatic_setup(tmp_path)
    model = Workspace(tmp_path)
    model.settings = preview.settings
    model._preview = preview
    model.set_busy(True, 'held automatic preview')
    with model.library.db:
        model.library.db.execute('INSERT INTO sessions VALUES (?, ?)',
                                 (preview.session.name, 'connecting_game'))
    preview.start()
    env.close_attempts = []
    env.close_failure = True

    def close(handle):
        env.close_attempts.append(handle)
        if handle == 11 and env.close_failure:
            raise OSError('transient CloseHandle failure')
        env.pipe_closed.append(handle)

    preview.pipes.backend.close = close
    preview.stop()
    assert preview.state == 'recovery_blocked'
    assert preview.pipe_cleanup_pending and preview.pipes.handles == [11]
    assert preview.transaction.data['complete'] and env.closed == env.restored == 1
    model._preview_timer.start()
    yield model, preview, env
    model._preview_timer.stop()
    env.close_failure = False
    env.restore_failure = False
    if preview.pipe_cleanup_pending:
        preview.stop()
    model.library.close()


def test_poll_retains_failed_resources_and_busy_until_explicit_cleanup(pending_workspace):
    model, preview, env = pending_workspace
    model.poll_preview()
    assert model._preview is preview and model.busy
    assert not model._preview_timer.isActive()
    assert model.library.unfinished() == [preview.session.name]
    assert '命令通路' in model.preview_status and '重试' in model.preview_status
    before = env.close_attempts[:], env.restored, env.closed
    model.poll_preview()
    assert (env.close_attempts, env.restored, env.closed) == before
    assert preview.pipes.handles == [11] and env.sent == []


def test_public_stop_retries_cleanup_but_does_not_release_on_failure(pending_workspace):
    model, preview, env = pending_workspace
    model.poll_preview()
    before = env.close_attempts.count(11)
    model.stop_preview()
    assert env.close_attempts.count(11) == before + 1
    assert model._preview is preview and model.busy
    assert preview.state == 'recovery_blocked' and preview.pipe_cleanup_pending
    assert preview.pipes.handles == [11] and model.library.unfinished() == [preview.session.name]
    assert env.closed == 1 and env.sent == []


def test_public_stop_rechecks_files_then_releases_only_after_success(pending_workspace):
    model, preview, env = pending_workspace
    model.poll_preview()
    env.close_failure = False
    env.restore_failure = True
    before = env.close_attempts[:]
    model.stop_preview()
    assert env.close_attempts == before  # A file conflict prevents cleanup completion.
    assert model._preview is preview and model.busy
    assert preview.pipe_cleanup_pending and model.library.unfinished() == [preview.session.name]
    env.restore_failure = False
    model.stop_preview()
    assert preview.state == 'complete' and not preview.pipe_cleanup_pending
    assert preview.pipes.handles == [] and preview.pipes.cleanup_errors == ()
    assert model._preview is None and not model.busy and not model._preview_timer.isActive()
    assert model.library.unfinished() == []
    assert env.closed == 1 and env.sent == []


@pytest.mark.parametrize('operation', ['prepare', 'restore', 'settings'])
def test_pending_resources_cannot_be_bypassed_by_false_busy_flag(pending_workspace, operation):
    model, preview, env = pending_workspace
    # A stale UI flag must not authorize a new task or file-only index shortcut.
    model.set_busy(False)
    assert model.busy
    action = {'prepare': model.start_preview,
              'restore': lambda: model.restore_session(preview.session),
              'settings': lambda: model.save_settings(model.settings)}[operation]
    before = env.close_attempts[:], env.restored, env.launched
    with pytest.raises(DataError, match='命令通路.*清理'):
        action()
    assert model._preview is preview and preview.pipes.handles == [11]
    assert (env.close_attempts, env.restored, env.launched) == before
    assert env.sent == []


def test_checkpoint_failure_retains_pending_resources_for_same_process_retry(pending_workspace):
    model, preview, env = pending_workspace
    model.library.db.execute("""CREATE TRIGGER fail_cleanup_checkpoint BEFORE UPDATE ON sessions
        BEGIN SELECT RAISE(ABORT, 'pipe checkpoint failure'); END""")
    before = env.close_attempts[:]
    model.poll_preview()
    assert model._preview is preview and model.busy and preview.pipe_cleanup_pending
    assert 'pipe checkpoint failure' in model.preview_status
    assert not model._preview_timer.isActive()
    assert env.close_attempts == before  # Database polls cannot become close retries.
    assert model.library.unfinished() == [preview.session.name] and env.sent == []
    model.library.db.execute('DROP TRIGGER fail_cleanup_checkpoint')
    env.close_failure = False
    model.stop_preview()
    assert model._preview is None and not model.busy
    assert preview.pipes.handles == [] and model.library.unfinished() == []
    assert env.closed == 1 and env.sent == []


def test_recording_terminal_hands_pending_preview_to_public_cleanup(pending_workspace):
    model, preview, env = pending_workspace
    def forbidden_cancel():
        pytest.fail('A terminal recording must never be rearmed or cancelled again')
    task = SimpleNamespace(state='recovery_blocked', error='pipe cleanup failure',
                           terminal=True, durable=True, poll=lambda: None,
                           cancel=forbidden_cancel)
    model._recording_task = task
    model.poll_recording()
    assert model._recording_task is None and model._last_recording_task is task
    assert model._preview is preview and model.busy
    assert '命令通路' in model.preview_status and '重试' in model.preview_status
    assert not model._preview_timer.isActive()
    env.close_failure = False
    model.stop_preview()
    assert model._preview is None and not model.busy
    assert model._last_recording_task is task and model.library.unfinished() == []
    assert env.sent == [] and env.closed == 1


@pytest.mark.parametrize('recording', [False, True])
def test_pending_pipe_failure_keeps_polling_while_owned_game_is_still_closing(tmp_path, qapp, recording):
    preview, env, _proof = automatic_setup(tmp_path)
    model = Workspace(tmp_path)
    model._preview = preview
    model.set_busy(True)
    with model.library.db:
        model.library.db.execute('INSERT INTO sessions VALUES (?, ?)',
                                 (preview.session.name, 'connecting_game'))
    preview.start()
    failed = {11}
    def close(handle):
        if handle in failed:
            raise OSError('temporary pipe close failure')
        env.pipe_closed.append(handle)
    preview.pipes.backend.close = close
    preview.game.force_close = lambda: setattr(env, 'closed', env.closed + 1)
    try:
        preview.stop()
        assert preview.state == 'closing' and preview.pipe_cleanup_pending and env.running
        model._preview_timer.start()
        if recording:
            model._recording_task = SimpleNamespace(state='recovery_blocked', error='cleanup timeout',
                terminal=True, durable=True, poll=lambda: None)
            model.poll_recording()
            assert model._recording_task is None
        else:
            model.poll_preview()
        assert model._preview is preview and model.busy
        assert model._preview_timer.isActive()  # Waiting for actual owned exit is still active.
        assert env.restored == 0 and env.sent == []
        env.running = False
        model.poll_preview()
        assert preview.state == 'recovery_blocked' and model._preview is preview and model.busy
        assert not model._preview_timer.isActive()
        assert env.restored == 1 and env.closed == 1
        failed.clear()
        model.stop_preview()
        assert model._preview is None and not model.busy and preview.state == 'complete'
        assert env.sent == [] and env.closed == 1
    finally:
        model._preview_timer.stop()
        env.running = False
        failed.clear()
        preview.stop()
        model.library.close()


@pytest.mark.parametrize('flag', ['absent', False])
def test_file_only_recovery_keeps_previous_unlocked_behavior(tmp_path, qapp, flag):
    model = Workspace(tmp_path)
    preview = SimpleNamespace(session=tmp_path/'sessions'/'file-only', state='recovery_blocked',
                              error='file conflict', console_confirmed=False, poll=lambda: None)
    if flag != 'absent':
        preview.pipe_cleanup_pending = flag
    model._preview = preview
    model.set_busy(True)
    with model.library.db:
        model.library.db.execute('INSERT INTO sessions VALUES (?, ?)',
                                 (preview.session.name, 'closing'))
    try:
        model.poll_preview()
        assert model._preview is None and not model.busy and model.recovery
        assert model.library.unfinished() == [preview.session.name]
    finally:
        model._preview_timer.stop()
        model.library.close()
