from types import SimpleNamespace
import pytest
from cs2pov.adapters.nvidia_status import _canonical_windows, NvidiaStatusError
from cs2pov.adapters.owned_process import ProcessIdentity


def refs():
    identity = ProcessIdentity(101, 1001, r'C:\NVIDIA\NVIDIA Overlay.exe')
    return [SimpleNamespace(hwnd=1, identity=identity), SimpleNamespace(hwnd=2, identity=identity)]


def test_alias_is_removed_only_when_it_is_the_same_enumerated_native_root():
    values = refs()
    result = _canonical_windows(values, root_identity=lambda hwnd: (1, 101),
                                window_pid=lambda hwnd: 101, window_visible=lambda hwnd: True)
    assert result == [values[0]]


def test_two_independent_overlay_windows_remain_independent():
    values = refs()
    result = _canonical_windows(values, root_identity=lambda hwnd: (hwnd, 101),
                                window_pid=lambda hwnd: 101, window_visible=lambda hwnd: True)
    assert result == values


@pytest.mark.parametrize('failure', ['foreign_root', 'wrong_pid', 'invisible', 'changing_root', 'foreign_identity'])
def test_unverified_alias_does_not_produce_idle(failure):
    values = refs()
    root = lambda hwnd: (1, 101)
    pid = lambda hwnd: 101
    visible = lambda hwnd: True
    if failure == 'foreign_root': root = lambda hwnd: (3, 101)
    elif failure == 'wrong_pid': pid = lambda hwnd: 999
    elif failure == 'invisible': visible = lambda hwnd: False
    elif failure == 'changing_root': root = lambda hwnd: (1 if hwnd == 2 else 2, 101)
    else: values[1].identity = ProcessIdentity(102, 1002, r'C:\NVIDIA\NVIDIA Overlay.exe')
    with pytest.raises(NvidiaStatusError):
        _canonical_windows(values, root_identity=root, window_pid=pid, window_visible=visible)
