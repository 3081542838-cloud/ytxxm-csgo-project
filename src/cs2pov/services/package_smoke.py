"""Explicit headless package verification; never starts the normal app."""
import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--hud', type=Path)
    args = parser.parse_args(argv)
    from cs2pov.storage.transaction import atomic_write, no_redirection
    from cs2pov.storage.library import Library
    no_redirection(args.data_dir)
    if args.data_dir.exists() and any(args.data_dir.iterdir()):
        raise RuntimeError('包检查只允许全新的独立数据目录。')
    args.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        import PySide6, demoparser2, pandas, numpy, comtypes, polars, pyarrow, tqdm
        import _polars_runtime_32
        # Exercise the actual dataframe bridge required by the Rust parser,
        # not just its importable extension or parse_header path.
        rows = polars.from_arrow(pyarrow.table({'tick': [1, 2], 'player': ['a', 'b']})).to_pandas().to_dict('records')
        if rows != [{'tick': 1, 'player': 'a'}, {'tick': 2, 'player': 'b'}]:
            raise RuntimeError('解析器 Arrow / Polars / pandas 数据转换未通过。')
        from cs2pov.services import output_worker, demo_worker
        from cs2pov.adapters.worker_job import contain_current_worker
        from cs2pov.ui.window import ICON, MainWindow
        base = Path(__file__).resolve().parents[1] / 'resources'
        assets = {}
        for name in ('shrimp.ico', 'replay_telemetry.js', 'pov_visibility.js'):
            path = base / name
            no_redirection(path)
            if not path.is_file() or path.stat().st_size == 0: raise RuntimeError('缺少包资源：' + name)
            assets[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        library = Library(args.data_dir / 'library.sqlite')
        try:
            assert library.records() == [] and library.unfinished() == []
        finally:
            library.close()
        session_hud = None
        if args.hud is not None:
            from cs2pov.services.telemetry_resource import build_session_resource, verify_session_resource
            first = build_session_resource(args.hud, args.data_dir / 'session-first.vpk')
            second = build_session_resource(args.hud, args.data_dir / 'session-second.vpk')
            assert first.nonce != second.nonce and first.sha256 != second.sha256
            assert verify_session_resource(first, first.path) == first.sha256
            assert verify_session_resource(second, second.path) == second.sha256
            native = build_session_resource(args.hud, args.data_dir / 'session-native.vpk', native_radar=True)
            assert native.native_radar is True and native.radar is None
            assert verify_session_resource(native, native.path) == native.sha256
            assert native.path.stat().st_size == first.path.stat().st_size
            from cs2pov.storage.settings import HudPreset, Presets
            from dataclasses import replace
            presets = Presets(args.data_dir)
            presets.save(replace(HudPreset(), show_radar=True))
            assert Presets(args.data_dir).items[0].show_radar is True
            session_hud = dict(ok=True, distinct_sessions=True, sha256=first.sha256,
                               native_radar=True, native_sha256=native.sha256)
        result = dict(schema=1, ok=True, frozen=bool(getattr(sys, 'frozen', False)),
                      runtime={name: version(name) for name in ('PySide6-Essentials', 'demoparser2', 'comtypes', 'pandas', 'numpy',
                               'polars', 'polars-runtime-32', 'pyarrow', 'tqdm')},
                      assets=assets, worker_dispatch=['output', 'demo'],
                      stage='internal-only', game_started=False, recording_triggered=False,
                      gui_started=False, network_used=False, sqlite=True,
                      parser_dataframe_bridge=True,
                      session_hud=session_hud,
                      imports={name: True for name in ('PySide6', 'demoparser2', 'pandas', 'numpy', 'comtypes',
                               'polars', '_polars_runtime_32', 'pyarrow', 'tqdm')})
        code = 0
    except Exception as error:
        result, code = {'schema': 1, 'ok': False, 'error': str(error)}, 1
    atomic_write(args.data_dir / 'package-smoke-result.json', json.dumps(result, ensure_ascii=True).encode())
    if sys.stdout is not None: print(json.dumps(result, ensure_ascii=True), flush=True)
    return code
