import hashlib
import json
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from cs2pov.storage.settings import DataError, DataVersionError


def api():
    from cs2pov.storage import local_resources
    return local_resources


@pytest.fixture
def store(tmp_path, monkeypatch):
    module = api()
    hud = tmp_path / 'pov.vpk'; hud.write_bytes(b'approved test hud')
    probe = tmp_path / 'ffprobe.exe'; probe.write_bytes(b'approved test probe, never executed')
    monkeypatch.setattr(module, '_APPROVED', {
        'hud': (hashlib.sha256(hud.read_bytes()).hexdigest(), hud.stat().st_size),
        'probe': (hashlib.sha256(probe.read_bytes()).hexdigest(), probe.stat().st_size),
    })
    @contextmanager
    def source_lease(path):
        # Unit policy tests use synthetic tiny files and a mocked resource
        # lease. Real ancestor/file locking has separate Windows coverage.
        before = path.stat()
        yield SimpleNamespace(bytes=before.st_size)
        after = path.stat()
        assert (before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino)
    monkeypatch.setattr(module, 'read_lease', source_lease)
    return module.LocalResources(tmp_path / 'local-resources.json'), hud, probe


def test_pick_and_save_only_register_verified_absolute_path_references(store):
    resources, hud, probe = store
    before = {path: path.read_bytes() for path in (hud, probe)}
    reference = resources.pick('hud', hud)
    assert not resources.path.exists()
    assert reference.path == str(hud) and reference.sha256 == hashlib.sha256(before[hud]).hexdigest()
    resources.save('hud', reference)
    resources.save('probe', probe)
    raw = json.loads(resources.path.read_text(encoding='utf-8'))
    assert set(raw) == {'schema', 'resources'} and raw['schema'] == 1
    assert set(raw['resources']) == {'hud', 'probe'}
    assert set(raw['resources']['hud']) == {'path', 'sha256', 'bytes'}
    assert resources.get_verified_path('hud') == hud and resources.get_verified_path('probe') == probe
    assert {path: path.read_bytes() for path in (hud, probe)} == before
    assert set(resources.path.parent.iterdir()) == {hud, probe, resources.path}


def test_changed_resource_is_rejected_on_every_use_and_reference_text_is_preserved(store):
    resources, hud, _ = store
    resources.save('hud', hud)
    reference_text = resources.path.read_bytes()
    hud.write_bytes(b'altered test hud!')
    with pytest.raises(DataError): resources.get_verified_path('hud')
    with pytest.raises(DataError): resources.save('hud', hud)
    assert resources.path.read_bytes() == reference_text


def test_future_resource_schema_cannot_be_loaded_or_overwritten(store):
    resources, hud, _ = store
    raw = b'{"schema": 2, "resources": {}}'
    resources.path.write_bytes(raw)
    with pytest.raises(DataVersionError): resources.load()
    with pytest.raises(DataVersionError): resources.save('hud', hud)
    assert resources.path.read_bytes() == raw


def test_actual_approved_hud_matches_existing_review_and_is_only_read(tmp_path):
    module = api()
    from cs2pov.services.telemetry_resource import BASE_SHA256
    source = Path(__file__).resolve().parents[2] / 'resources/private/minimal-pov/pov.vpk'
    before = source.stat()
    resources = module.LocalResources(tmp_path / 'local-resources.json')
    reference = resources.pick('hud', source)
    assert reference.sha256 == BASE_SHA256 == module.HUD_SHA256
    assert reference.bytes == module.HUD_BYTES == 410376
    assert not resources.path.exists()
    after = source.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_real_profile_data_registration_can_save_and_read_actual_audited_hud(tmp_path):
    module = api()
    source = Path(__file__).resolve().parents[2] / 'resources/private/minimal-pov/pov.vpk'
    resources = module.LocalResources(tmp_path / 'local-resources.json')
    saved = resources.save('hud', source)
    assert resources.load() == {'hud': saved}
    assert resources.get_verified_path('hud') == source


def test_real_data_lease_supports_profile_path_and_blocks_file_writes_and_replacement(tmp_path):
    from cs2pov.adapters.video import read_data_lease
    data = tmp_path / 'ordinary.json'; data.write_text('{"schema":1}', encoding='utf-8')
    replacement = tmp_path / 'replacement.json'; replacement.write_text('{}', encoding='utf-8')
    with read_data_lease(data) as signature:
        assert signature.identity is not None and signature.bytes == data.stat().st_size
        assert json.loads(data.read_text(encoding='utf-8')) == {'schema': 1}
        with pytest.raises(OSError): data.write_bytes(b'changed')
        with pytest.raises(OSError): replacement.replace(data)
    assert json.loads(data.read_text(encoding='utf-8')) == {'schema': 1}


