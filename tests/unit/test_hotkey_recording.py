from cs2pov.services.hotkey_recording import HotkeyRecording
from tests.recording_support import recording_setup, fresh


def test_direct_flow_sends_two_hotkeys_and_restores_without_confirmation_or_uia(tmp_path):
    task, preview, workflow, env = recording_setup(tmp_path)
    runner = HotkeyRecording(task)
    task.arm()
    env.foreground = env.identity.pid
    runner.poll()
    task.poll()
    assert env.inputs == ['Alt+F9']
    runner.poll()
    fresh(env)
    task.poll()
    fresh(env, moving=True)
    task.poll()
    fresh(env, end=True)
    task.poll()
    runner.poll()
    assert env.inputs == ['Alt+F9', 'Alt+F9'] and env.keys == 1
    assert task.terminal and env.closes == env.restores == 1
    assert env.queries == [] and env.binding_checks == 0
    assert task.snapshot()['dispatch_mode'] == 'direct_hotkey'
    assert task.snapshot()['nvidia_state_verified'] is False
    assert workflow.state == 'awaiting_video'
    for _ in range(5):
        runner.poll()
        task.poll()
    assert env.inputs == ['Alt+F9', 'Alt+F9']


def test_direct_flow_input_failure_is_not_retried(tmp_path):
    task, preview, workflow, env = recording_setup(tmp_path)
    runner = HotkeyRecording(task)
    task.arm()
    env.foreground = env.identity.pid
    runner.poll()
    env.input_failure = True
    task.poll()
    for _ in range(5):
        runner.poll()
        task.poll()
    assert env.inputs == ['Alt+F9']
    assert task.terminal and env.restores == 1


def test_cancelled_direct_runner_does_not_advance_or_send(tmp_path):
    task, _, _, env = recording_setup(tmp_path)
    runner = HotkeyRecording(task)
    task.arm()
    runner.cancel()
    runner.poll()
    assert task.state == 'awaiting_not_recording' and env.inputs == []
