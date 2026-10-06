from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import pytest
from cs2pov.adapters.vpk import validate_resource, VpkError
from cs2pov.services.telemetry_resource import (BASE_SHA256, PATHS, SCRIPT_PATH,
    build_session_resource, verify_session_resource)
from cs2pov.storage.settings import DataError

BASE = Path('resources/private/minimal-pov/pov.vpk')


def test_session_resource_changes_only_reviewed_script_preserving_lengths(tmp_path):
    first = build_session_resource(BASE,tmp_path/'first.vpk')
    second = build_session_resource(BASE,tmp_path/'second.vpk')
    assert first.nonce!=second.nonce and first.sha256!=second.sha256
    original = validate_resource(BASE,BASE_SHA256,set(PATHS))
    derived = validate_resource(first.path,first.sha256,set(PATHS))
    before={e.path:e.data for e in original.entries}; after={e.path:e.data for e in derived.entries}
    assert len(first.path.read_bytes())==len(BASE.read_bytes())==410376
    for path in PATHS:
        assert len(before[path])==len(after[path])
        if path!=SCRIPT_PATH: assert before[path]==after[path]
    assert first.nonce.encode() in after[SCRIPT_PATH]
    assert b'__CS2POV_SESSION_NONCE__' not in after[SCRIPT_PATH]
    assert b'POV_API ' not in after[SCRIPT_PATH]  # No diagnostic discovery in production.
    assert verify_session_resource(first,first.path)==first.sha256
    assert hashlib.sha256(BASE.read_bytes()).hexdigest()==BASE_SHA256


def test_changed_bytes_or_forged_hash_nonce_path_and_source_are_rejected(tmp_path):
    result=build_session_resource(BASE,tmp_path/'session.vpk',nonce='SESSION_0000000001')
    for forged in (replace(result,sha256='0'*64),replace(result,nonce='OTHER_SESSION_0001'),
                   replace(result,path=tmp_path/'elsewhere.vpk')):
        with pytest.raises((DataError,VpkError)): verify_session_resource(forged,result.path)
    altered=result.path.read_bytes()+b'extra'
    result.path.write_bytes(altered)
    forged=replace(result,sha256=hashlib.sha256(altered).hexdigest())
    with pytest.raises(DataError): verify_session_resource(forged,forged.path)
    bad_base=tmp_path/'bad-base.vpk'; bad_base.write_bytes(BASE.read_bytes()+b'changed')
    with pytest.raises(VpkError): build_session_resource(bad_base,tmp_path/'bad.vpk')
    assert not (tmp_path/'bad.vpk').exists()


@pytest.mark.parametrize('nonce',['short','name";quit_______','x'*65,'é'*16,None])
def test_invalid_nonce_rejected_without_creating_resource(tmp_path,nonce):
    if nonce is None:
        # None selects a cryptographically generated session marker, not literal None.
        result=build_session_resource(BASE,tmp_path/'new.vpk',nonce=nonce)
        assert len(result.nonce)==32
    else:
        with pytest.raises(DataError): build_session_resource(BASE,tmp_path/'new.vpk',nonce=nonce)
        assert not (tmp_path/'new.vpk').exists()


def test_existing_session_resource_is_not_overwritten(tmp_path):
    path=tmp_path/'pov.vpk'; path.write_bytes(b'user file')
    with pytest.raises(DataError,match='覆盖'): build_session_resource(BASE,path)
    assert path.read_bytes()==b'user file'


def test_compact_telemetry_script_has_no_input_audio_network_or_visibility_effects():
    bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node=os.environ.get('CS2POV_NODE_PATH') or shutil.which('node') or str(bundled)
    result=subprocess.run([node,'tests/js/test_replay_telemetry.cjs','src/cs2pov/resources/replay_telemetry.js'],
                          capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'PASS' in result.stdout
