"""Explicit original-file parser probes reject missing native dependencies."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from cs2pov.adapters.demo import fingerprint
from cs2pov.services.demo_worker import analysis_digest, CACHE_VERSION
from tests.unit.test_package import build, load_script, package_fixture


@pytest.fixture
def probe():
    return load_script('portable_demo_smoke')


def example_analysis():
    return dict(schema=2, parser='demoparser2-0.42.0', map='de_mirage',
        tick_rate=64, duration=1., timeline=[[10,100],[20,110]], players=[{'id':'101'}],
        rounds=[], deaths=[], kills=[], kill_issues=[])


def write_success(command, *, change=None):
    demo = Path(command[command.index('--demo')+1])
    cache = Path(command[command.index('--cache')+1]); cache.mkdir()
    result = Path(command[command.index('--result')+1])
    before = fingerprint(demo)
    cache_path = cache/(CACHE_VERSION+before['sha256']+'.json')
    analysis = example_analysis()
    cached = dict(fingerprint=before, analysis=analysis, analysis_sha256=analysis_digest(analysis))
    response = dict(cache=str(cache_path), fingerprint=before)
    if change == 'result_hash': response['fingerprint'] = {**before,'sha256':'a'*64}
    elif change == 'wrong_cache': response['cache'] = str(cache.parent/'outside.json')
    elif change == 'cache_hash': cached['analysis_sha256'] = 'b'*64
    elif change == 'empty_timeline':
        analysis['timeline'] = []; cached['analysis_sha256'] = analysis_digest(analysis)
    cache_path.write_text(json.dumps(cached))
    result.write_text(json.dumps(response))
    return SimpleNamespace(returncode=0)


def test_parser_probe_invokes_real_frozen_dispatch_with_original_and_new_cache(tmp_path, build, probe):
    package = package_fixture(tmp_path, build)
    demo = tmp_path/'原比赛.dem'; demo.write_bytes(b'original never copied')
    before = fingerprint(demo)
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        assert 1 <= kwargs['timeout'] <= 120
        assert command[:2] == [str(package/'XiamiPOV.exe'), '--demo-worker']
        assert Path(command[command.index('--demo')+1]) == demo
        assert '--package-smoke' not in command and '--record' not in command
        return write_success(command)
    result = probe.parse_smoke(package, demo, tmp_path/'new-data', runner=runner,
                               expected_sha256=before['sha256'])
    assert result['parser_passed'] and result['timeline_pairs'] == 2 and result['players'] == 1
    assert result['fingerprint_before'] == result['fingerprint_after'] == fingerprint(demo) == before
    assert not result['gui_started'] and not result['game_started'] and not result['recording_triggered']
    assert len(commands) == 1 and len(list(tmp_path.rglob('*.dem'))) == 1
    with pytest.raises(probe._package.SmokeError, match='新的'):
        probe.parse_smoke(package, demo, tmp_path/'new-data', runner=runner)
    assert len(commands) == 1


def test_native_polars_import_error_is_preserved_in_failed_probe(tmp_path, build, probe):
    package = package_fixture(tmp_path, build)
    demo = tmp_path/'原比赛.dem'; demo.write_bytes(b'original')
    def runner(command, **kwargs):
        Path(command[command.index('--result')+1]).write_text(json.dumps({'error':"No module named 'polars'"}))
        return SimpleNamespace(returncode=1)
    with pytest.raises(probe._package.SmokeError, match='polars'):
        probe.parse_smoke(package, demo, tmp_path/'new-data', runner=runner)
    assert demo.read_bytes() == b'original'
    assert 'polars' in (tmp_path/'new-data/demo-result.json').read_text()


@pytest.mark.parametrize('change', ['result_hash','wrong_cache','cache_hash','empty_timeline'])
def test_parser_probe_rejects_unbound_or_incomplete_analysis(tmp_path, build, probe, change):
    package = package_fixture(tmp_path, build)
    demo = tmp_path/'原比赛.dem'; demo.write_bytes(b'original')
    with pytest.raises(probe._package.SmokeError):
        probe.parse_smoke(package, demo, tmp_path/'new-data',
            runner=lambda command, **kwargs: write_success(command, change=change))
    assert demo.read_bytes() == b'original'


def test_original_demo_lease_blocks_child_write_instead_of_restoring_a_changed_original(tmp_path, build, probe):
    package = package_fixture(tmp_path, build)
    demo = tmp_path/'原比赛.dem'; demo.write_bytes(b'original')
    def runner(command, **kwargs):
        demo.write_bytes(b'bad overwrite')
        return SimpleNamespace(returncode=0)
    with pytest.raises(probe._package.SmokeError, match='未完成'):
        probe.parse_smoke(package, demo, tmp_path/'new-data', runner=runner)
    assert demo.read_bytes() == b'original'


def test_expected_demo_hash_failure_prevents_child_and_timeout_is_a_failure(tmp_path, build, probe):
    package = package_fixture(tmp_path, build)
    demo = tmp_path/'原比赛.dem'; demo.write_bytes(b'original')
    def no_child(*_, **__): pytest.fail('wrong original hash must not start a child')
    with pytest.raises(probe._package.SmokeError, match='SHA256'):
        probe.parse_smoke(package, demo, tmp_path/'wrong-hash', runner=no_child, expected_sha256='a'*64)
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    with pytest.raises(probe._package.SmokeError, match='未完成'):
        probe.parse_smoke(package, demo, tmp_path/'timed-out', runner=timeout)
    assert demo.read_bytes() == b'original'
