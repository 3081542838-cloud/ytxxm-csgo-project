import json

import pytest

from cs2pov.storage import library as storage
from cs2pov.storage.library import Library
from cs2pov.storage.settings import DataError


@pytest.fixture
def library(tmp_path):
    result = Library(tmp_path / "library.sqlite")
    yield result
    result.close()


def raw_record(library, raw):
    with library.db:
        library.db.execute("INSERT INTO records VALUES (?, ?, ?, ?)", ("bad", raw, 1.0, "unknown"))
    return library.record("bad")


@pytest.mark.parametrize("raw", [
    "{", "[]", "null", "1", '"text"',
    '{"duration": NaN}', '{"duration": Infinity}', '{"duration": 1e309}',
    '{"duration": -1}', '{"duration": 0}', '{"duration": true}',
    '{"duration": "4.5"}', '{"width": 0}', '{"width": 1.5}',
    '{"height": false}', '{"video_streams": 0}', '{"audio_streams": -1}',
    '{"player": []}', '{"map": {}}', '{"video_result": 1}',
    '{"video": "relative.mp4"}', '{"video": "https://example.com/clip.mp4"}',
    '{"video": "\\\\\\\\server\\\\clip.mp4"}',
    '{"duration": 1, "duration": 2}', '{"extra":{"x":1,"x":2}}',
    '{"extra":{"x":1e309}}',
])
def test_corrupt_payload_is_rejected_without_editing_or_removing_row(library, raw):
    row = raw_record(library, raw)
    with pytest.raises(DataError):
        storage.decode_record_payload(row)
    assert library.record("bad") == row
    assert library.records() == [row]


@pytest.mark.parametrize("payload", [[], None, 2, {"duration": float("nan")},
    {"duration": "5"}, {"width": True}, {"audio_streams": -1}])
def test_invalid_new_record_is_not_written(library, payload):
    with pytest.raises(DataError):
        library.save_record("r1", payload)
    assert library.records() == []


def test_decode_keeps_legacy_and_current_result_fields_and_snapshot(library, tmp_path):
    payload = {"video": str(tmp_path / "中文录像.mp4"), "player": "101", "map": "de_mirage",
        "video_result": "metadata_verified", "duration": 4.5, "width": 1920, "height": 1080,
        "video_streams": 1, "audio_streams": 0,
        "draft_snapshot": {"demo": str(tmp_path / "original.dem"), "selection": {"start_tick": 1}}}
    library.save_record("new", payload, "complete")
    assert storage.decode_record_payload(library.record("new")) == payload
    library.save_record("legacy", {"video": str(tmp_path / "old.mp4"), "result": "verified"})
    assert storage.decode_record_payload(library.record("legacy"))["result"] == "verified"


def test_overwrite_validation_preserves_previous_record(library):
    library.save_record("r1", {"player": "101"}, "complete")
    before = library.record("r1")
    with pytest.raises(DataError):
        library.save_record("r1", {"duration": "broken"}, "blocked")
    assert library.record("r1") == before


def test_oversized_payload_is_unavailable_without_removal(library):
    row = raw_record(library, json.dumps({"notes": "x" * (1024 * 1024)}))
    with pytest.raises(DataError):
        storage.decode_record_payload(row)
    assert library.record("bad") == row


def test_remove_bad_row_does_not_require_decoding(library):
    raw_record(library, "{")
    library.remove_record("bad")
    assert library.record("bad") is None


@pytest.mark.parametrize("field,value", [("created", "bad"), ("created", -1),
    ("created", float("inf")), ("restore_status", "success"), ("id", "")])
def test_invalid_index_metadata_is_rejected_without_writes(library, field, value):
    row = raw_record(library, "{}")
    candidate = {**row, field: value}
    with pytest.raises(DataError):
        storage.decode_record_payload(candidate)
    assert library.record("bad") == row


def test_excessive_nesting_is_unavailable_without_recursion_crash(library):
    row = raw_record(library, '{"extra":' + '[' * 40 + '1' + ']' * 40 + '}')
    with pytest.raises(DataError):
        storage.decode_record_payload(row)
    assert library.record("bad") == row
