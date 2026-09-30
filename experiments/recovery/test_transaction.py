from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.recovery.transaction import (
    Conflict, FakeDirectoryTransaction, RecoveryBlocked, exclusive_lock,
)


class Crash(BaseException):
    pass


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cs2pov-fake-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.game = self.base / 'fake-game'
        self.game.mkdir()
        (self.game / '.fake-game').write_text('EXPERIMENT ONLY')
        (self.game / 'cfg').mkdir()
        (self.game / 'gameinfo.gi').write_bytes(b'original gameinfo\r\n')
        (self.game / 'cfg/player.cfg').write_bytes(b'original user settings\r\n')
        self.original = {name: (self.game / name).read_bytes()
                         for name in ('gameinfo.gi', 'cfg/player.cfg')}
        self.allowed = {'gameinfo.gi', 'cfg/player.cfg', 'pov.vpk'}
        self.data = self.base / 'data'
        self.tx = self.make_tx()
        self.changes = {'gameinfo.gi': b'patched gameinfo',
                        'cfg/player.cfg': b'changed config', 'pov.vpk': b'HUD resource'}

    def make_tx(self, fault=None):
        return FakeDirectoryTransaction(self.base, self.game, self.data,
                                        self.allowed, fault)

    def assert_original(self):
        for name, original in self.original.items():
            self.assertEqual((self.game / name).read_bytes(), original)
        self.assertFalse((self.game / 'pov.vpk').exists())

    def only_session(self):
        sessions = list(self.tx.sessions.iterdir())
        self.assertEqual(len(sessions), 1)
        return sessions[0]

    def test_normal_restore_is_byte_exact_and_idempotent(self):
        session = self.tx.deploy(self.changes)
        self.assertEqual(len(self.tx.pending()), 1)
        self.tx.restore(session)
        self.tx.restore(session)
        self.assert_original()
        self.assertEqual(self.tx.pending(), [])

    def test_existing_same_named_hud_is_preserved(self):
        (self.game / 'pov.vpk').write_bytes(b'user existing hud')
        session = self.tx.deploy(self.changes)
        self.tx.restore(session)
        self.assertEqual((self.game / 'pov.vpk').read_bytes(), b'user existing hud')

    def test_unknown_target_rejected_before_any_write(self):
        with self.assertRaises(RecoveryBlocked):
            self.tx.deploy({'gameinfo.gi': b'change', 'cs2.exe': b'bad'})
        self.assert_original()
        self.assertEqual(list(self.tx.sessions.iterdir()), [])

    def test_traversal_and_command_like_paths_rejected(self):
        for name in ('../escape', '/escape', 'cfg/../gameinfo.gi', 'cfg\\player.cfg',
                     'C:/escape', 'gameinfo.gi:stream', 'gameinfo.gi;connect x',
                     'NUL', 'cfg/COM1.cfg', 'gameinfo.gi.'):
            with self.subTest(name=name), self.assertRaises(RecoveryBlocked):
                FakeDirectoryTransaction(self.base, self.game, self.data, {name})

    def test_real_game_or_missing_marker_is_rejected(self):
        (self.game / '.fake-game').unlink()
        with self.assertRaises(RecoveryBlocked):
            self.make_tx()

    def test_root_outside_sandbox_is_rejected(self):
        with self.assertRaises(RecoveryBlocked):
            FakeDirectoryTransaction(self.base / 'nested', self.game, self.data,
                                     self.allowed)

    def test_overlapping_backup_and_game_roots_are_rejected(self):
        with self.assertRaises(RecoveryBlocked):
            FakeDirectoryTransaction(self.base, self.game, self.game / 'backup',
                                     self.allowed)

    def test_interrupted_backups_never_modify_game(self):
        def fault(point):
            if point == 'backup:cfg/player.cfg':
                raise OSError('simulated disk full')
        with self.assertRaises(OSError):
            self.make_tx(fault).deploy(self.changes)
        self.assert_original()
        with self.assertRaises(RecoveryBlocked):
            self.tx.deploy(self.changes)
        self.tx.restore(self.only_session())
        self.assert_original()

    def test_atomic_replace_failure_preserves_original(self):
        def fault(point):
            if point == 'before_replace:gameinfo.gi':
                raise PermissionError('simulated replacement denied')
        with self.assertRaises(PermissionError):
            self.make_tx(fault).deploy(self.changes)
        self.assert_original()
        self.tx.restore(self.only_session())
        self.assertEqual(list(self.game.glob('*.tmp-*')), [])

    def test_process_crash_after_partial_deployment_recovers_on_restart(self):
        script = '''
import os, sys
from pathlib import Path
from experiments.recovery.transaction import FakeDirectoryTransaction
b = Path(sys.argv[1])
def fault(p):
    if p == 'after_deploy:gameinfo.gi': os._exit(73)
t = FakeDirectoryTransaction(b,b/'fake-game',b/'data',
    {'gameinfo.gi','cfg/player.cfg','pov.vpk'},fault)
t.deploy({'gameinfo.gi':b'patched gameinfo','cfg/player.cfg':b'changed config',
          'pov.vpk':b'HUD resource'})
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.base)],
                                cwd=Path(__file__).resolve().parents[2], timeout=20)
        self.assertEqual(result.returncode, 73)
        restarted = self.make_tx()
        self.assertEqual(len(restarted.pending()), 1)
        restarted.restore(restarted.pending()[0])
        self.assert_original()

    def test_restore_interruption_is_discovered_and_retryable(self):
        session = self.tx.deploy(self.changes)
        def fault(point):
            if point == 'after_restore:gameinfo.gi':
                raise Crash('application terminated')
        with self.assertRaises(Crash):
            self.make_tx(fault).restore(session)
        self.assertEqual(len(self.make_tx().pending()), 1)
        self.make_tx().restore(session)
        self.assert_original()

    def test_external_changes_are_preserved_and_block_all_restore(self):
        session = self.tx.deploy(self.changes)
        (self.game / 'cfg/player.cfg').write_bytes(b'new user setting')
        with self.assertRaises(Conflict):
            self.tx.restore(session)
        self.assertEqual((self.game / 'gameinfo.gi').read_bytes(), b'patched gameinfo')
        self.assertEqual((self.game / 'cfg/player.cfg').read_bytes(), b'new user setting')
        self.assertEqual(len(self.tx.pending()), 1)

    def test_external_replacement_of_created_file_is_not_deleted(self):
        session = self.tx.deploy(self.changes)
        (self.game / 'pov.vpk').write_bytes(b'unrelated file')
        with self.assertRaises(Conflict):
            self.tx.restore(session)
        self.assertEqual((self.game / 'pov.vpk').read_bytes(), b'unrelated file')

    def test_corrupt_backup_does_not_overwrite_any_game_file(self):
        session = self.tx.deploy(self.changes)
        backup = next((session / 'files').iterdir())
        backup.write_bytes(b'corrupt backup')
        with self.assertRaises(RecoveryBlocked):
            self.tx.restore(session)
        self.assertEqual((self.game / 'gameinfo.gi').read_bytes(), b'patched gameinfo')

    def test_missing_backup_blocks_restore(self):
        session = self.tx.deploy(self.changes)
        next((session / 'files').iterdir()).unlink()
        with self.assertRaises(RecoveryBlocked):
            self.tx.restore(session)

    def test_corrupt_journal_blocks_discovery_and_new_deployment(self):
        session = self.tx.deploy(self.changes)
        (session / 'journal.json').write_text('{truncated')
        with self.assertRaises(RecoveryBlocked):
            self.tx.pending()
        with self.assertRaises(RecoveryBlocked):
            self.tx.deploy(self.changes)

    def test_manifest_target_tampering_is_rejected(self):
        session = self.tx.deploy(self.changes)
        journal = session / 'journal.json'
        manifest = json.loads(journal.read_text(encoding='utf-8'))
        manifest['entries'][0]['target'] = '../outside'
        journal.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(RecoveryBlocked):
            self.tx.restore(session)

    def test_manifest_backup_path_tampering_is_rejected(self):
        session = self.tx.deploy(self.changes)
        journal = session / 'journal.json'
        manifest = json.loads(journal.read_text(encoding='utf-8'))
        manifest['entries'][0]['backup'] = '../../../outside'
        journal.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(RecoveryBlocked):
            self.tx.restore(session)

    def test_process_crash_before_replace_cleans_declared_staging_file(self):
        script = '''
