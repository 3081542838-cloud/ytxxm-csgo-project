from pathlib import Path
import pytest
from cs2pov.adapters.demo_path import playdemo_command


def test_chinese_spaces_and_backslashes_preserved_in_quoted_forward_path():
    path = Path(r"C:\Users\测试员\AppData\Roaming\我的 demo\one.dem")
    assert playdemo_command(path) == 'playdemo "C:/Users/测试员/AppData/Roaming/我的 demo/one.dem"'


@pytest.mark.parametrize("value", ["relative.dem", r"C:\demo.txt", 'C:/a;quit.dem', 'C:/a".dem', 'C:/a\n.dem'])
def test_uncertain_paths_cannot_generate_console_commands(value):
    with pytest.raises(ValueError):
        playdemo_command(Path(value))
