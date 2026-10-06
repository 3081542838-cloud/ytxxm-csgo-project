from datetime import datetime
from types import SimpleNamespace

import pytest

from cs2pov.adapters.binding_log import (BindingLogDecoder, BindingLogReader,
                                         binding_query_command)
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplayLogReader
from cs2pov.storage.settings import DataError


IDENTITY = ProcessIdentity(28612, 100, "C:/game/cs2.exe")
NONCE = "a7ab2982b316d993edb96e7629caeb5f"
OTHER = "c4a7c91c5e7fca6213ffbdfca2909157"
PLAYBACK = "unbind F8; demo_resume; demo_pauseatservertick 13810"
WALL = datetime(2026, 10, 3, 12, 8, 36).timestamp()


def decoder(expected=PLAYBACK):
    timer = SimpleNamespace(mono=100.0, wall=WALL + 0.25)
    value = BindingLogDecoder(IDENTITY, NONCE, expected=expected,
                              clock=lambda: timer.mono, wall=lambda: timer.wall)
    return value, timer


def line(body, stamp="12:08:36", channel="Console"):
    return f"10/03 {stamp} [{channel}] {body}\n"


def response(value=PLAYBACK, nonce=NONCE):
    return [line(f"POV_BIND_BEGIN_{nonce}"),
            line(f'bind [player 0]: "F8" = "{value}"'),
            line(f"POV_BIND_END_{nonce}")]


def feed(value, lines):
    for item in lines:
        value.consume(item)
    return value.readback()


def test_live_engine_bound_response_keeps_owned_identity_exact_text_and_oldest_time():
    # Verbatim three-line response observed in the real engine log at 12:08:36.
    value, _ = decoder()
    proof = feed(value, response())
    assert proof.process_identity == IDENTITY
    assert proof.key == "F8" and proof.value == PLAYBACK
    assert proof.request_nonce == NONCE and proof.observed_at == 99.75
    assert value.state == "complete"


def test_live_empty_binding_can_be_proved_without_an_expected_command():
    # Separate verbatim empty response observed after the temporary live bind
    # self-unbound, not a fabricated rewriting of the preceding bound response.
    value = BindingLogDecoder(IDENTITY, OTHER, clock=lambda: 100,
        wall=lambda: datetime(2026, 10, 3, 12, 9, 16).timestamp() + .25)
    proof = feed(value, [line(f"POV_BIND_BEGIN_{OTHER}", stamp="12:09:16"),
                        line('bind [player 0]: "F8" = ""', stamp="12:09:16"),
                        line(f"POV_BIND_END_{OTHER}", stamp="12:09:16")])
    assert proof.value == "" and proof.request_nonce == OTHER


def test_query_helper_contains_only_the_validated_key_and_request_markers():
    assert binding_query_command("F8", NONCE) == (
        f'echo POV_BIND_BEGIN_{NONCE}; bind "F8"; echo POV_BIND_END_{NONCE}')


@pytest.mark.parametrize("key,nonce", [("F9", NONCE), ("f8", NONCE),
    ('F8"; quit', NONCE), (8, NONCE), ("F8", ""), ("F8", "a" * 31),
    ("F8", "z" * 32), ("F8", NONCE + "; quit"), ("F8", 123)])
def test_query_key_or_nonce_cannot_inject_commands(key, nonce):
    with pytest.raises(DataError):
        binding_query_command(key, nonce)
    with pytest.raises(DataError):
        BindingLogDecoder(IDENTITY, nonce, key=key)


@pytest.mark.parametrize("expected", ["", "demo_resume", 'unbind "F8"; demo_resume; demo_pauseatservertick 13810',
    PLAYBACK + "; quit", PLAYBACK + "\n", PLAYBACK.replace("13810", "0"),
    PLAYBACK.replace("13810", "-1"), PLAYBACK.replace("13810", "2147483648"),
    PLAYBACK.replace("13810", "1.5"), PLAYBACK.replace("13810", "013810"), 123])
def test_only_the_restricted_self_unbinding_command_can_be_expected(expected):
    with pytest.raises(DataError):
        decoder(expected=expected)


@pytest.mark.parametrize("identity", [None, SimpleNamespace(pid=1, created=1, executable="cs2.exe"),
    ProcessIdentity(True, 1, "cs2.exe"), ProcessIdentity(1, 0, "cs2.exe"),
    ProcessIdentity(1, 1, "")])
def test_evidence_requires_the_held_owned_process_identity(identity):
    with pytest.raises(DataError):
        BindingLogDecoder(identity, NONCE)


@pytest.mark.parametrize("lines", [
    [line(f"echo POV_BIND_BEGIN_{NONCE}; bind F8; echo POV_BIND_END_{NONCE}")],
    [line(f"POV_BIND_BEGIN_{NONCE}", channel="ALL"), line('bind [player 0]: "F8" = ""', channel="ALL"),
     line(f"POV_BIND_END_{NONCE}", channel="ALL")],
    [line(f"someone: POV_BIND_BEGIN_{NONCE}"), line('someone: bind [player 0]: "F8" = ""'),
     line(f"someone: POV_BIND_END_{NONCE}")],
    [line(f"POV_BIND_BEGIN_{OTHER}"), response()[1], response()[2]],
    [response()[0], response()[1], line(f"POV_BIND_END_{OTHER}")],
    [response()[2], response()[0], response()[1]],
    [response()[0], response()[2]],
    [response()[1], response()[0], response()[2]],
    [response()[0], response()[0], response()[1], response()[2]],
    [response()[0], response()[1], response()[1], response()[2]],
    [response()[0], line("Unknown command: bind"), response()[1], response()[2]],
    [response()[0], line('bind [player 1]: "F8" = ""'), response()[2]],
    [response()[0], line('bind [player 0]: "F9" = ""'), response()[2]],
    [response()[0], line('bind [player 0]: "F8" = "quit"'), response()[2]],
    [response()[0], line('bind [player 0]: "F8" = "' + PLAYBACK.replace("13810", "13811") + '"'), response()[2]],
    [response()[0], line('bind [player 0]: "F8" = "" trailing'), response()[2]],
])
def test_echo_chat_wrong_nonce_order_duplicates_unknown_and_other_values_cannot_succeed(lines):
    value, _ = decoder()
    assert feed(value, lines) is None
    # A failure cannot be repaired by replaying a fresh-looking successful reply.
    if value.state == "invalid":
        assert feed(value, response()) is None


