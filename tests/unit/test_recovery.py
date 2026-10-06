import json
import stat
import pytest
from cs2pov.storage.transaction import (FileTransaction, RecoveryError, attributes,
                                      pending_sessions, set_attributes)


class Crash(BaseException):
    pass


def fixture(tmp_path, checkpoint=lambda _: None, guard=lambda: True):
    root = tmp_path / "game"
    root.mkdir(exist_ok=True)
    original = root / "gameinfo.gi"
    if not original.exists():
        original.write_bytes(b"original config")
    targets = {"gameinfo": original, "pov": root / "pov.vpk"}
    session = tmp_path / "sessions" / "one"
    transaction = FileTransaction(session, root, targets, can_modify=guard, checkpoint=checkpoint)
    return transaction, targets


def prepared(tmp_path, **kwargs):
    transaction, targets = fixture(tmp_path, **kwargs)
    transaction.prepare({"gameinfo": b"modified config", "pov": b"our pov"})
    return transaction, targets


def reload(transaction):
    restored = FileTransaction(transaction.session, transaction.root, transaction.targets, can_modify=lambda: True)
    restored.load()
    return restored


def test_existing_and_absent_restore_is_idempotent(tmp_path):
    t, targets = prepared(tmp_path)
    t.deploy()
    assert targets["gameinfo"].read_bytes() == b"modified config"
    assert targets["pov"].read_bytes() == b"our pov"
    assert pending_sessions(t.session.parent) == [t.session]
    r = reload(t)
    assert r.restore() == {"gameinfo": "restored", "pov": "restored"}
    assert r.restore() == {"gameinfo": "restored", "pov": "restored"}
    assert targets["gameinfo"].read_bytes() == b"original config"
    assert not targets["pov"].exists()
    assert pending_sessions(t.session.parent) == []


def test_existing_pov_is_backed_up_and_restored(tmp_path):
    t, targets = fixture(tmp_path)
    targets["pov"].write_bytes(b"user pov")
    t.prepare({"gameinfo": b"new", "pov": b"our pov"})
    t.deploy()
    reload(t).restore()
    assert targets["pov"].read_bytes() == b"user pov"


@pytest.mark.parametrize("point", [
    "intent:gameinfo", "backup:gameinfo", "intent:pov", "backup:pov", "prepared",
    "before_deploy:gameinfo", "after_deploy:gameinfo", "applied:gameinfo",
    "before_deploy:pov", "after_deploy:pov", "applied:pov",
    "before_restore:gameinfo", "after_restore:gameinfo", "restored:gameinfo",
    "before_restore:pov", "after_restore:pov", "restored:pov", "restore_finished",
])
def test_crash_at_each_durable_boundary_recovers_originals(tmp_path, point):
    def crash(checkpoint):
        if checkpoint == point:
            raise Crash(checkpoint)
    t, targets = fixture(tmp_path, checkpoint=crash)
    with pytest.raises(Crash):
        t.prepare({"gameinfo": b"modified config", "pov": b"our pov"})
        t.deploy()
        t.restore()
    results = reload(t).restore()
    assert all(value == "restored" for value in results.values())
    assert targets["gameinfo"].read_bytes() == b"original config"
    assert not targets["pov"].exists()


def test_external_content_preserved_but_other_file_restores(tmp_path):
    t, targets = prepared(tmp_path)
    t.deploy()
    targets["pov"].write_bytes(b"externally updated")
    r = reload(t)
    result = r.restore()
    assert result["gameinfo"] == "restored"
    assert result["pov"] != "restored"
    assert targets["pov"].read_bytes() == b"externally updated"
    assert not r.data["complete"]
    assert r.restore()["pov"] != "restored"
    assert pending_sessions(t.session.parent) == [t.session]


