from pathlib import Path
from test_package import load_script, source_fixture


def test_single_file_command_keeps_icon_resources_and_runtime_dependencies(tmp_path):
    module = load_script('build_onefile')
    root = source_fixture(tmp_path)
    candidate = root / '.build' / 'singlefile-test'
    args = module.command(root, candidate, python='python.exe')
    assert '--onefile' in args and '--onedir' not in args
    assert args[args.index('--icon') + 1] == str(root / 'src/cs2pov/resources/shrimp.ico')
    assert args.count('--add-data') == 4
    assert 'pyarrow' in args and 'demoparser2' in args
    assert args[-1] == str(root / 'src/cs2pov/app.py')


def test_single_file_command_does_not_embed_local_settings_or_hud(tmp_path):
    module = load_script('build_onefile')
    root = source_fixture(tmp_path)
    args = module.command(root, root / '.build' / 'singlefile-test')
    for item in args:
        assert 'local-validation' not in item and 'pov.vpk' not in item
        assert '.dem' not in item and 'library.sqlite' not in item
