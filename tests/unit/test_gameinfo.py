import pytest
from cs2pov.adapters.gameinfo import GameInfoError, add_pov_search_path


@pytest.mark.parametrize("newline,bom,quoted", [("\n", False, False), ("\r\n", True, True)])
def test_only_one_line_inserted_and_original_bytes_preserved(newline, bom, quoted):
    entry = '"Game" "csgo"' if quoted else 'Game csgo'
    text = newline.join(['"GameInfo"', '{', '// 中文 { SearchPaths', 'FileSystem', '{',
                         'SearchPaths', '{', f'\t{entry} // keep', '\tGame core', '}', '}', '}', ''])
    raw = text.encode("utf-8-sig" if bom else "utf-8")
    result = add_pov_search_path(raw)
    insertion = f"\tGame\tcsgo/pov.vpk{newline}".encode()
    assert result.count(insertion) == 1
    assert result.replace(insertion, b"", 1) == raw
    assert result.index(b"csgo/pov.vpk") < result.index(entry.encode())


@pytest.mark.parametrize("text", [
    'SearchPaths\n{\nGame core\n}\n',
    'SearchPaths\n{\nGame csgo\nGame csgo\n}\n',
    'SearchPaths\n{\nGame csgo/pov.vpk\nGame csgo\n}\n',
    'SearchPaths\n{\nGame csgo\n',
    'SearchPaths\n{\nGame csgo\n}\nSearchPaths { }',
    'SearchPaths { Game csgo }',
    'Other\n{\nGame csgo\n}\nSearchPaths\n{\nGame core\n}\n',
])
def test_uncertain_or_existing_entries_block(text):
    with pytest.raises(GameInfoError):
        add_pov_search_path(text.encode())


def test_invalid_encoding_blocks():
    with pytest.raises(GameInfoError):
        add_pov_search_path(b"\xff")
