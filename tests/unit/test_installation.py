"""Stage 0 rules, runnable with stdlib while GUI dependencies download."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from cs2pov.adapters.installation import InstallationError, inspect_installation


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "CS2 安装"
        self.exe = self.root / "game/bin/win64/cs2.exe"
        self.cfg = self.root / "game/csgo/gameinfo.gi"
        self.info = self.cfg.with_name("steam.inf")
        self.exe.parent.mkdir(parents=True)
        self.cfg.parent.mkdir(parents=True)
        self.exe.write_bytes(b"test fixture, never executed")
        self.cfg.write_text('SearchPaths\n{\n Game csgo\n}\n', encoding="utf-8")
        self.info.write_text('appID=730\nPatchVersion=1.41.8.6\n', encoding="utf-8")

    def test_valid_snapshot_is_read_only_and_reports_actual_hash(self):
        before = self.cfg.read_bytes()
        snapshot = inspect_installation(self.root)
        self.assertEqual(snapshot.patch_version, "1.41.8.6")
        self.assertEqual(snapshot.gameinfo_sha256, hashlib.sha256(before).hexdigest())
        self.assertFalse(snapshot.has_pov_search_path)
        self.assertFalse(snapshot.has_pov_file)
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.cfg.parent.iterdir()), ["gameinfo.gi", "steam.inf"])

    def test_missing_path_or_game_file_is_rejected(self):
        with self.assertRaises(InstallationError):
            inspect_installation(self.root / "missing")
        self.exe.unlink()
        with self.assertRaisesRegex(InstallationError, "必要文件"):
            inspect_installation(self.root)

    def test_wrong_game_id_is_rejected(self):
        self.info.write_text('appID=440\n', encoding="utf-8")
        with self.assertRaisesRegex(InstallationError, "appID"):
            inspect_installation(self.root)

    def test_invalid_encoding_and_missing_searchpaths_are_rejected(self):
        self.cfg.write_bytes(b"\xff\xfe\x80")
        with self.assertRaises(InstallationError):
            inspect_installation(self.root)
        self.cfg.write_text("not a gameinfo", encoding="utf-8")
        with self.assertRaisesRegex(InstallationError, "SearchPaths"):
            inspect_installation(self.root)

    def test_pov_reference_and_existing_file_are_reported(self):
        for line in ('Game csgo/pov.vpk', '"Game" "csgo\\pov.vpk"', 'GAME csgo/POV.vpk // note'):
            with self.subTest(line=line):
                self.cfg.write_text(f'SearchPaths\n{{\n {line}\n}}', encoding="utf-8")
                self.assertTrue(inspect_installation(self.root).has_pov_search_path)
        self.cfg.with_name("pov.vpk").write_bytes(b"existing user file")
        self.assertTrue(inspect_installation(self.root).has_pov_file)
        self.assertEqual(self.cfg.with_name("pov.vpk").read_bytes(), b"existing user file")

    def test_comment_is_not_reported_as_active_pov_reference(self):
        self.cfg.write_text('SearchPaths\n{\n // Game csgo/pov.vpk\n Game csgo\n}', encoding="utf-8")
        self.assertFalse(inspect_installation(self.root).has_pov_search_path)
        self.info.write_text('appID=730\n', encoding="utf-8")
        self.assertIsNone(inspect_installation(self.root).patch_version)


if __name__ == "__main__":
    unittest.main()
