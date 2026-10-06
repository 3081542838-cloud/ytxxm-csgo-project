from dataclasses import replace
from types import SimpleNamespace
import pytest

from cs2pov.adapters.nvidia_status import (ObservationRequest, OverlayNode,
    OverlayWindow, OverlaySnapshot, NvidiaStatusEvidence, classify_snapshot)
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.services.overlay_cycle import OverlayCycle

IDENTITY=ProcessIdentity(123,456,r'C:\Program Files\NVIDIA Corporation\NVIDIA App\CEF\NVIDIA Overlay.exe')


class Monitor:
    busy=False
    def start(self,request,callback):
        if self.busy: return False
        self.busy=True
        self.job=(request,callback)
        return True
    def complete(self,evidence):
        self.busy=False
        self.job[1](evidence)
    def cancel(self): self.busy=False


@pytest.fixture
def env():
    e=SimpleNamespace(now=100.,valid=True,inputs=[],journal=[],results=[],pid=777)
    m=Monitor()
    def send(direction,identity,**kwargs):
        assert identity==IDENTITY and kwargs['input_guard']() is True
        assert e.journal[-1]['actions'][-1]['direction']==direction
        e.inputs.append(direction)
    adapter=SimpleNamespace(send_once=send,foreground_pid=lambda:e.pid,
                            expected_identity=ProcessIdentity(777,888,'game.exe'))
    c=OverlayCycle(m,adapter,guard=lambda:e.valid,checkpoint=lambda v:e.journal.append(v),clock=lambda:e.now)
    r=ObservationRequest('a'*32,IDENTITY.executable,'Alt+F9',e.now)
    c.start(r,e.results.append,identity=IDENTITY,deadline=102.)
    e.cycle,e.monitor,e.request,e.adapter=c,m,r,adapter
    return e


def deliver(e,state='closed',*,change=None):
    r=e.monitor.job[0]
    e.now+=.1
    nodes=[OverlayNode(None,'',50032,False,True,IDENTITY.pid)]
    if state!='closed':
        nodes.extend((OverlayNode(0,'录制',50026,False,True,IDENTITY.pid),
                      OverlayNode(1,'开始' if state=='idle' else '停止',50000,False,True,IDENTITY.pid)))
    s=OverlaySnapshot(r.request_id,r.requested_at,e.now,True,
        (OverlayWindow(999,IDENTITY,True,tuple(nodes)),),'')
    if change: s=change(s)
    p=classify_snapshot(s,r,now=e.now,identity_query=lambda _:IDENTITY)
    e.monitor.complete(p)
    return p


@pytest.mark.parametrize('state',['idle','recording'])
def test_closed_tree_never_authorizes_recording_but_exact_new_open_state_does(env,state):
    initial_nonce=env.monitor.job[0].request_id
    deliver(env)
    assert env.inputs==['open'] and not env.results
    assert env.monitor.job[0].request_id==env.request.request_id!=initial_nonce
    proof=deliver(env,state)
    assert env.inputs==['open','close'] and not env.results
    assert env.monitor.job[0].request_id not in (initial_nonce,env.request.request_id)
    deliver(env)
    assert env.results==[proof] and env.results[0].state==state
    assert not env.cycle.active and not env.cycle.failed


def test_already_open_requires_fresh_authorization_read_before_closing(env):
    deliver(env,'idle')
    assert not env.inputs and not env.results
    proof=deliver(env,'idle')
    assert env.inputs==['close']
    deliver(env)
    assert env.results==[proof]