@pytest.mark.parametrize('change', ['content', 'attributes', 'recreated_absent'])
def test_completion_rechecks_all_files_after_last_restore_without_overwriting_external_changes(tmp_path, change):
    t, targets = prepared(tmp_path)
    t.deploy()
    def change_after_last_restore(point):
        if point != 'restored:pov':
            return
        if change == 'content':
            targets['gameinfo'].write_bytes(b'new external setting after restoration')
        elif change == 'attributes':
            set_attributes(targets['gameinfo'], attributes(targets['gameinfo']) | stat.FILE_ATTRIBUTE_READONLY)
        else:
            targets['pov'].write_bytes(b'external new file')
    t.checkpoint = change_after_last_restore
    try:
        results = t.restore()
        key = 'pov' if change == 'recreated_absent' else 'gameinfo'
        assert results[key] != 'restored'
        assert t.data['entries'][key]['state'] == 'conflict'
        assert t.data['complete'] is False
        assert pending_sessions(t.session.parent) == [t.session]
        if change == 'content':
            assert targets['gameinfo'].read_bytes() == b'new external setting after restoration'
            assert (t.session/'gameinfo.backup').read_bytes() == b'original config'
        elif change == 'attributes':
            assert attributes(targets['gameinfo']) & stat.FILE_ATTRIBUTE_READONLY
        else:
            assert targets['pov'].read_bytes() == b'external new file'
    finally:
        set_attributes(targets['gameinfo'], attributes(targets['gameinfo']) & ~stat.FILE_ATTRIBUTE_READONLY)


def test_corrupt_backup_does_not_overwrite_game(tmp_path):
    t, targets = prepared(tmp_path)
    t.deploy()
    (t.session / "gameinfo.backup").write_bytes(b"corrupt")
    r = reload(t)
    assert r.restore()["gameinfo"] != "restored"
    assert targets["gameinfo"].read_bytes() == b"modified config"
    assert not targets["pov"].exists()


@pytest.mark.parametrize("payload", [b"{", b"[]", b'{"version":1}', b'null'])
def test_corrupt_journal_blocks(tmp_path, payload):
    t, targets = prepared(tmp_path)
    (t.session / "journal.json").write_bytes(payload)
    with pytest.raises(RecoveryError):
        reload(t)
    assert pending_sessions(t.session.parent) == [t.session]
    assert targets["gameinfo"].read_bytes() == b"original config"


def test_external_update_before_deploy_blocks(tmp_path):
    t, targets = prepared(tmp_path)
    targets["gameinfo"].write_bytes(b"Steam update")
    with pytest.raises(RecoveryError):
        t.deploy()
    assert targets["gameinfo"].read_bytes() == b"Steam update"
    assert not targets["pov"].exists()


def test_live_guard_blocks_deploy_and_recovery(tmp_path):
    closed = True
    t, targets = prepared(tmp_path, guard=lambda: closed)
    closed = False
    with pytest.raises(RecoveryError):
        t.deploy()
    assert targets["gameinfo"].read_bytes() == b"original config"
    closed = True
    t.deploy()
    closed = False
    assert all(value != "restored" for value in t.restore().values())
    assert targets["gameinfo"].read_bytes() == b"modified config"
    closed = True
    assert all(value == "restored" for value in t.restore().values())


def test_readonly_attributes_preserved(tmp_path):
    t, targets = fixture(tmp_path)
    original_attrs = attributes(targets["gameinfo"]) | stat.FILE_ATTRIBUTE_READONLY
    set_attributes(targets["gameinfo"], original_attrs)
    try:
        t.prepare({"gameinfo": b"modified config", "pov": b"our pov"})
        t.deploy()
        assert attributes(targets["gameinfo"]) == original_attrs
        reload(t).restore()
        assert targets["gameinfo"].read_bytes() == b"original config"
        assert attributes(targets["gameinfo"]) == original_attrs
    finally:
        set_attributes(targets["gameinfo"], original_attrs & ~stat.FILE_ATTRIBUTE_READONLY)


def test_whitelist_escape_and_duplicate_rejected(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    for targets in ({"escape": tmp_path / "other"}, {"a": root / "a", "b": root / "a"}, {"../key": root / "a"}):
        with pytest.raises(RecoveryError):
            FileTransaction(tmp_path / "session", root, targets, can_modify=lambda: True)


def test_junction_redirect_after_prepare_blocks(tmp_path):
    import subprocess
    t, targets = prepared(tmp_path)
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    (elsewhere / "gameinfo.gi").write_bytes(b"outside")
    saved = tmp_path / "saved-game"
    t.root.rename(saved)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(t.root), str(elsewhere)], check=True, capture_output=True)
    try:
        with pytest.raises(RecoveryError):
            t.deploy()
        assert (elsewhere / "gameinfo.gi").read_bytes() == b"outside"
    finally:
        t.root.rmdir()  # Remove only the junction itself.
        saved.rename(t.root)


def test_permission_and_disk_full_failure_can_restore(tmp_path, monkeypatch):
    from cs2pov.storage import transaction as module
    t, targets = prepared(tmp_path)
    actual = module.atomic_write
    def fail(path, data):
        if path == targets["pov"]:
            raise OSError(112, "disk full / denied")
        actual(path, data)
    monkeypatch.setattr(module, "atomic_write", fail)
    with pytest.raises(OSError):
        t.deploy()
    assert targets["gameinfo"].read_bytes() == b"modified config"
    assert not targets["pov"].exists()
    assert all(value == "restored" for value in reload(t).restore().values())


