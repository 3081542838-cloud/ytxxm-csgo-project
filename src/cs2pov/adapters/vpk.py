"""Bounded, read-only parser for small self-contained VPK v1/v2 resources.

Inspection verifies archive structure/CRCs, not that scripts are safe to run.
Deployment additionally requires an explicitly reviewed file list and SHA256.
"""

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
import struct
import zlib
from collections import defaultdict

MAX_RESOURCE_BYTES = 8 * 1024 * 1024
SIGNATURE = 0x55AA1234


class VpkError(ValueError):
    pass


@dataclass(frozen=True)
class VpkEntry:
    path: str
    data: bytes
    crc32: int


@dataclass(frozen=True)
class VpkArchive:
    version: int
    sha256: str
    entries: tuple[VpkEntry, ...]


def _resource_path(extension: str, directory: str, name: str) -> str:
    if directory == " ":
        directory = ""
    suffix = "" if extension == " " else "." + extension
    candidate = (directory + "/" if directory else "") + name + suffix
    if ("\\" in candidate or ":" in candidate or candidate.startswith("/")
            or any(ord(character) < 32 for character in candidate)
            or any(part in ("", ".", "..") for part in candidate.split("/"))
            or "/" in name or "/" in extension):
        raise VpkError(f"Unsafe resource path: {candidate!r}")
    if PurePosixPath(candidate).is_absolute():
        raise VpkError("Absolute resource path")
    return candidate


def parse_vpk(raw: bytes) -> VpkArchive:
    if len(raw) > MAX_RESOURCE_BYTES or len(raw) < 12:
        raise VpkError("Resource is oversized or header is truncated")
    signature, version, tree_size = struct.unpack_from("<3I", raw)
    if signature != SIGNATURE or version not in (1, 2):
        raise VpkError("Unsupported VPK signature or version")
    header_size = 12 if version == 1 else 28
    if len(raw) < header_size:
        raise VpkError("Truncated VPK v2 header")
    tree_end = header_size + tree_size
    if tree_size > 512 * 1024 or tree_end > len(raw):
        raise VpkError("Invalid tree size")
    data_end = len(raw)
    if version == 2:
        data_size, archive_md5, other_md5, signature_size = struct.unpack_from("<4I", raw, 12)
        data_end = tree_end + data_size
        if data_end + archive_md5 + other_md5 + signature_size != len(raw):
            raise VpkError("Declared section sizes do not match file")
    cursor = header_size

    def string() -> str:
        nonlocal cursor
        end = raw.find(b"\0", cursor, tree_end)
        if end < 0 or end - cursor > 1024:
            raise VpkError("Unterminated or oversized tree string")
        try:
            result = raw[cursor:end].decode("utf-8")
        except UnicodeError as error:
            raise VpkError("Invalid tree string encoding") from error
        cursor = end + 1
        return result

    entries = []
    names = set()
    while extension := string():
        while directory := string():
            while name := string():
                if cursor + 18 > tree_end:
                    raise VpkError("Truncated directory entry")
                crc, preload_size, archive_index, offset, length, terminator = struct.unpack_from("<IHHIIH", raw, cursor)
                cursor += 18
                if terminator != 0xFFFF or archive_index != 0x7FFF:
                    raise VpkError("Invalid terminator or external archive reference")
                if cursor + preload_size > tree_end or tree_end + offset + length > data_end:
                    raise VpkError("Entry data is outside declared sections")
                payload = raw[cursor:cursor + preload_size] + raw[tree_end + offset:tree_end + offset + length]
                cursor += preload_size
                path = _resource_path(extension, directory, name)
                if path.casefold() in names:
                    raise VpkError("Duplicate resource path")
                names.add(path.casefold())
                if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                    raise VpkError(f"CRC mismatch: {path}")
                entries.append(VpkEntry(path, payload, crc))
                if len(entries) > 2048:
                    raise VpkError("Too many resource entries")
    if cursor != tree_end or not entries:
        raise VpkError("Tree has trailing bytes or no resources")
    return VpkArchive(version, hashlib.sha256(raw).hexdigest(), tuple(entries))


def inspect_vpk(path: Path) -> VpkArchive:
    with path.open("rb") as stream:
        raw = stream.read(MAX_RESOURCE_BYTES + 1)
    return parse_vpk(raw)


def validate_resource(path: Path, expected_sha256: str, allowed_paths: set[str]) -> VpkArchive:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) or not allowed_paths:
        raise VpkError("Reviewed SHA256 and nonempty file list are required")
    archive = inspect_vpk(path)
    if archive.sha256 != expected_sha256.lower():
        raise VpkError("Resource hash differs from reviewed version")
    if {entry.path for entry in archive.entries} != allowed_paths:
        raise VpkError("Resource file list differs from reviewed version")
    return archive


def build_vpk(files: dict[str, bytes]) -> bytes:
    """Create a self-contained v2 archive; validate the result before use."""
    groups = defaultdict(lambda: defaultdict(list))
    seen = set()
    for path, data in files.items():
        parsed = PurePosixPath(path)
        extension = parsed.suffix[1:] or " "
        name = parsed.name[:-len(parsed.suffix)] if parsed.suffix else parsed.name
        directory = str(parsed.parent) if str(parsed.parent) != "." else " "
        normalized = _resource_path(extension, directory, name)
        if normalized != path or path.casefold() in seen or not isinstance(data, bytes):
            raise VpkError("Invalid, duplicate or noncanonical resource")
        seen.add(path.casefold())
        groups[extension][directory].append((name, data))
    tree = bytearray()
    body = bytearray()
    for extension in sorted(groups):
        tree.extend(extension.encode("utf-8") + b"\0")
        for directory in sorted(groups[extension]):
            tree.extend(directory.encode("utf-8") + b"\0")
            for name, data in sorted(groups[extension][directory]):
                tree.extend(name.encode("utf-8") + b"\0")
                tree.extend(struct.pack("<IHHIIH", zlib.crc32(data), 0, 0x7FFF, len(body), len(data), 0xFFFF))
                body.extend(data)
            tree.extend(b"\0")
        tree.extend(b"\0")
    tree.extend(b"\0")
    result = struct.pack("<7I", SIGNATURE, 2, len(tree), len(body), 0, 0, 0) + tree + body
    parse_vpk(result)
    return result