@pytest.mark.parametrize('phase',['initial','opened','closed'])
@pytest.mark.parametrize('fault',['expired','contract','different_pid','incomplete'])
def test_phase_failure_never_retries_visibility_or_provides_record_authority(env,phase,fault):
    if phase in ('opened','closed'): deliver(env)
    if phase=='closed': deliver(env,'idle')
    inputs=list(env.inputs)
    change=None
    if fault=='expired': env.now=102.
    if fault=='contract': env.valid=False
    if fault=='different_pid':
        change=lambda s:replace(s,windows=(replace(s.windows[0],identity=replace(IDENTITY,pid=124)),))
    if fault=='incomplete': change=lambda s:replace(s,complete=False,reason='tree_limit')
    deliver(env,'idle' if phase=='opened' else 'closed',change=change)
    assert env.inputs==inputs and env.results[-1].state=='unknown'
    assert env.cycle.failed and not env.cycle.active
    assert not env.cycle.start(env.request,env.results.append,identity=IDENTITY,deadline=200)


def test_close_not_observed_blocks_play_even_if_state_is_exact(env):
    deliver(env)
    deliver(env,'recording')
    deliver(env,'recording')
    assert env.results[-1].state=='unknown' and env.inputs==['open','close']


def test_unknown_nonempty_page_does_not_toggle_or_claim_idle(env):
    deliver(env,'idle',change=lambda s:replace(s,windows=(replace(s.windows[0],nodes=(
        s.windows[0].nodes[0],OverlayNode(0,'设置',50000,False,True,IDENTITY.pid))),)))
    assert not env.inputs and env.results[-1].state=='unknown'


def test_checkpoint_failure_consumes_open_without_sending_or_retry(env):
    def fail(v):
        if v['actions']: raise OSError('disk full')
    env.cycle.checkpoint=fail
    deliver(env)
    assert env.cycle.actions[0]['direction']=='open' and not env.inputs
    assert env.results[-1].state=='unknown' and env.cycle.failed


def test_observation_checkpoint_failure_blocks_before_any_consumption(env):
    def fail(v): raise OSError('disk full')
    env.cycle.checkpoint=fail
    deliver(env)
    assert not env.cycle.actions and not env.inputs
    assert env.results[-1].state=='unknown' and env.cycle.failed


def test_cancelled_callback_cannot_deliver_or_open(env):
    env.cycle.cancel()
    deliver(env)
    assert not env.inputs and not env.results


def test_return_to_other_foreground_blocks_result(env):
    deliver(env)
    deliver(env,'idle')
    env.pid=555
    deliver(env)
    assert env.results[-1].state=='unknown'


def test_after_checkpoint_contract_change_prevents_input(env):
    def revoke(v): env.valid=False
    env.cycle.checkpoint=revoke
    deliver(env)
    assert not env.inputs and env.results[-1].state=='unknown'


def scaffold(snapshot):
    rows=((None,50032,False,True),(0,50033,False,False),
          (1,50030,False,True),(2,50026,False,True),(3,50026,False,True),
          (4,50026,True,True),(5,50020,True,True),(4,50026,False,True),(3,50026,True,True))
    nodes=tuple(OverlayNode(parent,'',kind,offscreen,enabled,IDENTITY.pid)
                for parent,kind,offscreen,enabled in rows)
    return replace(snapshot,windows=(replace(snapshot.windows[0],nodes=nodes),))


def test_actual_cold_closed_scaffold_only_opens_never_confirms_recording(env):
    deliver(env,change=scaffold)
    assert env.inputs==['open'] and not env.results
    proof=deliver(env,'idle')
    deliver(env,change=scaffold)
    assert env.results==[proof] and env.inputs==['open','close']


@pytest.mark.parametrize('index,field,value',[(7,'control_type',50000),
    (7,'name','开始'),(5,'offscreen',False),(8,'parent',4),(1,'enabled',True),
    (7,'process_id',124)])
def test_altered_scaffolding_does_not_authorize_visibility_or_idle(env,index,field,value):
    def altered(s):
        s=scaffold(s)
        nodes=list(s.windows[0].nodes)
        nodes[index]=replace(nodes[index],**{field:value})
        return replace(s,windows=(replace(s.windows[0],nodes=tuple(nodes)),))
    deliver(env,change=altered)
    assert not env.inputs and env.results[-1].state=='unknown'
