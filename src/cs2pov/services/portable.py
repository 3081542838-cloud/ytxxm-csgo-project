"""Explicit portable profiles; packaged resources are verified by LocalResources.

A marker next to a frozen executable opts into a neighbouring data directory.
No private paths, task history or recovery material are copied implicitly.
"""
from dataclasses import asdict, replace
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from cs2pov.storage.settings import DataError, HudPreset, JsonFile, Presets, Settings
from cs2pov.storage.transaction import no_redirection, pending_sessions


class PortableError(DataError):
    pass


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise PortableError('便携配置包含重复字段。')
        result[key] = value
    return result


def _constant(_value):
    raise PortableError('便携配置包含无效数字。')


def _read_json(path, decode, *, limit=1_048_576):
    no_redirection(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise PortableError(f'{path.name} 不是可读取的有界配置文件。')
    with path.open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise PortableError(f'{path.name} 超过配置大小限制。')
    try:
        raw = json.loads(content.decode('utf-8'), object_pairs_hook=_pairs,
                         parse_constant=_constant)
        return decode(raw)
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise PortableError(f'{path.name} 无法读取；原文件保留。') from error


def portable_root(executable=None, frozen=None):
    """Return the opted-in executable directory, never its extraction directory."""
    if frozen is None:
        frozen = getattr(sys, 'frozen', False)
    if not frozen:
        return None
    executable = Path(sys.executable if executable is None else executable).absolute()
    if '..' in executable.parts:
        raise PortableError('便携程序路径不能包含上级跳转。')
    no_redirection(executable)
    root = executable.parent
    marker = root / 'portable.json'
    no_redirection(marker)
    if not marker.exists():
        return None

    def decode(raw):
        if (type(raw) is not dict or set(raw) != {'schema', 'application'}
                or type(raw['schema']) is not int or raw['schema'] != 1
                or raw['application'] != 'XiamiPOV'):
            raise PortableError('portable.json 的版本或应用标识无效，请重新解压完整便携包。')
        return raw

    _read_json(marker, decode, limit=32768)
    return root


def data_directory(explicit=None, *, executable=None, frozen=None, environ=None):
    """The explicit CLI override remains first, including for portable builds."""
    if explicit is not None:
        return Path(explicit)
    root = portable_root(executable, frozen)
    if root is not None:
        return root / 'data'
    environment = os.environ if environ is None else environ
    return Path(environment['LOCALAPPDATA']) / 'CS2POVHelper'


def initialize_profile(directory):
    """Write visible generic defaults only when neither primary nor backup exists."""
    directory = Path(directory).absolute()
    no_redirection(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Test writable storage even on an existing fully populated profile. A
        # portable folder must not silently redirect recovery data elsewhere.
        probe = directory / ('.portable-write-' + uuid.uuid4().hex)
        try:
            with probe.open('xb') as stream:
                stream.write(b'portable-profile\n')
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            probe.unlink(missing_ok=True)
        defaults = (
            ('settings.json', Settings.decode, asdict(Settings())),
            ('hud-presets.json', Presets.decode,
             {'schema': 1, 'default': 'builtin', 'items': [asdict(HudPreset())]}),
        )
        for name, decode, raw in defaults:
            file = JsonFile(directory / name, decode)
            no_redirection(file.path)
            no_redirection(file.backup)
            if not file.path.exists() and not file.backup.exists():
                file.save(raw)
    except OSError as error:
        raise PortableError('便携 data 文件夹无法写入。请将整个虾米pov文件夹解压到'
                            '可写位置后运行；不会改用其他数据目录。') from error
    return directory


def _assert_migratable(source):
    no_redirection(source)
    if not source.is_dir():
        raise PortableError('旧数据目录不存在。')
    lock = source / 'app.lock'
    no_redirection(lock)
    if lock.exists():
        raise PortableError('旧数据目录仍有应用锁，请关闭旧应用后再迁移。')
    if pending_sessions(source / 'sessions'):
        raise PortableError('旧数据目录存在未恢复或损坏会话，请先在旧应用完成恢复。')
    from cs2pov.services.recording_recovery import scan_recording_recovery
    if scan_recording_recovery(source):
        raise PortableError('旧数据目录存在未处理录制状态，请先在旧应用完成恢复。')
    database = source / 'library.sqlite'
    no_redirection(database)
    if database.exists():
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] not in (1, 2):
                    raise PortableError('旧任务库版本无法核验，请先检查旧数据目录。')
                if connection.execute("SELECT id FROM sessions WHERE state != 'complete' LIMIT 1").fetchone():
                    raise PortableError('旧任务库仍有未完成会话，请先完成恢复。')
        except sqlite3.DatabaseError as error:
            raise PortableError('旧任务库无法读取，禁止迁移以免遗漏恢复任务。') from error


def migrate_profile(source, target):
    """Explicitly copy settings/presets to a new empty target, with no task data.

    Invoke before the target's first launch. Existing targets with any content
    are rejected. Source files and recovery backups are always left in place.
    """
    source, target = Path(source).absolute(), Path(target).absolute()
    no_redirection(source)
    no_redirection(target)
    if source == target or source in target.parents or target in source.parents:
        raise PortableError('新旧数据目录必须相互独立。')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise PortableError('迁移目标必须为空；已有配置和恢复备份不会被覆盖。')
    _assert_migratable(source)
    settings = _read_json(source / 'settings.json', Settings.decode)
    preset_path = source / 'hud-presets.json'
    items, default = (_read_json(preset_path, Presets.decode) if preset_path.exists()
                      else ([HudPreset()], 'builtin'))
    # A historical successful test is not approval for a new package or machine.
    settings = replace(settings, nvidia_path_confirmed=False, output_verified=False)
    _assert_migratable(source)
    no_redirection(target)
    if target.exists() and any(target.iterdir()):
        raise PortableError('迁移目标在检查期间发生变化，已有内容保留。')
    stage = target.with_name('.' + target.name + '.migration-' + uuid.uuid4().hex)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        no_redirection(stage)
        stage.mkdir()
        JsonFile(stage / 'settings.json', Settings.decode).save(asdict(settings))
        JsonFile(stage / 'hud-presets.json', Presets.decode).save(
            {'schema': 1, 'default': default, 'items': [asdict(item) for item in items]})
        # Publish a fully prepared directory. Never let JsonFile's ordinary
        # save/backup behaviour replace configuration created by another writer.
        _assert_migratable(source)
        no_redirection(target)
        if target.exists():
            if not target.is_dir() or any(target.iterdir()):
                raise PortableError('迁移目标在准备期间发生变化，已有内容保留。')
            target.rmdir()  # Only an empty directory can be removed.
        stage.rename(target)
    except OSError as error:
        raise PortableError('新 data 文件夹无法写入；旧设置和恢复备份保留。') from error
    finally:
        # These are the only new files written in this exclusive staging path.
        # Leave any unexpected content in place, rather than recursively delete.
        if stage.exists():
            no_redirection(stage)
            for name in ('settings.json', 'settings.json.bak',
                         'hud-presets.json', 'hud-presets.json.bak'):
                path = stage / name
                no_redirection(path)
                path.unlink(missing_ok=True)
            try:
                stage.rmdir()
            except OSError:
                pass
    return target
