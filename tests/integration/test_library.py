import json
import sqlite3
import pytest
from cs2pov.storage.library import Library
from cs2pov.storage.settings import DataError


def test_migrate_v1_preserves_draft_records_and_unrestored_sessions(tmp_path):
    path = tmp_path / "library.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE drafts (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL)")
        db.execute("CREATE TABLE records (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL)")
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        db.execute("INSERT INTO drafts VALUES ('current', '{\"demo\":\"原比赛.dem\"}', 1)")
        db.execute("INSERT INTO records VALUES ('record', '{}', 1)")
        db.execute("INSERT INTO sessions VALUES ('unfinished', 'needs_restore')")
        db.execute("PRAGMA user_version=1")
    library = Library(path)
    assert library.draft()["demo"] == "原比赛.dem"
    assert library.records()[0]["restore_status"] == "unknown"
    assert library.unfinished() == ["unfinished"]
    assert library.db.execute("PRAGMA user_version").fetchone()[0] == 2
    library.close()


def test_draft_transaction_failure_keeps_previous_payload(tmp_path):
    library = Library(tmp_path / "library.sqlite")
    library.save_draft({"demo": "one.dem"})
    library.db.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON drafts BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(sqlite3.DatabaseError):
        library.save_draft({"demo": "two.dem"})
    assert library.draft() == {"demo": "one.dem"}
    library.close()
    reloaded = Library(tmp_path / "library.sqlite")
    assert reloaded.draft() == {"demo": "one.dem"}
    reloaded.close()


def test_failed_migration_rolls_back_version_and_data(tmp_path):
    path = tmp_path / "broken-v1.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT)")
        db.execute("INSERT INTO sessions VALUES ('important', 'needs_restore')")
        db.execute("PRAGMA user_version=1")
    with pytest.raises(sqlite3.DatabaseError):
        Library(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT * FROM sessions").fetchone() == ("important", "needs_restore")


def test_unknown_new_database_is_preserved(tmp_path):
    path = tmp_path / "new-version.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=3")
    with pytest.raises(DataError):
        Library(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
