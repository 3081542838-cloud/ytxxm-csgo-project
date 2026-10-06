from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from cs2pov.services import portable
from cs2pov.storage.library import Library
from cs2pov.storage.settings import HudPreset, JsonFile, Presets, Settings


def marker(root, raw=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'portable.json').write_text(json.dumps(
        {'schema': 1, 'application': 'XiamiPOV'} if raw is None else raw), encoding='utf-8')
    return root / '虾米pov.exe'


def old_profile(directory):
    directory.mkdir()
    settings = Settings(installation=str(directory.parent / 'game'),
                        cfg=str(directory.parent / 'cfg'), video_directory=str(directory.parent / 'videos'),
                        hotkey='Ctrl+F8', console_key='Slash',
                        nvidia_path_confirmed=True, output_verified=True)
    JsonFile(directory / 'settings.json', Settings.decode).save(asdict(settings))
    presets = Presets(directory)
    presets.save(replace(HudPreset(), id='personal', name='自己的', hud_scale=.75))
    presets.set_default('personal')
    return settings


def test_portable_marker_frozen_only_and_cli_override(tmp_path):
    exe = marker(tmp_path / 'portable')
    assert portable.portable_root(exe, True) == exe.parent
    assert portable.portable_root(exe, False) is None
    assert portable.data_directory(executable=exe, frozen=True) == exe.parent / 'data'
    assert portable.data_directory(tmp_path / 'explicit', executable=exe, frozen=True) == tmp_path / 'explicit'
    assert portable.data_directory(executable=exe, frozen=False,
        environ={'LOCALAPPDATA': str(tmp_path / 'local')}) == tmp_path / 'local' / 'CS2POVHelper'
    (exe.parent / 'portable.json').unlink()
    assert portable.data_directory(executable=exe, frozen=True,
        environ={'LOCALAPPDATA': str(tmp_path / 'local')}) == tmp_path / 'local' / 'CS2POVHelper'


@pytest.mark.parametrize('raw', [
    {'schema': True, 'application': 'XiamiPOV'},
    {'schema': 2, 'application': 'XiamiPOV'},
    {'schema': 1, 'application': 'Other'},
    {'schema': 1, 'application': 'XiamiPOV', 'data': '../other'},
])
def test_invalid_marker_does_not_fallback(tmp_path, raw):
    exe = marker(tmp_path, raw)
    with pytest.raises(portable.PortableError):
        portable.portable_root(exe, True)


def test_marker_duplicate_and_oversized_are_rejected(tmp_path):
    exe = marker(tmp_path)
    path = tmp_path / 'portable.json'
    path.write_text('{"schema":1,"schema":1,"application":"XiamiPOV"}')
    with pytest.raises(portable.PortableError):
        portable.portable_root(exe, True)
    path.write_bytes(b' ' * 32769)
    with pytest.raises(portable.PortableError):
        portable.portable_root(exe, True)


def test_initialization_visible_generic_and_preserves_existing_files(tmp_path):
    data = tmp_path / 'data'
    portable.initialize_profile(data)
    assert JsonFile(data / 'settings.json', Settings.decode).load(None) == Settings()
    assert Presets(data).items == [HudPreset()]
    assert not (data / 'local-resources.json').exists()
    custom = Settings(hotkey='Ctrl+F8', console_key='Slash')
    JsonFile(data / 'settings.json', Settings.decode).save(asdict(custom))
    before = {p.name: p.read_bytes() for p in data.iterdir()}
    portable.initialize_profile(data)
    assert {p.name: p.read_bytes() for p in data.iterdir()} == before
    # A missing primary with a saved backup belongs to the existing profile.
    (data / 'settings.json').unlink()
    backup = (data / 'settings.json.bak').read_bytes()
    portable.initialize_profile(data)
    assert not (data / 'settings.json').exists()
    assert (data / 'settings.json.bak').read_bytes() == backup


