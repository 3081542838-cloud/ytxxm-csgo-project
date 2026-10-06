from types import SimpleNamespace
import pytest
from cs2pov.adapters.pipe_console import PipeConsole
from cs2pov.services.replay_preparation import ReplayPreparation
from cs2pov.services.replay import ReplayError

def preparation(console, pairs=None):
    analysis={'tick_rate':64,'timeline':pairs or [[i,i+3332] for i in range(1,401)]}
    draft={'selection':{'start_tick':257,'server_start_tick':3589,
        'end_tick':321,'server_end_tick':3653}}
    return ReplayPreparation(SimpleNamespace(console=console),analysis,draft)

def test_pipe_leadin_has_render_budget_without_changing_cut_or_exact_mapping():
    session=preparation(object.__new__(PipeConsole))
    assert session._lead_in()==65
    assert session.draft['selection']=={'start_tick':257,'server_start_tick':3589,
        'end_tick':321,'server_end_tick':3653}
    assert session.timeout==20
    assert preparation(object())._lead_in()==193

def test_larger_budget_still_rejects_mapping_discontinuity():
    pairs=[[65,3200],[257,3589],[321,3653]]
    with pytest.raises(ReplayError,match='映射中断'):
        preparation(object.__new__(PipeConsole),pairs)._lead_in()
