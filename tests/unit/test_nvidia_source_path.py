import os
from pathlib import Path
from cs2pov.adapters import nvidia_status


def test_source_observer_child_receives_exact_product_import_root(monkeypatch):
    captured={}
    class Process:
        returncode=0
        def communicate(self,*args,**kwargs): return b'{}',b''
        def poll(self): return 0
    def popen(argv,**kwargs):
        captured.update(argv=argv,env=kwargs['env'])
        return Process()
    monkeypatch.delenv('PYTHONPATH',raising=False)
    nvidia_status.observe_nvidia_status(r'C:\Program Files\NVIDIA Corporation\NVIDIA App\CEF\NVIDIA Overlay.exe',popen=popen)
    assert captured['env']['PYTHONPATH']==str(Path(nvidia_status.__file__).resolve().parents[2])
    assert 'PYTHONPATH' not in os.environ
