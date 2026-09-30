"""Fail-closed write-ahead backup experiment, not a real-game deployer.

Only directories beneath a caller-selected experiment sandbox are accepted.
This implementation deliberately refuses real CS2 installs. Rust migration
and handle-based race protection are separate gates before real-game writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator


class RecoveryBlocked(RuntimeError):
    pass


class Conflict(RecoveryBlocked):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, fault: Callable[[str], None],
                 temporary: Path | None = None) -> None:
    temporary = temporary or path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        fault('before_replace:' + path.name)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0)
        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    )


def reject_links(path: Path) -> None:
    """Check every existing component, including roots and missing targets."""
    for part in reversed((path, *path.parents)):
        if os.path.lexists(part) and is_link(part):
            raise RecoveryBlocked('Path contains symlink or reparse point: ' + str(part))


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    # An OS-held lock is released by a process crash; the file is never unlinked.
    reject_links(path)
    with path.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RecoveryBlocked('Another transaction is active') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class FakeDirectoryTransaction:
    def __init__(
        self,
        sandbox: Path,
        game_root: Path,
        data_root: Path,
        allowed: set[str],
        fault: Callable[[str], None] | None = None,
    ):
        self.sandbox = sandbox.absolute()
        self.game_root = game_root.absolute()
        self.data_root = data_root.absolute()
        self.allowed = frozenset(allowed)
        self.fault = fault or (lambda _: None)
        for root in (self.sandbox, self.game_root, self.data_root):
            reject_links(root)
        for root in (self.game_root, self.data_root):
            if not root.is_relative_to(self.sandbox) or root == self.sandbox:
                raise RecoveryBlocked('Experiment requires a fake root within sandbox')
        if (self.game_root.is_relative_to(self.data_root)
                or self.data_root.is_relative_to(self.game_root)):
            raise RecoveryBlocked('Game and backup roots must be disjoint')
        if not (self.game_root / '.fake-game').is_file():
            raise RecoveryBlocked('Missing fake-game marker; real-game writes forbidden')
        for name in allowed:
            self._target(name)
        self.sessions = self.data_root / 'sessions'
        self.sessions.mkdir(parents=True, exist_ok=True)

    def _target(self, name: str) -> Path:
        if not isinstance(name, str) or name not in self.allowed:
            raise RecoveryBlocked('Target is not allowlisted')
        parts = PurePosixPath(name).parts
        if (not parts or not re.fullmatch(r'[A-Za-z0-9_./-]+', name)
                or name.startswith('/') or '\\' in name or ':' in name
                or any(part in ('..', '.') for part in parts)
                or any(part.endswith('.') for part in parts)
                or any(part.split('.')[0].upper() in
                       {'CON', 'PRN', 'AUX', 'NUL',
                        *(f'COM{i}' for i in range(1, 10)),
                        *(f'LPT{i}' for i in range(1, 10))} for part in parts)
                or PurePosixPath(name).as_posix() != name):
            raise RecoveryBlocked('Invalid relative target')
        target = self.game_root.joinpath(*parts)
        reject_links(target)
        if not target.parent.is_dir():
            raise RecoveryBlocked('Target parent must already exist')
        if target.exists() and not target.is_file():
            raise RecoveryBlocked('Target is not a regular file')
        return target

    def _save(self, session: Path, manifest: dict) -> None:
        reject_links(session)
        data = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
        atomic_write(session / 'journal.json', data, self.fault)

    def _load(self, session: Path) -> dict:
        reject_links(session)
        if session.parent != self.sessions or len(session.name) != 32:
            raise RecoveryBlocked('Unrecognized session path')
        try:
            int(session.name, 16)
            manifest = json.loads((session / 'journal.json').read_text('utf-8'))
            if (manifest['version'] != 1 or manifest['id'] != session.name
                    or manifest['root'] != str(self.game_root)
                    or manifest['state'] not in ('preparing', 'ready', 'deploying',
                                                  'deployed', 'restoring', 'restored')
                    or not isinstance(manifest['entries'], list)):
                raise ValueError('Invalid manifest identity')
            seen: set[str] = set()
            for entry in manifest['entries']:
                self._target(entry['target'])
                if entry['target'] in seen:
                    raise ValueError('Duplicate target')
                seen.add(entry['target'])
                if type(entry['existed']) is not bool:
                    raise ValueError('Invalid existence flag')
                for key in ('original_hash', 'deployed_hash'):
                    value = entry[key]
                    if value is None and key == 'original_hash' and not entry['existed']:
                        continue
                    if not isinstance(value, str) or len(value) != 64:
                        raise ValueError('Invalid checksum')
                    int(value, 16)
                if entry['backup'] != digest(entry['target'].encode()) + '.bin':
                    raise ValueError('Invalid backup reference')
                if entry['staging'] != '.cs2pov-' + session.name + '-' + entry['backup']:
                    raise ValueError('Invalid staging reference')
                if type(entry['mode']) is not int or not 0 <= entry['mode'] <= 0o7777:
                    raise ValueError('Invalid mode')
            return manifest
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RecoveryBlocked('Missing or corrupt recovery journal') from exc

    def pending(self) -> list[Path]:
        reject_links(self.sessions)
        unfinished = []
        for session in self.sessions.iterdir():
            if session.is_dir():
                # Corrupt journals block, never disappear from recovery discovery.
                manifest = self._load(session)
                if manifest['state'] != 'restored':
                    unfinished.append(session)
        return unfinished

    def deploy(self, changes: dict[str, bytes]) -> Path:
        if not changes or any(not isinstance(value, bytes) for value in changes.values()):
            raise RecoveryBlocked('Changes must be nonempty bytes')
        # Validate ALL paths before creating backups or changing any game file.
        for name in changes:
            self._target(name)
        with exclusive_lock(self.data_root / 'transaction.lock'):
            if self.pending():
                raise RecoveryBlocked('Unfinished recovery prevents a new deployment')
            session = self.sessions / uuid.uuid4().hex
            session.mkdir()
            files = session / 'files'
            files.mkdir()
            manifest = {'version': 1, 'id': session.name, 'root': str(self.game_root),
                        'state': 'preparing', 'entries': []}
            self._save(session, manifest)
            for name, content in changes.items():
                target = self._target(name)
                existed = target.exists()
                original = target.read_bytes() if existed else None
                backup_name = digest(name.encode()) + '.bin'
                if existed:
                    self.fault('backup:' + name)
                    atomic_write(files / backup_name, original, self.fault)
                    if digest((files / backup_name).read_bytes()) != digest(original):
                        raise RecoveryBlocked('Backup verification failed')
                manifest['entries'].append({
                    'target': name, 'existed': existed,
                    'original_hash': digest(original) if existed else None,
                    'deployed_hash': digest(content), 'backup': backup_name,
                    'staging': '.cs2pov-' + session.name + '-' + backup_name,
                    'mode': stat.S_IMODE(target.stat().st_mode) if existed else 0o600,
                })
                self._save(session, manifest)
            manifest['state'] = 'ready'
            self._save(session, manifest)
            self.fault('backup_verified')
            # Every backup is validated again before any game write.
            self._preflight_restore(session, manifest, allow_deployed=False)
            manifest['state'] = 'deploying'
            self._save(session, manifest)
            for entry in manifest['entries']:
                target = self._target(entry['target'])
                self._check_current(target, entry, allow_deployed=False)
                self.fault('deploy:' + entry['target'])
                atomic_write(target, changes[entry['target']], self.fault,
                             target.parent / entry['staging'])
                self.fault('after_deploy:' + entry['target'])
            manifest['state'] = 'deployed'
            self._save(session, manifest)
            return session

    def _check_current(self, target: Path, entry: dict, *, allow_deployed: bool) -> None:
        current = digest(target.read_bytes()) if target.exists() else None
        expected = {entry['original_hash']}
        if allow_deployed:
            expected.add(entry['deployed_hash'])
        if current not in expected:
            raise Conflict('External change preserved: ' + entry['target'])

    def _preflight_restore(self, session: Path, manifest: dict,
                           *, allow_deployed: bool) -> None:
        # Do not restore any target if another backup or target is invalid.
        for entry in manifest['entries']:
            target = self._target(entry['target'])
            if entry['existed']:
                backup = session / 'files' / entry['backup']
                reject_links(backup)
                try:
                    if digest(backup.read_bytes()) != entry['original_hash']:
                        raise RecoveryBlocked('Backup checksum mismatch')
                except OSError as exc:
                    raise RecoveryBlocked('Backup unavailable') from exc
            self._check_current(target, entry, allow_deployed=allow_deployed)
            staging = target.parent / entry['staging']
            reject_links(staging)
            if staging.exists() and digest(staging.read_bytes()) not in {
                    entry['original_hash'], entry['deployed_hash']}:
                raise Conflict('Unknown staged content preserved')

    def restore(self, session: Path) -> None:
        with exclusive_lock(self.data_root / 'transaction.lock'):
            manifest = self._load(session)
            self._preflight_restore(session, manifest, allow_deployed=True)
            manifest['state'] = 'restoring'
            self._save(session, manifest)
            for entry in manifest['entries']:
                target = self._target(entry['target'])
                self._check_current(target, entry, allow_deployed=True)
                staging = target.parent / entry['staging']
                if staging.exists():
                    reject_links(staging)
                    if digest(staging.read_bytes()) not in {
                            entry['original_hash'], entry['deployed_hash']}:
                        raise Conflict('Unknown staged content preserved')
                    staging.unlink()
                self.fault('restore:' + entry['target'])
                if entry['existed']:
                    original = (session / 'files' / entry['backup']).read_bytes()
                    atomic_write(target, original, self.fault, staging)
                    target.chmod(entry['mode'])
                elif target.exists():
                    # Current content was checked against this session's bytes.
                    target.unlink()
                actual = digest(target.read_bytes()) if target.exists() else None
                if actual != entry['original_hash']:
                    raise RecoveryBlocked('Restored content verification failed')
                self.fault('after_restore:' + entry['target'])
            manifest['state'] = 'restored'
            self._save(session, manifest)
