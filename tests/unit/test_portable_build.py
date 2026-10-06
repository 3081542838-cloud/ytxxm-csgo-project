import hashlib
import pytest
from pathlib import Path
from test_package import load_script


def test_portable_resource_copy_accepts_only_exact_reviewed_bytes(tmp_path):
    module = load_script('build_portable')
    source = tmp_path / 'resource'; source.write_bytes(b'exact reviewed bytes')
    target = tmp_path / 'package' / 'pov.vpk'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    module.copy_verified(source, target, digest, source.stat().st_size)
    assert source.read_bytes() == target.read_bytes()
    with pytest.raises(module.base.BuildError, match='覆盖'):
        module.copy_verified(source, target, digest, source.stat().st_size)


def test_portable_resource_copy_rejects_tamper_before_creating_target(tmp_path):
    module = load_script('build_portable')
    source = tmp_path / 'resource'; source.write_bytes(b'changed')
    target = tmp_path / 'package' / 'ffprobe.exe'
    with pytest.raises(module.base.BuildError):
        module.copy_verified(source, target, '0' * 64, source.stat().st_size)
    assert not target.exists()


def test_portable_package_contains_verified_offline_hud_and_not_private_profile(tmp_path):
    module = load_script('build_portable')
    root = Path(__file__).resolve().parents[2]
    candidate = tmp_path / 'candidate'
    (candidate / 'licenses').mkdir(parents=True)
    (candidate / 'licenses/NOTICE.txt').write_text('application notices')
    executable = tmp_path / 'application.exe'
    executable.write_bytes(b'build fixture')
    folder = module.assemble(root, candidate, executable)
    from cs2pov.storage.local_resources import LocalResources, HUD_SHA256
    hud = folder / 'resources/pov.vpk'
    assert hashlib.sha256(hud.read_bytes()).hexdigest() == HUD_SHA256
    assert LocalResources(folder / 'data/local-resources.json').pick('hud', hud).path == str(hud)
    assert not any((folder / 'data').iterdir())
    assert not (folder / 'tools').exists()
    assert 'ffprobe' not in (folder / '使用说明.txt').read_text(encoding='utf-8')
    assert (folder / 'licenses/hud-reference-LICENSE').read_bytes() == (
        root / 'resources/bundled/LICENSE').read_bytes()
    assert 'Required Notice: Copyright (c) 2026 DrEAmSs59' in (
        folder / 'licenses/hud-CHANGES.txt').read_text(encoding='utf-8')
    assert '启动不会下载资源' in (folder / '使用说明.txt').read_text(encoding='utf-8')
