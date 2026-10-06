import hashlib
from pathlib import Path
import sys

import pytest

from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError
from test_local_resources import store


def portable_workspace(tmp_path, monkeypatch, store):
    registry, hud, probe = store
    root = tmp_path / 'portable'
    (root / 'resources').mkdir(parents=True)
    (root / 'tools').mkdir()
    (root / 'resources/pov.vpk').write_bytes(hud.read_bytes())
    (root / 'tools/ffprobe.exe').write_bytes(probe.read_bytes())
    from cs2pov.services import portable
    monkeypatch.setattr(portable, 'portable_root', lambda: root)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    workspace = Workspace(root / 'data')
    return workspace, root


def test_frozen_portable_automatically_finds_both_verified_resources(qapp, tmp_path, monkeypatch, store):
    workspace, root = portable_workspace(tmp_path, monkeypatch, store)
    try:
        assert workspace.local_resource_path('hud') == root / 'resources/pov.vpk'
        assert workspace.local_resource_path('probe') == root / 'tools/ffprobe.exe'
        assert workspace.verified_local_resource('hud') == root / 'resources/pov.vpk'
        assert workspace.verified_local_resource('probe') == root / 'tools/ffprobe.exe'
        assert not (root / 'data/local-resources.json').exists()
    finally:
        workspace.library.close()


def test_portable_resource_tamper_blocks_use(qapp, tmp_path, monkeypatch, store):
    workspace, root = portable_workspace(tmp_path, monkeypatch, store)
    try:
        (root / 'resources/pov.vpk').write_bytes(b'modified')
        with pytest.raises(DataError):
            workspace.verified_local_resource('hud')
    finally:
        workspace.library.close()


def test_explicit_resource_choice_still_overrides_bundled_default(qapp, tmp_path, monkeypatch, store):
    workspace, root = portable_workspace(tmp_path, monkeypatch, store)
    _, hud, _ = store
    try:
        workspace.register_local_resource('hud', hud)
        assert workspace.verified_local_resource('hud') == hud
        assert (root / 'data/local-resources.json').exists()
    finally:
        workspace.library.close()


def test_portable_hud_is_usable_without_optional_probe(qapp, tmp_path, monkeypatch, store):
    workspace, root = portable_workspace(tmp_path, monkeypatch, store)
    try:
        (root / 'tools/ffprobe.exe').unlink()
        assert workspace.verified_local_resource('hud') == root / 'resources/pov.vpk'
        assert workspace.local_resource_path('probe') is None
        with pytest.raises(DataError, match='ffprobe'):
            workspace.verified_local_resource('probe')
    finally:
        workspace.library.close()
