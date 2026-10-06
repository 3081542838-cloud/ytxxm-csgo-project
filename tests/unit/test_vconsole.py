import pytest
from cs2pov.adapters.vconsole import HEADER, VERSION, FrameReader, VConsoleError, command_packet, print_text


def test_utf8_command_packet_and_full_two_byte_length():
    command = 'echo "' + '虾'*200 + '"'
    packet = command_packet(command)
    assert HEADER.unpack_from(packet) == (b"CMND", VERSION, len(packet), 0)
    assert packet[12:-1].decode() == command and len(packet) > 255


@pytest.mark.parametrize("command", ["", "echo x\nquit", "x\0", "x\t", "x"*4097, "\ud800"])
def test_unsafe_command_rejected(command):
    with pytest.raises(VConsoleError): command_packet(command)


def test_split_coalesced_messages_preserve_utf8_and_do_not_confuse_echo_with_proof():
    text = "echo player 123 小虾米\n".encode() + b"\0"
    packet = HEADER.pack(b"PRNT", VERSION, 40+len(text), 0) + bytes(28) + text
    for split in range(len(packet)):
        reader = FrameReader()
        frames = reader.feed(packet[:split]) + reader.feed(packet[split:] + packet)
        assert len(frames) == 2
        assert print_text(frames[0]) == text[:-1].decode()
        reader.finish()


def test_malformed_stream_fails_closed_without_resync():
    reader = FrameReader()
    with pytest.raises(VConsoleError): reader.feed(b"junk" + bytes(8) + command_packet("echo x"))
    with pytest.raises(VConsoleError): reader.feed(command_packet("echo x"))
    reader = FrameReader(); reader.feed(b"PRN")
    with pytest.raises(VConsoleError): reader.finish()
    with pytest.raises(VConsoleError): FrameReader().feed(bytes(131073))


def test_unknown_frames_are_not_text_or_evidence():
    assert print_text((b"AINF", VERSION, 0, b"hello")) is None
    with pytest.raises(VConsoleError): print_text((b"PRNT", VERSION, 0, b"bad"))
