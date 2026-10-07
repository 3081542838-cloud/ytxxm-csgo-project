from pathlib import Path

import pytest

from cs2pov.services import telemetry_resource as hud
from cs2pov.storage.settings import DataError


@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_bundled_hud_reconstructs_and_verifies_with_windows_or_lf_assets(tmp_path, monkeypatch, newline):
    original_read = hud.read_small
    def read(path):
        raw = original_read(path)
        if Path(path).suffix == '.js':
            raw = raw.replace(b'\r\n', b'\n').replace(b'\n', newline)
        return raw
    monkeypatch.setattr(hud, 'read_small', read)
    base = Path('resources/bundled/pov.vpk')
    result = hud.build_session_resource(base, tmp_path / 'session.vpk', nonce='SESSION_0000000001')
    assert hud.verify_session_resource(result, result.path) == result.sha256
    assert result.path.stat().st_size == base.stat().st_size
    assert result.path.read_bytes() == hud.resource_bytes(base, result.nonce)


def test_newline_normalization_does_not_accept_changed_visibility_script(tmp_path, monkeypatch):
    original_read = hud.read_small
    def read(path):
        raw = original_read(path)
        if Path(path).name == 'pov_visibility.js':
            return raw + b'\n// altered content\n'
        return raw
    monkeypatch.setattr(hud, 'read_small', read)
    with pytest.raises(DataError, match='脚本槽位'):
        hud.build_session_resource(Path('resources/bundled/pov.vpk'), tmp_path / 'session.vpk')
    assert not (tmp_path / 'session.vpk').exists()
