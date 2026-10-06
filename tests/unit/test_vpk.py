import hashlib
import struct
import zlib

import pytest

from cs2pov.adapters.vpk import VpkError, build_vpk, parse_vpk, validate_resource


def fixture(*, version=2, directory="panorama/styles/hud", name="pov", preload=b"", data=b"fixture CSS", archive=0x7FFF, terminator=0xFFFF):
    tree = b"vcss_c\0" + directory.encode() + b"\0" + name.encode() + b"\0"
    entry_start = len(tree)
    tree += struct.pack("<IHHIIH", zlib.crc32(preload + data), len(preload), archive, 0, len(data), terminator)
    tree += preload + b"\0\0\0"
    header = struct.pack("<3I", 0x55AA1234, version, len(tree))
    if version == 2:
        header += struct.pack("<4I", len(data), 0, 0, 0)
    return header + tree + data, len(header) + entry_start


@pytest.mark.parametrize("version,preload", [(1,b""),(2,b""),(2,b"prefix")])
def test_self_contained_file_and_preload(version, preload):
    raw, _ = fixture(version=version, preload=preload)
    result = parse_vpk(raw)
    assert result.version == version
    assert result.sha256 == hashlib.sha256(raw).hexdigest()
    assert result.entries[0].path == "panorama/styles/hud/pov.vcss_c"
    assert result.entries[0].data == preload + b"fixture CSS"


@pytest.mark.parametrize("directory", ["../escape", "/absolute", "C:/outside", "a//b", "a\\b", "a/./b"])
def test_unsafe_paths_are_rejected(directory):
    raw, _ = fixture(directory=directory)
    with pytest.raises(VpkError, match="path"):
        parse_vpk(raw)


@pytest.mark.parametrize("archive,terminator", [(0,0xFFFF),(0x7FFF,0)])
def test_external_archives_and_bad_terminators_rejected(archive, terminator):
    raw, _ = fixture(archive=archive, terminator=terminator)
    with pytest.raises(VpkError):
        parse_vpk(raw)


def test_truncation_bad_header_crc_and_entry_bounds():
    raw, entry_start = fixture()
    variants = [raw[:7], b"\0" * 12, raw[:-1], raw[:-1] + b"X"]
    damaged = bytearray(raw)
    struct.pack_into("<I", damaged, entry_start + 8, 100000)
    variants.append(bytes(damaged))
    for damaged in variants:
        with pytest.raises(VpkError):
            parse_vpk(damaged)


def test_unterminated_tree_string():
    with pytest.raises(VpkError):
        parse_vpk(struct.pack("<3I",0x55AA1234,1,4) + b"xxxx")


def test_deployment_requires_exact_reviewed_hash_and_paths(tmp_path):
    raw, _ = fixture()
    target = tmp_path / "pov.vpk"
    target.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    paths = {"panorama/styles/hud/pov.vcss_c"}
    assert validate_resource(target, digest, paths).sha256 == digest
    for wrong_hash, wrong_paths in [("",paths),(digest,set()),("0"*64,paths),(digest,{"other"})]:
        with pytest.raises(VpkError):
            validate_resource(target, wrong_hash, wrong_paths)


def test_rebuilt_archive_keeps_exact_selected_payloads():
    files = {"panorama/styles/hud/a.vcss_c": b"CSS", "panorama/scripts/hud/pov.vts_c": b"script"}
    archive = parse_vpk(build_vpk(files))
    assert {entry.path: entry.data for entry in archive.entries} == files


@pytest.mark.parametrize("files", [{}, {"a//b.vcss_c": b"x"}, {"../b.vcss_c": b"x"},
    {"a/x.vcss_c": b"x", "A/X.vcss_c": b"y"}])
def test_rebuilder_rejects_unsafe_or_ambiguous_packages(files):
    with pytest.raises(VpkError):
        build_vpk(files)
