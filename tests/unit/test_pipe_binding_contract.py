import pytest
from cs2pov.adapters.binding_log import BindingLogDecoder
from cs2pov.storage.settings import DataError
from tests.unit.test_binding_log import IDENTITY,NONCE,WALL,feed,response


def test_real_decoder_accepts_only_exact_pipe_self_unbinding_resume():
    command='unbind F8; demo_resume'
    decoder=BindingLogDecoder(IDENTITY,NONCE,expected=command,clock=lambda:100.,wall=lambda:WALL+.25)
    proof=feed(decoder,response(command))
    assert proof.value==command and proof.request_nonce==NONCE


@pytest.mark.parametrize('command',['demo_resume','unbind F8; demo_resume; quit',
    'unbind F8; demo_resume\n','unbind F9; demo_resume','unbind F8; demo_resume;'])
def test_new_pipe_contract_does_not_admit_other_commands(command):
    with pytest.raises(DataError): BindingLogDecoder(IDENTITY,NONCE,expected=command)
