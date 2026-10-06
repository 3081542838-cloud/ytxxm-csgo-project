import copy
from dataclasses import asdict
import json

import pytest

from cs2pov.adapters.demo import fingerprint
from cs2pov.services.clips import selection
from cs2pov.services.demo_worker import CACHE_VERSION, analysis_digest
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError, HudPreset


@pytest.fixture
def prepared_record(tmp_path, qtbot):
    model = Workspace(tmp_path / 'data')
    demo = tmp_path / '原始 Demo.dem'
    demo.write_bytes(b'original-demo-read-only')
    fp = fingerprint(demo)
    analysis = {'schema': 2, 'parser': 'demoparser2-0.42.0', 'map': 'de_test',
                'tick_rate': 64, 'duration': 2,
                'timeline': [[t, t + 3332] for t in range(100, 121)],
                'players': [{'id': '202', 'name': '虾米', 'aliases': [], 'alive': [[100, 120]]}],
                'rounds': [{'id': 1, 'number': 1, 'start': 100, 'end': 110, 'complete': True}],
                'deaths': [], 'kills': [], 'kill_issues': []}
    clip = selection(analysis, '202', 'round', round_id=1)
    draft = {'demo': str(demo), 'fingerprint': fp, 'map': analysis['map'],
             'selection': {**clip, 'hud': asdict(HudPreset()), 'content_sha256': fp['sha256']}}
    cache = model.directory / 'cache' / (CACHE_VERSION + fp['sha256'] + '.json')
    cache.write_text(json.dumps({'fingerprint': fp, 'analysis': analysis,
                               'analysis_sha256': analysis_digest(analysis)}), encoding='utf-8')
    model.library.save_record('saved', {'draft_snapshot': draft})
    model.library.save_draft({'demo': 'previous', 'status': 'awaiting_parse'})
    yield model, draft, analysis, cache, demo
    model.library.close()


def test_reprepare_restores_only_exact_draft_and_does_not_launch(prepared_record):
    model, draft, analysis, _, demo = prepared_record
    old_bytes = demo.read_bytes()
    result = model.reprepare_record('saved')
    assert result['selection'] == draft['selection']
    assert result['status'] == 'parsed' and model.analysis == analysis
    assert model.library.draft() == result
    assert model._preview is None and not model.busy
    assert demo.read_bytes() == old_bytes and model.presets.default == 'builtin'


@pytest.mark.parametrize('failure', ['changed_demo', 'missing_cache', 'bad_digest', 'cache_identity',
                                    'selection_tick', 'wrong_map', 'wrong_hud', 'wrong_hash',
                                    'missing_record', 'busy', 'pending_recovery'])
def test_reprepare_failure_preserves_current_draft_and_analysis(prepared_record, failure):
    model, draft, _, cache, demo = prepared_record
    original = model.library.draft()
    sentinel = {'old': 'analysis'}
    model.analysis = sentinel
    payload = copy.deepcopy(draft)
    if failure == 'changed_demo':
        demo.write_bytes(b'changed')
    elif failure == 'missing_cache':
        cache.rename(cache.with_suffix('.unavailable'))
    elif failure in ('bad_digest', 'cache_identity'):
        raw = json.loads(cache.read_text(encoding='utf-8'))
        if failure == 'bad_digest':
            raw['analysis_sha256'] = '0' * 64
        else:
            raw['fingerprint']['sha256'] = '0' * 64
        cache.write_text(json.dumps(raw), encoding='utf-8')
    elif failure == 'selection_tick':
        payload['selection']['end_tick'] -= 1
    elif failure == 'wrong_map':
        payload['map'] = 'de_other'
    elif failure == 'wrong_hud':
        payload['selection']['hud']['hud_scale'] = 9
    elif failure == 'wrong_hash':
        payload['selection']['content_sha256'] = '0' * 64
    elif failure == 'missing_record':
        model.library.remove_record('saved')
    elif failure == 'busy':
        model.busy = True
    else:
        model.library.db.execute("INSERT INTO sessions VALUES ('unfinished', 'recording')")
        model.library.db.commit()
    if failure != 'missing_record':
        model.library.save_record('saved', {'draft_snapshot': payload})
    with pytest.raises(DataError):
        model.reprepare_record('saved')
    assert model.library.draft() == original
    assert model.analysis is sentinel and model._preview is None
