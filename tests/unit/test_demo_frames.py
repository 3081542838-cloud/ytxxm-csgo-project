import struct
from pathlib import Path
import pytest
from cs2pov.adapters.demo import DemoError, validate_frames, fingerprint
from cs2pov.services.demo_worker import analyse


def vint(number):
    out = bytearray()
    while number >= 128:
        out.append((number & 127) | 128); number >>= 7
    out.append(number)
    return bytes(out)


def frame(command, tick, payload=b""):
    return vint(command) + vint(tick) + vint(len(payload)) + payload


def complete_frames():
    info = b"\x0d" + struct.pack("<f", 2) + b"\x10" + vint(128)
    return b"PBDEMS2\x00" + bytes(8) + frame(1, 0, b"header") + frame(7, 128, b"data") + frame(0, 128) + frame(2, 128, info)


def test_streaming_complete_frames_reads_file_tick_rate(tmp_path):
    path = tmp_path / "test.dem"; path.write_bytes(complete_frames())
    assert validate_frames(path) == {"duration": 2, "playback_ticks": 128, "tick_rate": 64}


@pytest.mark.parametrize("raw", [b"bad", b"HL2DEMO\x00" + bytes(40),
    complete_frames()[:-2], complete_frames()[:25],
    b"PBDEMS2\x00" + bytes(8) + frame(1, 0) + frame(7, 100, b"x"),
    b"PBDEMS2\x00" + bytes(8) + frame(1, 0) + frame(99, 0),
    b"PBDEMS2\x00" + bytes(8) + b"\x80" * 6,
])
def test_invalid_or_truncated_frames_do_not_silently_pass(tmp_path, raw):
    path = tmp_path / "bad.dem"; path.write_bytes(raw)
    with pytest.raises(DemoError): validate_frames(path)


def test_content_cache_invalidates_even_when_size_and_mtime_are_unchanged(tmp_path):
    import os
    path = tmp_path / "test.dem"; path.write_bytes(b"AAAA")
    original_time = path.stat().st_mtime_ns
    calls = []
    def parse(file):
        from test_clips import analysis_fixture
        calls.append(file.read_bytes())
        result = analysis_fixture()
        result["map"] = calls[-1].decode()
        return result
    first = analyse(path, tmp_path / "cache", parser=parse)
    assert analyse(path, tmp_path / "cache", parser=parse) == first
    path.write_bytes(b"BBBB")
    os.utime(path, ns=(original_time, original_time))
    second = analyse(path, tmp_path / "cache", parser=parse)
    assert first["fingerprint"]["sha256"] != second["fingerprint"]["sha256"]
    assert calls == [b"AAAA", b"BBBB"]


def test_corrupt_cache_is_rebuilt_from_original(tmp_path):
    import json
    from test_clips import analysis_fixture
    path = tmp_path / "test.dem"; path.write_bytes(b"original")
    first = analyse(path, tmp_path / "cache", parser=lambda _: analysis_fixture())
    cache = Path(first["cache"])
    raw = json.loads(cache.read_text(encoding="utf-8"))
    raw["analysis"]["map"] = "incorrect"
    cache.write_text(json.dumps(raw), encoding="utf-8")
    calls = []
    def parser(_):
        calls.append(True)
        return analysis_fixture()
    analyse(path, tmp_path / "cache", parser=parser)
    assert calls == [True]
    assert json.loads(cache.read_text(encoding="utf-8"))["analysis"]["map"] == "de_test"


def test_file_changed_during_parse_has_no_published_cache(tmp_path):
    path = tmp_path / "test.dem"; path.write_bytes(b"AAAA")
    def changing(file):
        file.write_bytes(b"BBBB")
        return {"schema": 1}
    with pytest.raises(DemoError, match="内容变化"):
        analyse(path, tmp_path / "cache", parser=changing)
    assert not list((tmp_path / "cache").glob("*.json"))


def test_old_analysis_cache_reparsed_without_removing_old_data(tmp_path):
    import json
    from test_clips import analysis_fixture
    path = tmp_path / "old.dem"; path.write_bytes(b"original")
    directory = tmp_path / "cache"; directory.mkdir()
    old = directory / ("analysis-v1-demoparser-0.42.0-" + fingerprint(path)["sha256"] + ".json")
    old.write_text('{"old": true}', encoding="utf-8")
    calls = []
    def parser(_):
        calls.append(True)
        return analysis_fixture()
    result = analyse(path, directory, parser=parser)
    assert calls == [True] and old.read_text() == '{"old": true}'
    assert json.loads(Path(result["cache"]).read_text(encoding="utf-8"))["analysis"]["schema"] == 2
