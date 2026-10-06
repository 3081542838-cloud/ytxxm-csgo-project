"""Chromium exposes a disabled structural Pane above enabled recording controls."""
from dataclasses import replace

import pytest

from cs2pov.adapters import nvidia_status as status
from cs2pov.adapters.owned_process import ProcessIdentity


IDENTITY = ProcessIdentity(20028, 134356000000000001, r'C:\NVIDIA\NVIDIA Overlay.exe')
REQUEST = status.ObservationRequest('a' * 32, IDENTITY.executable, 'Alt+F9', 100.)


def observed_structure(action='开始'):
    # Observed metadata path: Window -> disabled Pane -> enabled Pane /
    # Document / Group -> enabled Record Button -> label and hotkey Text.
    rows = [(None, 'NVIDIA', 50032, True), (0, '', 50033, False),
            (1, '', 50033, True), (2, '', 50030, True), (3, '', 50026, True),
            (4, '', 50000, True), (5, '录制', 50020, True),
            (5, 'Alt+F9 - ' + action, 50020, True)]
    nodes = tuple(status.OverlayNode(parent, name, kind, False, enabled, IDENTITY.pid)
                  for parent, name, kind, enabled in rows)
    return status.OverlaySnapshot(REQUEST.request_id, 100.1, 100.2, True,
        (status.OverlayWindow(66370, IDENTITY, True, nodes),), '')


def classify(value):
    return status.classify_snapshot(value, REQUEST, now=100.3, identity_query=lambda _: IDENTITY)


def mutate(value, index, **changes):
    window = value.windows[0]
    nodes = list(window.nodes)
    nodes[index] = replace(nodes[index], **changes)
    return replace(value, windows=(replace(window, nodes=tuple(nodes)),))


@pytest.mark.parametrize(('action', 'expected'), [('开始', 'idle'), ('停止', 'recording'),
                                                ('停止并保存', 'recording')])
def test_actual_enabled_record_row_below_disabled_top_level_structural_pane(action, expected):
    value = observed_structure(action)
    result = classify(value)
    assert result.state == expected and result.reason == 'exact_record_section'
    assert result.hwnd == 66370 and result.snapshot == value


@pytest.mark.parametrize(('index', 'changes'), [
    (0, {'enabled': False}), (0, {'control_type': 50033}),
    (1, {'offscreen': True}), (1, {'name': '设置'}), (1, {'name': '即时重放'}),
    (1, {'control_type': 50000}), (1, {'control_type': 50026}),
    (2, {'enabled': False}), (3, {'enabled': False}), (4, {'enabled': False}),
    (5, {'enabled': False}), (5, {'offscreen': True}),
    (6, {'enabled': False}), (7, {'enabled': False}), (7, {'offscreen': True}),
    (7, {'process_id': 999}),
])
def test_disabled_interactive_controls_named_modules_and_other_hidden_ancestors_still_block(index, changes):
    assert classify(mutate(observed_structure(), index, **changes)).state == 'unknown'


def test_structural_wrapper_cannot_itself_be_a_disabled_record_scope():
    value = observed_structure()
    window = value.windows[0]
    nodes = (window.nodes[0], window.nodes[1],
             status.OverlayNode(1, '录制', 50020, False, True, IDENTITY.pid),
             status.OverlayNode(1, 'Alt+F9 - 开始', 50020, False, True, IDENTITY.pid))
    value = replace(value, windows=(replace(window, nodes=nodes),))
    assert classify(value).state == 'unknown'


def test_structural_exception_does_not_create_record_section_or_resolve_conflicts():
    value = observed_structure()
    assert classify(mutate(value, 6, name='')).state == 'unknown'
    window = value.windows[0]
    conflicting = (*window.nodes,
        status.OverlayNode(4, '', 50000, False, True, IDENTITY.pid),
        status.OverlayNode(8, '录制', 50020, False, True, IDENTITY.pid),
        status.OverlayNode(8, 'Alt+F9 - 停止', 50020, False, True, IDENTITY.pid))
    value = replace(value, windows=(replace(window, nodes=conflicting),))
    assert classify(value).state == 'unknown'