def test_readonly_portable_has_clear_error_and_no_fallback(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    real_open = Path.open

    def denied(path, *args, **kwargs):
        if path.name.startswith('.portable-write-'):
            raise PermissionError('denied')
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(portable.PortableError, match='无法写入.*不会改用其他'):
        portable.initialize_profile(data)
    assert not list(data.iterdir())


def test_explicit_migration_preserves_choices_only_and_source(tmp_path):
    source = tmp_path / 'old'
    settings = old_profile(source)
    (source / 'local-resources.json').write_text('private paths are deliberately not read')
    (source / 'logs').mkdir()
    library = Library(source / 'library.sqlite')
    library.close()
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    target = tmp_path / 'new'
    assert portable.migrate_profile(source, target) == target
    copied = JsonFile(target / 'settings.json', Settings.decode).load(None)
    assert copied == replace(settings, nvidia_path_confirmed=False, output_verified=False)
    assert Presets(target).default == 'personal'
    assert next(p for p in Presets(target).items if p.id == 'personal').hud_scale == .75
    assert {p.name for p in target.iterdir()} == {
        'settings.json', 'settings.json.bak', 'hud-presets.json', 'hud-presets.json.bak'}
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == before


@pytest.mark.parametrize('block', ['lock', 'journal', 'database', 'recording', 'invalid_database'])
def test_migration_rejects_unfinished_or_unreadable_state_before_writes(tmp_path, block):
    source = tmp_path / 'old'
    old_profile(source)
    if block == 'lock':
        (source / 'app.lock').write_text('still open')
    elif block == 'journal':
        (source / 'sessions' / 'pending').mkdir(parents=True)
    elif block == 'database':
        library = Library(source / 'library.sqlite')
        with library.db:
            library.db.execute("INSERT INTO sessions VALUES ('unfinished', 'recording')")
        library.close()
    elif block == 'recording':
        session = source / 'sessions' / 'done'
        session.mkdir(parents=True)
        # A complete file journal does not dismiss unknown NVIDIA state.
        entry = {'original': None, 'deployed': 'a' * 64, 'attributes': None,
                 'state': 'restored', 'write_intent': False, 'protect_readonly': False}
        (session / 'journal.json').write_text(json.dumps(
            {'version': 1, 'prepared': True, 'complete': True, 'entries': {'one': entry}}))
        (session / 'recording-session.json').write_text('{broken')
    else:
        (source / 'library.sqlite').write_bytes(b'invalid sqlite')
    target = tmp_path / 'new'
    with pytest.raises(portable.PortableError):
        portable.migrate_profile(source, target)
    assert not target.exists()


@pytest.mark.parametrize('bad', ['settings', 'presets', 'duplicate'])
def test_migration_prevalidates_both_files_and_does_not_create_partial_profile(tmp_path, bad):
    source = tmp_path / 'old'
    old_profile(source)
    if bad == 'settings':
        (source / 'settings.json').write_text('{"schema":99}')
    elif bad == 'presets':
        (source / 'hud-presets.json').write_text('{broken')
    else:
        (source / 'settings.json').write_text('{"schema":2,"schema":2}')
    target = tmp_path / 'new'
    with pytest.raises(portable.PortableError):
        portable.migrate_profile(source, target)
    assert not target.exists()


def test_migration_refuses_nonempty_and_nested_targets(tmp_path):
    source = tmp_path / 'old'
    old_profile(source)
    target = tmp_path / 'new'
    target.mkdir()
    (target / 'settings.json').write_bytes(b'keep')
    with pytest.raises(portable.PortableError):
        portable.migrate_profile(source, target)
    assert (target / 'settings.json').read_bytes() == b'keep'
    with pytest.raises(portable.PortableError):
        portable.migrate_profile(source, source / 'nested')


def test_migration_concurrent_target_writer_is_preserved(tmp_path, monkeypatch):
    source = tmp_path / 'old'
    old_profile(source)
    target = tmp_path / 'new'
    real_save = JsonFile.save

    def concurrent_save(file, raw):
        real_save(file, raw)
        if file.path.name == 'hud-presets.json':
            target.mkdir()
            (target / 'settings.json').write_bytes(b'concurrent data')

    monkeypatch.setattr(JsonFile, 'save', concurrent_save)
    with pytest.raises(portable.PortableError, match='发生变化'):
        portable.migrate_profile(source, target)
    assert (target / 'settings.json').read_bytes() == b'concurrent data'
    assert not list(tmp_path.glob('.new.migration-*'))


def test_migration_failed_second_file_leaves_no_partial_target(tmp_path, monkeypatch):
    source = tmp_path / 'old'
    old_profile(source)
    real_save = JsonFile.save

    def disk_full(file, raw):
        if file.path.name == 'hud-presets.json':
            raise OSError('disk full')
        return real_save(file, raw)

    monkeypatch.setattr(JsonFile, 'save', disk_full)
    target = tmp_path / 'new'
    with pytest.raises(portable.PortableError, match='无法写入'):
        portable.migrate_profile(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob('.new.migration-*'))


@pytest.mark.parametrize('explicit', [False, True])
def test_entry_migration_rejects_nonportable_or_explicit_data_override(tmp_path, monkeypatch, explicit):
    from cs2pov import app
    critical = []
    monkeypatch.setattr(app, 'QApplication', lambda _args: SimpleNamespace(
        setApplicationName=lambda _name: None))
    monkeypatch.setattr(app.QMessageBox, 'critical', lambda *args: critical.append(args[2]))
    monkeypatch.setattr(portable, 'portable_root', lambda *args, **kwargs: None)
    monkeypatch.setattr(portable, 'data_directory', lambda *args, **kwargs: tmp_path / 'new')
    monkeypatch.setattr(app.sys, 'argv', ['app', '--migrate-from', str(tmp_path / 'old')]
                        + (['--data-dir', str(tmp_path / 'explicit')] if explicit else []))
    assert app.main() == 1
    assert '设置迁移只用于' in critical[0]
    assert not (tmp_path / 'new').exists()


def test_portable_entry_opens_main_window_without_network_or_probe(tmp_path, monkeypatch):
    from cs2pov import app
    from cs2pov.ui import resource_setup
    shown = []
    monkeypatch.setattr(app, 'QApplication', lambda _args: SimpleNamespace(
        setApplicationName=lambda _name: None, exec=lambda: 0))
    monkeypatch.setattr(portable, 'portable_root', lambda: tmp_path)
    monkeypatch.setattr(portable, 'data_directory', lambda _arg: tmp_path / 'data')
    def forbidden(*args, **kwargs):
        raise AssertionError('Startup must not require network resource setup')
    monkeypatch.setattr(resource_setup, 'ensure_resources', forbidden)
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(urllib.request, 'build_opener', forbidden)
    monkeypatch.setattr(app, 'Workspace', lambda *args, **kwargs: SimpleNamespace(
        library=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(app, 'MainWindow', lambda model: SimpleNamespace(show=lambda: shown.append(model)))
    monkeypatch.setattr(app.QMessageBox, 'critical', forbidden)
    monkeypatch.setattr(app.sys, 'argv', ['app'])
    assert app.main() == 0
    assert len(shown) == 1
    assert (tmp_path / 'data/settings.json').is_file()
    assert not (tmp_path / 'tools/ffprobe.exe').exists()