def test_unknown_journal_target_cannot_write_arbitrary_file(tmp_path):
    t, targets = prepared(tmp_path)
    data = json.loads((t.session / "journal.json").read_bytes())
    data["entries"]["arbitrary"] = data["entries"]["gameinfo"]
    (t.session / "journal.json").write_text(json.dumps(data))
    with pytest.raises(RecoveryError):
        reload(t)
    assert targets["gameinfo"].read_bytes() == b"original config"


def test_missing_journal_blocks_new_tasks(tmp_path):
    sessions = tmp_path / "sessions"
    orphan = sessions / "orphan"
    orphan.mkdir(parents=True)
    assert pending_sessions(sessions) == [orphan]


def test_pending_transaction_rejects_new_preparation(tmp_path):
    t, targets = prepared(tmp_path)
    another = FileTransaction(t.session.parent / "two", t.root, targets, can_modify=lambda: True)
    with pytest.raises(RecoveryError):
        another.prepare({"gameinfo": b"new", "pov": b"new"})
    assert not another.session.exists()


def test_matching_external_file_without_write_intent_is_never_removed(tmp_path):
    t, targets = prepared(tmp_path)
    targets["pov"].write_bytes(b"our pov")  # externally created, never deployed by us
    r = reload(t)
    assert r.restore()["pov"] != "restored"
    assert r.restore()["pov"] != "restored"
    assert targets["pov"].read_bytes() == b"our pov"


def test_backup_space_query_failure_blocks_before_creating_session(tmp_path, monkeypatch):
    from cs2pov.storage import transaction as module
    t, targets = fixture(tmp_path)
    def fail(*args):
        raise OSError("cannot query disk")
    monkeypatch.setattr(module, "check_directory", fail)
    with pytest.raises(OSError):
        t.prepare({"gameinfo": b"new", "pov": b"new"})
    assert not t.session.exists()
    assert targets["gameinfo"].read_bytes() == b"original config"


def test_process_query_failure_blocks_changes(monkeypatch):
    from cs2pov.adapters import processes
    monkeypatch.setattr(processes, "process_names", lambda: {1: "CS2.EXE"})
    assert not processes.cs2_closed()
    def fail():
        raise processes.ProcessError("unknown")
    monkeypatch.setattr(processes, "process_names", fail)
    assert not processes.cs2_closed()
    monkeypatch.setattr(processes, "process_names", lambda: {1: "steam.exe"})
    assert processes.cs2_closed()


@pytest.mark.parametrize("point", ["after_deploy:gameinfo", "after_protect:gameinfo", "applied:gameinfo"])
def test_temporary_readonly_protection_recovers_after_crash(tmp_path, point):
    def crash(checkpoint):
        if checkpoint == point:
            raise Crash(point)
    t, targets = fixture(tmp_path, checkpoint=crash)
    original_attrs = attributes(targets["gameinfo"])
    t.prepare({"gameinfo": b"original config", "pov": b"our pov"}, protect_readonly=frozenset({"gameinfo"}))
    with pytest.raises(Crash):
        t.deploy()
    assert all(result == "restored" for result in reload(t).restore().values())
    assert targets["gameinfo"].read_bytes() == b"original config"
    assert attributes(targets["gameinfo"]) == original_attrs


def test_readonly_protection_restores_original_attributes(tmp_path):
    t, targets = fixture(tmp_path)
    original_attrs = attributes(targets["gameinfo"])
    t.prepare({"gameinfo": b"original config", "pov": b"our pov"}, protect_readonly=frozenset({"gameinfo"}))
    t.deploy()
    assert attributes(targets["gameinfo"]) & stat.FILE_ATTRIBUTE_READONLY
    reload(t).restore()
    assert attributes(targets["gameinfo"]) == original_attrs


def test_protection_cannot_alter_content_or_protect_absent_file(tmp_path):
    t, _ = fixture(tmp_path)
    with pytest.raises(RecoveryError):
        t.prepare({"gameinfo": b"changed", "pov": b"new"}, protect_readonly=frozenset({"gameinfo"}))
    with pytest.raises(RecoveryError):
        t.prepare({"gameinfo": b"original config", "pov": b"new"}, protect_readonly=frozenset({"pov"}))
    assert not t.session.exists()