import os,sys
from pathlib import Path
from experiments.recovery.transaction import FakeDirectoryTransaction
b=Path(sys.argv[1])
def fault(p):
    if p == 'before_replace:gameinfo.gi': os._exit(74)
t=FakeDirectoryTransaction(b,b/'fake-game',b/'data',{'gameinfo.gi'},fault)
t.deploy({'gameinfo.gi':b'new content'})
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.base)],
                                cwd=Path(__file__).resolve().parents[2], timeout=20)
        self.assertEqual(result.returncode, 74)
        self.assertEqual(len(list(self.game.glob('.cs2pov-*'))), 1)
        self.make_tx().restore(self.only_session())
        self.assert_original()
        self.assertEqual(list(self.game.glob('.cs2pov-*')), [])

    def test_unknown_staging_content_is_not_deleted(self):
        session = self.tx.deploy(self.changes)
        entry = json.loads((session / 'journal.json').read_text('utf-8'))['entries'][0]
        staging = self.game / entry['staging']
        staging.write_bytes(b'external or incomplete bytes')
        with self.assertRaises(Conflict):
            self.tx.restore(session)
        self.assertEqual(staging.read_bytes(), b'external or incomplete bytes')

    def test_game_folder_permission_failure_retains_backup_and_original(self):
        def fault(point):
            if point == 'deploy:gameinfo.gi':
                raise PermissionError('simulated game directory access denied')
        with self.assertRaises(PermissionError):
            self.make_tx(fault).deploy(self.changes)
        self.assert_original()
        self.assertTrue((self.only_session() / 'files').is_dir())
        self.tx.restore(self.only_session())
        self.assert_original()

    def test_same_process_concurrent_transaction_is_blocked(self):
        with exclusive_lock(self.data / 'transaction.lock'):
            with self.assertRaises(RecoveryBlocked):
                self.tx.deploy(self.changes)
        self.assert_original()

    def test_windows_directory_junction_blocks_writes(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'player.cfg').write_bytes(b'outside original')
        link = self.game / 'linked'
        if os.name == 'nt':
            result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link),
                                     str(outside)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        with self.assertRaises(RecoveryBlocked):
            FakeDirectoryTransaction(self.base, self.game, self.data,
                                     {'linked/player.cfg'})
        self.assertEqual((outside / 'player.cfg').read_bytes(), b'outside original')

    def test_reparse_point_introduced_after_backup_is_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        def fault(point):
            if point == 'backup_verified':
                original_cfg = self.game / 'cfg'
                original_cfg.rename(self.game / 'cfg-moved')
                if os.name == 'nt':
                    result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J',
                        str(original_cfg), str(outside)], capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    original_cfg.symlink_to(outside, target_is_directory=True)
                self.addCleanup(lambda: os.rmdir(original_cfg)
                                if os.path.lexists(original_cfg) else None)
        with self.assertRaises(RecoveryBlocked):
            self.make_tx(fault).deploy(self.changes)
        self.assertEqual((self.game / 'gameinfo.gi').read_bytes(), self.original['gameinfo.gi'])
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