def test_other_channels_can_interleave_without_contributing_evidence():
    value, _ = decoder()
    proof = feed(value, [response()[0], line("POV_READBACK {state:example}", channel="PanoramaScript"),
                         response()[1], response()[2]])
    assert proof.value == PLAYBACK


def test_duplicate_response_after_completion_invalidates_and_cannot_replace_evidence():
    value, _ = decoder()
    assert feed(value, response()) is not None
    assert feed(value, response(value="")) is None
    assert value.state == "invalid"


def test_command_echo_never_reinterprets_nested_markers_as_a_response():
    value, _ = decoder()
    echo = line(binding_query_command("F8", NONCE))
    assert feed(value, [echo]) is None
    # An ordinary command echo before the separate engine response is harmless.
    assert feed(value, response()).value == PLAYBACK


@pytest.mark.parametrize("bad", [
    response()[0].rstrip("\n"), response()[0] + response()[1],
    line("\x00POV_BIND_BEGIN_" + NONCE), line("x" * 2049),
    line("虾" * 700), line("\ud800"),
])
def test_incomplete_multiline_or_unbounded_utf8_lines_fail_the_request(bad):
    value, _ = decoder()
    value.consume(bad)
    assert feed(value, response()) is None


@pytest.mark.parametrize("stamp", ["12:08:30", "12:08:37", "99:99:99"])
def test_stale_future_or_invalid_source_timestamps_cannot_prove_a_binding(stamp):
    value, _ = decoder()
    assert feed(value, [line(f"POV_BIND_BEGIN_{NONCE}", stamp=stamp),
                        response()[1], response()[2]]) is None


def test_ending_marker_cannot_rejuvenate_an_older_binding_or_reverse_source_time():
    value, timer = decoder()
    assert feed(value, response()[:2]) is None
    timer.mono += 4; timer.wall += 4
    value.consume(line(f"POV_BIND_END_{NONCE}", stamp="12:08:40"))
    assert value.readback().observed_at == 99.75
    timer.mono += 1; timer.wall += 1
    assert value.readback() is None and value.state == "invalid"
    # Monotonic reversal does not resurrect expired proof.
    timer.mono -= 5; timer.wall -= 5
    assert value.readback() is None
    value, timer = decoder()
    timer.mono += 1; timer.wall += 1
    value.consume(line(f"POV_BIND_BEGIN_{NONCE}", stamp="12:08:37"))
    assert feed(value, response()[1:]) is None


def test_nonempty_output_requires_the_callers_exact_expected_command():
    value, _ = decoder(expected=None)
    assert feed(value, response()) is None


def test_reader_checkpoint_ignores_old_query_without_advancing_replay_cursor(tmp_path):
    log = tmp_path / "engine.log"
    log.write_text("".join(response()), encoding="utf-8")
    replay = ReplayLogReader(tmp_path, IDENTITY, NONCE, clock=lambda: 100, wall=lambda: WALL + .25)
    old_replay_cursor = replay.cursor
    reader = BindingLogReader(tmp_path, IDENTITY, NONCE, expected=PLAYBACK,
                              clock=lambda: 100, wall=lambda: WALL + .25)
    assert reader.readback() is None
    with log.open("a", encoding="utf-8", newline="") as stream:
        stream.write("".join(response()))
    assert reader.readback().value == PLAYBACK
    assert replay.cursor == old_replay_cursor
    assert replay.readback() is None
    assert replay.cursor == reader.cursor


def test_reader_waits_for_complete_end_line_and_handles_crlf(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"")
    reader = BindingLogReader(tmp_path, IDENTITY, NONCE, expected=PLAYBACK,
                              clock=lambda: 100, wall=lambda: WALL + .25)
    text = "".join(response()).replace("\n", "\r\n").encode("utf-8")
    with log.open("ab") as stream:
        stream.write(text[:-1])
    assert reader.readback() is None
    with log.open("ab") as stream:
        stream.write(text[-1:])
    assert reader.readback().value == PLAYBACK


@pytest.mark.parametrize("fault", ["truncate", "replace", "encoding", "oversize"])
def test_reader_log_failures_invalidate_already_proved_binding(tmp_path, fault):
    log = tmp_path / "engine.log"
    log.write_bytes(b"")
    reader = BindingLogReader(tmp_path, IDENTITY, NONCE, expected=PLAYBACK,
                              clock=lambda: 100, wall=lambda: WALL + .25)
    with log.open("ab") as stream:
        stream.write("".join(response()).encode("utf-8"))
    assert reader.readback() is not None
    if fault == "truncate":
        log.write_bytes(b"x")
    elif fault == "replace":
        log.rename(tmp_path / "old-engine.log")
        log.write_bytes(b"new log\n")
    elif fault == "encoding":
        with log.open("ab") as stream:
            stream.write(b"\xff\n")
    else:
        with log.open("ab") as stream:
            stream.write(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises((DataError, OSError)):
        reader.readback()
    assert reader.decoder.readback() is None and reader.decoder.state == "invalid"
