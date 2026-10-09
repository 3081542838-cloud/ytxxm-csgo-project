"""Replace the audited plain-JavaScript DATA slot in a Source 2 resource.

No compiler, native executable, or unrelated block rewriting is involved. The
caller must authenticate the source archive before invoking this operation.
"""
import struct

from cs2pov.storage.settings import DataError


def replace_script_slot(raw, original, replacement):
    if len(raw) < 52 or not original or raw.count(original) != 1:
        raise DataError('HUD 脚本槽位无法唯一验证。')
    size, header_version, resource_version, directory_offset, count = struct.unpack_from('<IHHII', raw)
    if (size != len(raw) or header_version != 12 or resource_version != 2
            or directory_offset != 8 or count != 3):
        raise DataError('HUD 编译资源结构与审核版本不一致。')
    blocks = []
    for index in range(count):
        entry = 16 + index * 12
        tag = raw[entry:entry + 4]
        offset, length = struct.unpack_from('<II', raw, entry + 4)
        start = entry + 4 + offset
        if start < 52 or start + length > len(raw):
            raise DataError('HUD 编译资源区块越界。')
        blocks.append((tag, entry, start, length))
    if [block[0] for block in blocks] != [b'RED2', b'DATA', b'STAT']:
        raise DataError('HUD 编译资源区块列表改变。')
    if any(left[2] + left[3] > right[2] for left, right in zip(blocks, blocks[1:])):
        raise DataError('HUD 编译资源区块重叠。')
    at = raw.index(original)
    data = blocks[1]
    if not data[2] <= at or at + len(original) != data[2] + data[3]:
        raise DataError('HUD 脚本槽位不属于 DATA 区块末尾。')
    # Preserve the original trailing block alignment by growing in multiples
    # of four. Small ordinary sessions remain byte-length compatible.
    delta = max(0, (len(replacement) - len(original) + 3) // 4 * 4)
    padded = replacement.ljust(len(original) + delta, b' ')
    result = bytearray(raw[:at] + padded + raw[at + len(original):])
    struct.pack_into('<I', result, 0, len(result))
    struct.pack_into('<I', result, data[1] + 8, data[3] + delta)
    stat = blocks[2]
    struct.pack_into('<I', result, stat[1] + 4, stat[2] + delta - (stat[1] + 4))
    return bytes(result)