def test_probe_approval_is_exactly_the_existing_fixed_tool_policy():
    module = api()
    from cs2pov.adapters.probe_tool import PROBE_BYTES, PROBE_SHA256
    assert module._APPROVED['probe'] == (PROBE_SHA256, PROBE_BYTES)


@pytest.mark.parametrize('role', ['video', '', None, True, []])
def test_unknown_roles_are_rejected_before_storage_write(store, role):
    resources, hud, _ = store
    with pytest.raises(DataError): resources.pick(role, hud)
    with pytest.raises(DataError): resources.save(role, hud)
    with pytest.raises(DataError): resources.get_verified_path(role)
    assert not resources.path.exists()


def test_missing_role_never_guesses_source_tree_or_searches_path(store):
    resources, _, _ = store
    assert resources.load() == {}
    with pytest.raises(DataError, match='登记'): resources.get_verified_path('hud')
    with pytest.raises(DataError, match='登记'): resources.get_verified_path('probe')


@pytest.mark.parametrize('path', ['', 'relative/pov.vpk', r'\\server\share\pov.vpk',
                                  'C:/folder/../pov.vpk', 'C:/folder/pov.vpk:stream'])
def test_ambiguous_resource_paths_are_rejected(store, path):
    resources, _, _ = store
    with pytest.raises(DataError): resources.pick('hud', path)
    assert not resources.path.exists()


@pytest.mark.parametrize('bad', ['directory', 'missing', 'wrong_suffix', 'wrong_probe_name'])
def test_resource_must_be_a_named_ordinary_file_for_its_role(store, bad):
    resources, hud, probe = store
    if bad == 'directory': role, path = 'hud', hud.parent
    elif bad == 'missing': role, path = 'hud', hud.parent / 'missing.vpk'
    elif bad == 'wrong_suffix': role, path = 'hud', probe
    else:
        role, path = 'probe', probe.with_name('other.exe')
        path.write_bytes(probe.read_bytes())
    with pytest.raises(DataError): resources.pick(role, path)
    assert not resources.path.exists()


@pytest.mark.parametrize('raw', [b'{bad', b'[]', b'{"schema":1,"resources":{},"extra":true}',
    b'{"schema":true,"resources":{}}', b'{"schema":0,"resources":{}}',
    b'{"schema":1,"resources":{"unknown":{}}}', b'{"schema":1,"resources":[]}',
    b'{"schema":1,"schema":1,"resources":{}}', b'{"schema":1,"resources":{},"resources":{}}',
    b'{"schema":1,"resources":{"hud":{"path":"C:/pov.vpk","sha256":"bad","bytes":NaN}}}',
    b'x' * 32769], ids=['malformed', 'list', 'unknown_field', 'bool_schema', 'old_schema',
                      'unknown_role', 'list_resources', 'duplicate_schema', 'duplicate_resources',
                      'nan', 'oversized'])
def test_corrupt_resource_registration_is_never_reset_or_overwritten(store, raw):
    resources, hud, _ = store
    resources.path.write_bytes(raw)
    with pytest.raises(DataError): resources.load()
    with pytest.raises(DataError): resources.save('hud', hud)
    assert resources.path.read_bytes() == raw


@pytest.mark.parametrize('change', [{'sha256': 'c' * 64}, {'sha256': 'G' * 64},
    {'sha256': 'A' * 64}, {'bytes': True}, {'bytes': -1}, {'bytes': 1.5},
    {'path': 'relative'}, {'unreviewed': True}, {'role': 'probe'}])
def test_resource_reference_schema_cannot_claim_an_unknown_hash_or_approval(store, change):
    resources, hud, _ = store
    resources.save('hud', hud)
    raw = json.loads(resources.path.read_text(encoding='utf-8'))
    raw['resources']['hud'].update(change)
    encoded = json.dumps(raw).encode('utf-8'); resources.path.write_bytes(encoded)
    with pytest.raises(DataError): resources.load()
    with pytest.raises(DataError): resources.get_verified_path('hud')
    assert resources.path.read_bytes() == encoded


