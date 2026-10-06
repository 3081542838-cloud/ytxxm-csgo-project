"""Frozen parser dependencies must be exercised, not merely imported."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_package import build, smoke, package_fixture, source_fixture


def test_build_explicitly_collects_native_parser_dynamic_dependency_chain(tmp_path, build):
    command = build.build_command(source_fixture(tmp_path), tmp_path/'project/.build/new')
    collected = {command[index+1] for index, value in enumerate(command) if value == '--collect-all'}
    metadata = {command[index+1] for index, value in enumerate(command) if value == '--copy-metadata'}
    assert {'demoparser2', 'polars', '_polars_runtime_32', 'pyarrow', 'tqdm'} <= collected
    assert {'demoparser2', 'polars', 'polars-runtime-32', 'pyarrow', 'tqdm'} <= metadata


@pytest.mark.parametrize('missing', ['polars', '_polars_runtime_32', 'pyarrow', 'tqdm', 'bridge'])
def test_runtime_report_cannot_pass_without_parser_conversion_dependencies(tmp_path, build, smoke, missing):
    package = package_fixture(tmp_path, build)
    def runner(command, **kwargs):
        directory = Path(command[command.index('--data-dir')+1]); directory.mkdir()
        imports = dict.fromkeys(['PySide6', 'demoparser2', 'pandas', 'numpy', 'comtypes',
                                'polars', '_polars_runtime_32', 'pyarrow', 'tqdm'], True)
        if missing != 'bridge': imports.pop(missing)
        report = dict(schema=1, ok=True, frozen=True, gui_started=False, game_started=False,
            network_used=False, sqlite=True, recording_triggered=False, worker_dispatch=['output', 'demo'],
            stage='internal-only', imports=imports, parser_dataframe_bridge=missing != 'bridge',
            assets={name: build.file_sha256(package/'_internal/cs2pov/resources'/name) for name in build.ASSETS})
        (directory/'package-smoke-result.json').write_text(json.dumps(report))
        return SimpleNamespace(returncode=0)
    with pytest.raises(smoke.SmokeError, match='证据'):
        smoke.runtime_smoke(package, tmp_path/'new-data', runner=runner)


def test_source_smoke_performs_real_arrow_polars_pandas_bridge(tmp_path):
    from cs2pov.services.package_smoke import main
    directory = tmp_path/'real-runtime'
    assert main(['--data-dir', str(directory)]) == 0
    report = json.loads((directory/'package-smoke-result.json').read_text())
    assert report['parser_dataframe_bridge'] is True
    assert all(report['imports'][name] is True for name in ('polars', '_polars_runtime_32', 'pyarrow', 'tqdm'))
    assert not report['gui_started'] and not report['game_started'] and not report['recording_triggered']
