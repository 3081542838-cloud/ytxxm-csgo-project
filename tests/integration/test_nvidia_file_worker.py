"""Frozen-style file transport tests use synthetic child data, never input."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cs2pov.adapters import nvidia_status as nv
from cs2pov.adapters.owned_process import ProcessIdentity

EXE = r'C:\Program Files\NVIDIA Corporation\NVIDIA App\CEF\NVIDIA Overlay.exe'

@pytest.mark.parametrize('age', [-.01, 2.001])
def test_preissued_request_age_prevents_any_child(age):
    spawned = []
    request = nv.ObservationRequest('a'*32, EXE, 'Alt+F9', 10-age)
    evidence = nv.observe_nvidia_status(EXE, request=request, clock=lambda:10,
        popen=lambda *a, **k: spawned.append(a))
    assert evidence.state == 'unknown' and not spawned

@pytest.mark.parametrize('change', ['hotkey','exe'])
def test_preissued_request_scope_cannot_be_replaced(change):
    spawned=[]
    request=nv.ObservationRequest('a'*32, EXE if change=='hotkey' else r'D:\NVIDIA Overlay.exe',
        'Alt+F10' if change=='hotkey' else 'Alt+F9', 10)
    evidence=nv.observe_nvidia_status(EXE,request=request,clock=lambda:10,
        popen=lambda *a,**k:spawned.append(a))
    assert evidence.state=='unknown' and not spawned

def test_windowed_file_transport_binds_nonce_without_standard_streams(monkeypatch):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    code='''import json, sys, time
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text())
v={'request_id':r['request_id'],'started_at':r['requested_at'],
   'observed_at':time.monotonic(),'complete':True,'reason':'','windows':[
   {'hwnd':123,'visible':True,'identity':{'pid':42,'created':123,'executable':r['executable']},
    'nodes':[{'parent':None,'name':'NVIDIA Overlay','control_type':50032,'offscreen':False,'enabled':True,'process_id':42},
     {'parent':0,'name':'Record','control_type':50026,'offscreen':False,'enabled':True,'process_id':42},
     {'parent':1,'name':'Start','control_type':50000,'offscreen':False,'enabled':True,'process_id':42}]}]}
Path(sys.argv[2]).write_text(json.dumps(v))
'''
    captured=[]
    def spawn(argv,**kwargs):
        captured.append((argv,kwargs))
        return subprocess.Popen([sys.executable,'-c',code,
            argv[argv.index('--request')+1],argv[argv.index('--result')+1]],**kwargs)
    request=nv.ObservationRequest('a'*32,EXE,'Alt+F9',time.monotonic())
    evidence=nv.observe_nvidia_status(EXE,request=request,raw_view=True,popen=spawn,
        identity_query=lambda pid:ProcessIdentity(42,123,EXE))
    assert evidence.state=='idle' and evidence.request_id==request.request_id
    assert captured[0][1]['stdin']==subprocess.DEVNULL
    assert captured[0][1]['stdout']==subprocess.DEVNULL
    assert not Path(captured[0][0][-1]).exists()  # owned temp reclaimed

def test_duplicate_file_request_is_rejected_and_existing_result_preserved(tmp_path):
    request,result=tmp_path/'r.json',tmp_path/'s.json'
    request.write_text('{"request_id":"a","request_id":"b"}')
    result.write_text('keep')
    env=__import__('os').environ.copy()
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[2]/'src')
    child=subprocess.run([sys.executable,'-m','cs2pov.adapters.nvidia_status','--raw-worker',
        '--request',str(request),'--result',str(result)],env=env,capture_output=True,timeout=5)
    assert child.returncode==2 and result.read_text()=='keep'