def test_resource_hash_is_rechecked_when_saved_reference_is_reused(store):
    resources, hud, _ = store
    picked = resources.pick('hud', hud)
    hud.write_bytes(b'wrong content, identical bytes length'[:picked.bytes])
    with pytest.raises(DataError): resources.save('hud', picked)
    assert not resources.path.exists()


def test_resource_reference_survives_restart_without_bundling_source_data(store):
    resources, hud, _ = store
    saved = resources.save('hud', hud)
    reloaded = api().LocalResources(resources.path)
    assert reloaded.load() == {'hud': saved}
    assert reloaded.get_verified_path('hud') == hud
    hud.unlink()
    with pytest.raises(DataError): reloaded.get_verified_path('hud')
    assert reloaded.load() == {'hud': saved}


def test_failed_atomic_registration_save_keeps_previous_json_and_source_file(store, monkeypatch):
    resources, hud, probe = store
    resources.save('hud', hud)
    before = resources.path.read_bytes()
    def fail(*_args): raise OSError('disk full')
    monkeypatch.setattr(api(), 'atomic_write', fail)
    with pytest.raises(DataError): resources.save('probe', probe)
    assert resources.path.read_bytes() == before
    assert resources.get_verified_path('hud') == hud


def test_reparse_marked_resource_is_rejected_even_when_hash_and_file_type_match(store, monkeypatch):
    from types import SimpleNamespace
    resources, hud, _ = store
    real = Path.lstat
    def redirected(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if path == hud:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', redirected)
    with pytest.raises(DataError): resources.pick('hud', hud)
    assert not resources.path.exists()


def test_reparse_marked_config_parent_is_rejected_without_writing(store, monkeypatch):
    from types import SimpleNamespace
    resources, hud, _ = store
    real = Path.lstat
    def redirected(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if path == resources.path.parent:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', redirected)
    with pytest.raises(DataError): resources.load()
    with pytest.raises(DataError): resources.save('hud', hud)
    assert not resources.path.exists()


@pytest.mark.parametrize('replacement', ['future', 'different_reference'])
def test_registration_change_during_resource_review_is_not_overwritten(store, monkeypatch, replacement):
    resources, hud, probe = store
    resources.save('probe', probe)
    original_lease = api().read_lease
    changed = json.dumps({'schema': 2, 'resources': {}}).encode() if replacement == 'future' else b'{"schema":1,"resources":{}}'
    @contextmanager
    def changed_registration(path):
        with original_lease(path) as signature:
            resources.path.write_bytes(changed)
            yield signature
    monkeypatch.setattr(api(), 'read_lease', changed_registration)
    with pytest.raises(DataError): resources.save('hud', hud)
    assert resources.path.read_bytes() == changed


def test_read_data_lease_rejects_existing_writer_and_still_leaves_data_untouched(tmp_path):
    from cs2pov.adapters.video import read_data_lease, VideoError
    data = tmp_path / 'ordinary.json'; data.write_bytes(b'{"schema":1}')
    with data.open('ab'):
        with pytest.raises(VideoError):
            with read_data_lease(data):
                pytest.fail('a writable file must not authorize a stable snapshot')
    assert data.read_bytes() == b'{"schema":1}'


def test_data_lease_failure_never_falls_back_to_an_unlocked_registry_read(store, monkeypatch):
    from cs2pov.adapters.video import VideoError
    resources, hud, _ = store
    resources.save('hud', hud)
    original = resources.path.read_bytes()
    @contextmanager
    def inaccessible(_path):
        raise VideoError('cannot identify or lock ordinary data file')
        yield
    monkeypatch.setattr(api(), 'read_data_lease', inaccessible)
    with pytest.raises(DataError): resources.load()
    with pytest.raises(DataError): resources.get_verified_path('hud')
    with pytest.raises(DataError): resources.save('hud', hud)
    assert resources.path.read_bytes() == original


@pytest.mark.parametrize('path', ['', 'local-resources.json', '//server/share/local-resources.json',
                                  'C:/test/../local-resources.json', 'C:/test/local-resources.json:stream'])
def test_registry_location_requires_an_unambiguous_absolute_local_path(path):
    with pytest.raises(DataError): api().LocalResources(path)


def test_registry_cannot_overwrite_settings_or_a_directory(tmp_path):
    with pytest.raises(DataError): api().LocalResources(tmp_path / 'settings.json')
    location = tmp_path / 'local-resources.json'; location.mkdir()
    with pytest.raises(DataError): api().LocalResources(location).load()
    assert location.is_dir()
