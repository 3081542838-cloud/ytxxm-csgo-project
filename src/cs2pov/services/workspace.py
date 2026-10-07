"""Shared desktop state; settings/UI cannot bypass task and recovery guards."""
from dataclasses import asdict, dataclass
from pathlib import Path
from sqlite3 import DatabaseError
from PySide6.QtCore import QObject, Signal, QProcess, QProcessEnvironment, QTimer
from cs2pov.adapters.disk import check_directory, DiskError
from cs2pov.adapters.installation import inspect_installation
from cs2pov.storage.settings import DataError, JsonFile, Settings, Presets, local_path
from cs2pov.storage.library import Library
from cs2pov.storage.transaction import (pending_sessions, no_redirection, read_small,
                                      safe_path, digest, attributes)


@dataclass(frozen=True)
class RecoveryItem:
    """A recovery target is distinct from its human-readable description."""
    session_id: str | None
    directory: Path | None
    label: str
    source: str


class Workspace(QObject):
    changed = Signal()
    cancellation_requested = Signal()

    def __init__(self, directory: Path, *, disk_check=check_directory, preview_factory=None,
                 recording_factory=None, automatic_recording=False, input_check=None):
        super().__init__()
        self.directory = directory.absolute()
        no_redirection(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in ("sessions", "backups", "logs", "cache"):
            path = self.directory / name
            no_redirection(path)
            path.mkdir(exist_ok=True)
        self.settings_file = JsonFile(self.directory / "settings.json", Settings.decode)
        self.settings = self.settings_file.load(Settings())
        # A historical checkbox never establishes today's environment.
        from dataclasses import replace
        self.settings = replace(self.settings, output_verified=False)
        from cs2pov.storage.local_resources import LocalResources
        self.local_resources = LocalResources(self.directory / 'local-resources.json')
        self._local_resource_refs = None
        self.presets = Presets(self.directory)
        self.library = Library(self.directory / "library.sqlite")
        self.warnings = [item for item in (self.settings_file.warning, self.presets.file.warning) if item]
        self.disk_check = disk_check
        self.disk = None
        self.disk_error = "尚未检查视频目录。"
        self.environment = "CS2 尚未检查；插件兼容性待试录；NVIDIA 状态待使用者确认。"
        self.busy = False
        self.task_text = "没有正在执行的任务"
        self.analysis = None
        self.parse_error = ""
        self._parse_process = None
        self._parse_generation = 0
        from cs2pov.services.preview_session import PreviewSession
        self._preview_factory = preview_factory or PreviewSession
        self._preview = None
        self._recording_task = None
        self._last_recording_task = None
        self._output_workflow = None
        self._output_process = None
        self._output_generation = 0
        self._output_request = None
        self._output_restore_status = 'unknown'
        self.output_status = '完成录制并确认停止后，检查本次视频。'
        self.automatic_recording = automatic_recording
        self.input_check = input_check
        self._auto_capture_requested = False
        self._recording_automation = None
        self._environment_coordinator = None
        self._environment_observation = None
        self._environment_generation = 0
        self._trial_environment = None
        self._recording_task_factory = recording_factory or self._make_recording_task
        self._recording_database_error = ''
        self.nvidia_recovery_items = []
        self.recording_status = ''
        self.preview_status = "选择并保存片段后，可以准备本地预览。"
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self.poll_preview)
        self.cancellation_requested.connect(self.cancel_parse)
        self.cancellation_requested.connect(self.stop_preview)
        self.cancellation_requested.connect(self.cancel_output)
        self.recovery = []
        self.recovery_items = []
        self.refresh_recovery()

    @property
    def pipe_cleanup_pending(self):
        """Live resources cannot be discharged by a completed file journal."""
        return bool(self._preview is not None
                    and getattr(self._preview, 'pipe_cleanup_pending', False))

    def ensure_idle(self):
        if self.pipe_cleanup_pending:
            raise DataError('命令通路清理尚未完成，请先重试安全收尾；不能开始新任务或仅恢复文件。')
        if self.busy:
            raise DataError("任务执行中，当前参数已锁定；可先取消任务。")

    def refresh_recovery(self):
        try:
            pending = pending_sessions(self.directory / "sessions")
            items = [RecoveryItem(path.name, path, str(path), "journal") for path in pending]
            present = {item.session_id for item in items}
            for session_id in self.library.unfinished():
                if session_id in present:
                    continue
                # SQLite is an index, not a source of arbitrary restore paths.
                valid = (isinstance(session_id, str) and bool(session_id)
                         and session_id not in (".", "..")
                         and not any(char in session_id for char in ("/", "\\", ":", "\x00")))
                directory = self.directory / "sessions" / session_id if valid else None
                items.append(RecoveryItem(session_id if valid else None, directory,
                                          "数据库未完成会话：" + str(session_id), "database"))
            self.recovery_items = items
            # Retain the public summary list used by task guards and diagnostics.
            self.recovery = [item.label for item in items]
        except (OSError, ValueError, RuntimeError, DatabaseError) as error:
            message = "恢复检查失败：" + str(error)
            self.recovery_items = [RecoveryItem(None, None, message, "error")]
            self.recovery = [message]
        self.refresh_recording_recovery(emit=False)
        self.changed.emit()

    def refresh_recording_recovery(self, *, emit=True):
        from cs2pov.services.recording_recovery import scan_recording_recovery
        self.nvidia_recovery_items = scan_recording_recovery(self.directory)
        if emit:
            self.changed.emit()

    def resolve_recording_recovery(self, item, *, task_id, checkpoint_digest):
        from cs2pov.services.recording_recovery import resolve_recording_recovery
        self.ensure_idle()
        if item not in self.nvidia_recovery_items:
            raise DataError('NVIDIA 确认不属于当前保留的任务，请重新检查。')
        resolve_recording_recovery(self.directory, item, task_id=task_id,
                                   checkpoint_digest=checkpoint_digest)
        self.refresh_recording_recovery()

    def save_settings(self, settings):
        self.ensure_idle()
        # Callers cannot turn output_verified on via a checkbox or raw object.
        next_settings = self.settings.updated(installation=settings.installation, cfg=settings.cfg,
                                             video_directory=settings.video_directory, hotkey=settings.hotkey,
                                             console_key=settings.console_key,
                                             nvidia_path_confirmed=settings.nvidia_path_confirmed)
        if next_settings != self.settings:
            from dataclasses import replace
            next_settings = replace(next_settings, output_verified=False)
            self._environment_observation = None
            self._environment_generation += 1
            if self._environment_coordinator: self._environment_coordinator.cancel()
        self.settings_file.save(asdict(next_settings))
        self.settings = next_settings
        self.disk, self.disk_error = None, "目录或设置已保存，请重新检查空间。"
        self.environment = "设置已更新，CS2 和插件兼容性需要重新检查。"
        self.changed.emit()

    def local_resource_path(self, role):
        """Display references only; hashing is done at registration and use."""
        import sys
        if role not in ('hud', 'probe'): raise DataError('未知的本地资源用途。')
        if self._local_resource_refs is None:
            self._local_resource_refs = self.local_resources.load()
        ref = self._local_resource_refs.get(role)
        if ref is not None: return Path(ref.path)
        if getattr(sys, 'frozen', False):
            from cs2pov.services.portable import portable_root
            root = portable_root()
            if root is None: return None
            candidate = root / ('resources/pov.vpk' if role == 'hud' else 'tools/ffprobe.exe')
            no_redirection(candidate)
            return candidate if candidate.is_file() else None
        if role == 'hud':
            candidate = Path(__file__).resolve().parents[3]/'resources/bundled/pov.vpk'
        else:
            from cs2pov.adapters.probe_tool import development_probe_path
            candidate = development_probe_path()
        return candidate if candidate.is_file() else None

    def register_local_resource(self, role, path):
        self.ensure_idle()
        reference = self.local_resources.save(role, path)
        from dataclasses import replace
        self.settings = replace(self.settings, output_verified=False)
        self._environment_observation = None
        self._environment_generation += 1
        if self._environment_coordinator: self._environment_coordinator.cancel()
        self.environment = '本地资源已登记；当前环境需要重新试录核验。'
        # The resource is already committed. Revoke old callbacks before any
        # later registry read or settings write can fail.
        self._local_resource_refs = self.local_resources.load()
        self.settings_file.save(asdict(self.settings))
        self.changed.emit()
        return reference

    def verified_local_resource(self, role):
        # Re-read references at use, so UI display cannot grant executable trust.
        references = self.local_resources.load()
        if role in references: return self.local_resources.get_verified_path(role)
        path = self.local_resource_path(role)
        if path is None:
            label = 'HUD .vpk' if role == 'hud' else 'ffprobe.exe'
            raise DataError('请在设置与恢复中选择已审核的本地 '+label+' 文件。')
        return Path(self.local_resources.pick(role, path).path)

    def check_video(self):
        self.disk = None
        try:
            if not self.settings.video_directory:
                raise DiskError("请先选择视频保存目录。")
            self.disk = self.disk_check(Path(self.settings.video_directory))
            self.disk_error = ""
        except (OSError, DiskError) as error:
            self.disk_error = str(error)
        self.changed.emit()
        return self.disk

    def preparation_gate(self):
        self.ensure_idle()
        from cs2pov.adapters.nvidia import ensure_playback_hotkey_compatible
        ensure_playback_hotkey_compatible(self.settings.hotkey)
        self.refresh_recovery()
        if self.recovery:
            raise DataError("存在未恢复事务，先完成恢复。")
        if self.nvidia_recovery_items:
            raise DataError('NVIDIA 上次录制状态未知，请先在设置与恢复中检查并停止录制。')
        if not self.check_video():
            raise DataError(self.disk_error)
        return self.disk

    def import_demo(self, path: Path):
        self.ensure_idle()
        if self.recovery:
            raise DataError("存在未恢复事务，先完成恢复。")
        local_path(str(path))
        if not path.is_file() or path.suffix.casefold() != ".dem":
            raise DataError("请选择一个已有的 .dem 文件。")
        stat = path.stat()
        self.library.save_draft({"demo": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                 "status": "awaiting_parse"})
        self.analysis, self.parse_error = None, ""
        self.changed.emit()

    def remove_demo(self):
        self.ensure_idle()
        with self.library.db:
            self.library.db.execute("DELETE FROM drafts WHERE id='current'")
        self.analysis, self.parse_error = None, ""
        self.changed.emit()

    def start_parse(self):
        import sys
        self.ensure_idle()
        self.refresh_recovery()
        if self.recovery:
            raise DataError("存在未恢复事务，不能解析新任务。")
        draft = self.library.draft()
        if not draft:
            raise DataError("请先导入 Demo。")
        draft.pop("selection", None)
        draft["status"] = "parsing"
        self.library.save_draft(draft)
        self.analysis, self.parse_error = None, ""
        self._parse_generation += 1
        generation = self._parse_generation
        process = QProcess(self)
        self._parse_process = process
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
        process.setProcessEnvironment(environment)
        python = Path(sys.executable)
        if python.name.casefold() == "pythonw.exe":
            python = python.with_name("python.exe")
        process.setProgram(str(python))
        worker = ['--demo-worker'] if getattr(sys, 'frozen', False) else ['-m', 'cs2pov.services.demo_worker']
        import uuid
        self._parse_result_path = self.directory / 'cache' / ('parse-' + uuid.uuid4().hex + '.json')
        process.setArguments([*worker, "--demo", draft["demo"],
                              "--cache", str(self.directory / "cache"), '--result', str(self._parse_result_path)])
        process.finished.connect(lambda code, status: self.finish_parse(process, generation, code))
        process.errorOccurred.connect(lambda error: self.finish_parse(process, generation, -1)
                                      if error == QProcess.ProcessError.FailedToStart else None)
        self.set_busy(True, "正在解析 Demo；可取消，不会修改游戏或原文件")
        process.start()
        QTimer.singleShot(120_000, lambda: self.parse_timeout(process))

    def parse_timeout(self, process):
        if self._parse_process is process:
            self.parse_error = "解析超过两分钟，已停止；可重试或换一个完整 Demo。"
            self._parse_generation += 1
            process.kill()

    def cancel_parse(self):
        if self._parse_process is not None:
            self._parse_generation += 1
            self._parse_process.kill()

    def finish_parse(self, process, generation, code):
        import json
        from cs2pov.services.demo_worker import CACHE_VERSION, verify_cache
        if self._parse_process is not process:
            return
        self._parse_process = None
        draft = self.library.draft()
        try:
            if generation != self._parse_generation:
                draft["status"] = "failed" if self.parse_error else "cancelled"
            else:
                from cs2pov.adapters.video import read_data_lease
                if getattr(self, '_parse_result_path', None) is not None:
                    with read_data_lease(self._parse_result_path) as result_signature:
                        if result_signature.bytes > 65536: raise DataError('解析结果超出大小限制。')
                        raw = json.loads(self._parse_result_path.read_bytes())
                else:
                    raw = json.loads(bytes(process.readAllStandardOutput()).decode("utf-8"))
                if code != 0 or "error" in raw:
                    raise DataError(raw.get("error", "解析程序未正常结束。"))
                cache = Path(raw["cache"])
                expected = self.directory / "cache" / (CACHE_VERSION + raw["fingerprint"]["sha256"] + ".json")
                if cache != expected:
                    raise DataError("解析结果位置无效。")
                no_redirection(cache)
                if cache.stat().st_size > 32 * 1024 * 1024:
                    raise DataError("解析结果超出大小限制。")
                payload = json.loads(cache.read_text(encoding="utf-8"))
                stat = Path(draft["demo"]).stat()
                if (stat.st_size, stat.st_mtime_ns) != (raw["fingerprint"]["bytes"], raw["fingerprint"]["mtime_ns"]):
                    raise DataError("Demo 在解析结束后变化，请重试。")
                self.analysis = verify_cache(payload)
                draft.update(status="parsed", fingerprint=raw["fingerprint"], map=self.analysis["map"])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            self.analysis = None
            self.parse_error = "解析失败：" + str(error)
            draft["status"] = "failed"
        finally:
            process.deleteLater()
            draft["error"] = self.parse_error
            try:
                self.library.save_draft(draft)
            except Exception as error:
                self.analysis = None
                self.parse_error = "草稿保存失败：" + str(error)
            self.set_busy(False)

    def save_selection(self, player_id, mode, **options):
        from cs2pov.services.clips import selection
        self.ensure_idle()
        if self.analysis is None:
            raise DataError("请先解析当前 Demo。")
        draft = self.library.draft()
        stat = Path(draft["demo"]).stat()
        if (stat.st_size, stat.st_mtime_ns) != (draft["fingerprint"]["bytes"], draft["fingerprint"]["mtime_ns"]):
            self.analysis = None
            raise DataError("Demo 已变化，请重新解析。")
        clip = selection(self.analysis, player_id, mode, **options)
        preset = next(p for p in self.presets.items if p.id == self.presets.default)
        draft["selection"] = {**clip, "hud": asdict(preset), "content_sha256": draft["fingerprint"]["sha256"]}
        self.library.save_draft(draft)
        self.changed.emit()
        return clip

    def save_preset(self, preset):
        self.ensure_idle()
        self.presets.save(preset)
        self.changed.emit()

    def remove_record(self, record_id):
        """Remove only a local library row; never delete the video itself."""
        self.ensure_idle()
        self.library.remove_record(record_id)
        self.changed.emit()

    def reprepare_record(self, record_id):
        """Restore a verified saved draft only; starting CS2 remains explicit."""
        import json
        from copy import deepcopy
        from cs2pov.adapters.demo import fingerprint
        from cs2pov.services.clips import selection
        from cs2pov.services.demo_worker import CACHE_VERSION, verify_cache
        from cs2pov.storage.library import decode_record_payload
        from cs2pov.storage.settings import HudPreset
        self.ensure_idle()
        self.refresh_recovery()
        if self.recovery or self.nvidia_recovery_items:
            raise DataError('先完成保留的恢复检查，再重新准备记录。')
        try:
            row = self.library.record(record_id)
            if row is None:
                raise DataError('此记录已经移除，请刷新列表。')
            payload = decode_record_payload(row)
            saved = deepcopy(payload['draft_snapshot'])
            if not {'demo', 'fingerprint', 'selection', 'map'} <= saved.keys():
                raise DataError('旧记录没有完整草稿快照，请重新导入并解析 Demo。')
            demo = Path(local_path(saved['demo']))
            no_redirection(demo)
            if demo.suffix.casefold() != '.dem' or not demo.is_file():
                raise DataError('记录引用的原 Demo 不存在，请重新导入。')
            expected = saved['fingerprint']
            if (not isinstance(expected, dict) or set(expected) != {'sha256', 'bytes', 'mtime_ns'}
                    or fingerprint(demo) != expected):
                raise DataError('原 Demo 已变化，请重新导入并解析。')
            cache = self.directory / 'cache' / (CACHE_VERSION + expected['sha256'] + '.json')
            no_redirection(cache)
            if not cache.is_file() or cache.stat().st_size > 32 * 1024 * 1024:
                raise DataError('分析缓存不存在或无效，请重新解析原 Demo。')
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise DataError('分析缓存包含重复字段。')
                    result[key] = value
                return result
            def reject(_):
                raise DataError('分析缓存包含非有限数字。')
            with cache.open('rb') as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise DataError('分析缓存超出大小限制。')
            raw = json.loads(data, object_pairs_hook=pairs, parse_constant=reject)
            if raw['fingerprint'] != expected:
                raise DataError('分析缓存属于另一份 Demo，请重新解析。')
            analysis = verify_cache(raw)
            clip = saved['selection']
            if (saved['map'] != analysis['map'] or clip['map'] != analysis['map']
                    or clip['content_sha256'] != expected['sha256']):
                raise DataError('记录的地图或 Demo 身份不一致。')
            HudPreset.decode(clip['hud'])
            mode = clip['mode']
            options = ({'round_id': clip['round_id']} if mode == 'round' else
                       {'start_seconds': clip['start_seconds'], 'end_seconds': clip['end_seconds']}
                       if mode == 'time' else
                       {'candidate_id': clip['candidate_id'], 'start_tick': clip['start_tick'],
                        'end_tick': clip['end_tick']} if mode == 'highlight' else {})
            recalculated = selection(analysis, clip['player_id'], mode, **options)
            # Every derived field must match; never silently widen an old range.
            if any(clip.get(key) != value for key, value in recalculated.items()):
                raise DataError('记录片段与当前分析不一致，请重新选择范围。')
            no_redirection(demo)
            if fingerprint(demo) != expected:
                raise DataError('原 Demo 在检查中变化，草稿保留。')
            saved.update(status='parsed', error='')
            self.library.save_draft(saved)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, RecursionError) as error:
            raise DataError('重新准备失败：' + str(error)) from error
        self.analysis, self.parse_error = analysis, ''
        self.changed.emit()
        return saved

    def preset_action(self, operation, id):
        self.ensure_idle()
        result = getattr(self.presets, operation)(id)
        self.changed.emit()
        return result

    def set_busy(self, busy, text=""):
        if not busy and self.pipe_cleanup_pending:
            busy = True
            text = self.preview_status
        self.busy = busy
        self.task_text = text if busy else "没有正在执行的任务"
        self.changed.emit()

    def start_capture(self):
        if self.busy:
            raise DataError('已有任务正在执行。')
        self.ensure_idle()
        if not self.settings.nvidia_path_confirmed:
            raise DataError('请先确认 NVIDIA 实际视频保存目录。')
        if self.automatic_recording and self.input_check is not None:
            self.input_check()
        self._auto_capture_requested = True
        try:
            self.start_preview()
        except Exception:
            self._auto_capture_requested = False
            raise

    def start_preview(self, resource=None):
        self.preparation_gate()
        draft = self.library.draft()
        if self.analysis is None or not draft or not draft.get("selection"):
            raise DataError("请先解析 Demo 并保存玩家与片段范围。")
        if not self.settings.installation or not self.settings.cfg:
            raise DataError("请先在设置中填写 CS2 安装目录和用户配置目录。")
        base = Path(resource) if resource is not None else self.verified_local_resource('hud')
        if not base.is_file():
            raise DataError("没有本地已审核 HUD 资源；请先导入与受支持版本匹配的资源。")
        preview = self._preview_factory(self.directory/"sessions",self.settings,self.analysis,draft,base)
        self._preview = preview
        self.set_busy(True,"正在准备本地预览；不启动录制")
        try:
            # Register the task before any file deployment or owned process
            # launch. A database failure must never leave an unregistered game.
            with self.library.db:
                self.library.db.execute("INSERT INTO sessions VALUES (?, ?)",(preview.session.name,preview.state))
            preview.start()
        except Exception:
            preview.stop()
            raise
        finally:
            self.poll_preview()
            if self._preview is not None: self._preview_timer.start()

    def confirm_preview_console(self, *, session_id=None, step_token=None, task_id=None):
        if self._preview is None: raise DataError("没有等待控制台确认的预览任务。")
        if self._recording_task is not None:
            self._recording_task.confirm_console(task_id=task_id, session_id=session_id,
                                                 step_token=step_token)
            self.poll_recording()
            return
        if session_id is not None and session_id != self._preview.session.name:
            raise DataError("控制台确认属于其他预览任务，请查看当前步骤。")
        if session_id is not None and not step_token:
            raise DataError("当前界面没有有效的控制台步骤，请刷新后再确认。")
        try:
            self._preview.confirm_console(step_token=step_token)
        except Exception:
            self.poll_preview()
            raise
        self.preview_status = "请切回 CS2 并松开键鼠；检测到游戏前台后开始准备。"
        self.changed.emit()

    def prepare_preview_playback(self):
        if self._recording_task is not None: raise DataError('录制任务执行中，不能单独准备播放。')
        if self._preview is None: raise DataError('没有可准备播放的预览任务。')
        self._preview.prepare_playback()
        self.poll_preview()

    def play_preview_clip(self):
        if self._recording_task is not None: raise DataError('录制任务执行中，播放由当前录制步骤控制。')
        if self._preview is None: raise DataError('没有已准备的预览片段。')
        self._preview.play_clip()
        self.poll_preview()

    def check_preview_binding(self):
        if self._recording_task is not None: raise DataError('录制任务执行中，必须先确认停止才能核验绑定。')
        if self._preview is None: raise DataError('没有已停止的预览片段。')
        self._preview.check_playback_binding()
        self.poll_preview()

    def poll_preview(self):
        if self._recording_task is not None:
            self.poll_recording()
            return
        preview = self._preview
        if preview is None: return
        try:
            preview.poll()
        except Exception:
            # The session has already recorded the error and started safe cleanup.
            pass
        messages = {"awaiting_console":"在 CS2 中打开控制台，点击空输入框，看到光标后再确认并切回游戏。",
                    "connecting_game":"正在自动检查本次 CS2 初始化与受限命令连接；无需打开控制台。",
                    "checking_command_pipe":"正在等待本次游戏的新查询回执；尚未加载 Demo。",
                    "awaiting_game":"正在启动受管理 CS2。若游戏提示启动地区，请选择平时使用的地区并点开始；等待菜单出现。",
                    "loading":"正在载入选定 Demo，等待游戏回读确认。",
                    "awaiting_demo_console":"Demo 已载入。请再次打开控制台，点击空输入框，看到光标后再确认并切回游戏。",
                    "preparing":"正在定位片段起点并核验目标玩家第一人称。",
                    "ready":"预览准备完成。回到 CS2 收起控制台检查画面；结束时点“退出并恢复”。",
                    "awaiting_play_console":"请打开 CS2 控制台，点击空输入框确认光标，然后确认；应用将收起控制台准备播放。",
                    "arming_playback":"正在检查临时播放按键并收起控制台；这一步不录制。",
                    "play_ready":"控制台已收起。请检查目标玩家和画面，确认后播放所选片段；本次不录制。",
                    "awaiting_play_foreground":"请切回 CS2 并松开键鼠；检测到游戏前台后播放一次所选片段，不录制。",
                    "playing":"正在预览所选片段，等待实际终点暂停；不录制视频。",
                    "play_ended":"片段已在指定终点暂停。可以核验播放按键恢复，或退出并恢复。",
                    "awaiting_end_console":"请打开 CS2 控制台，点击空输入框确认光标，然后确认；只检查临时按键已恢复。",
                    "checking_binding":"正在核验临时播放按键已恢复；不再次播放。",
                    "play_verified":"片段播放和终点暂停已核验，临时播放按键已恢复。请退出并恢复游戏文件。",
                    "preview_changed":"游戏位置或视角已改变，原预览核验已失效。请退出并恢复后重新准备。",
                    "closing":"正在退出本次游戏并校验文件恢复。",
                    "complete":"预览会话已结束，文件恢复完成。",
                    "recovery_blocked":"恢复尚未完成。请到“设置与恢复”处理保留的备份。"}
        # Keep the switch-back instruction while waiting for foreground.
        if not (preview.state in ('awaiting_console','awaiting_demo_console','awaiting_play_console','awaiting_end_console') and preview.console_confirmed):
            self.preview_status = messages.get(preview.state,"预览状态待确认。")
        if getattr(preview,'automatic',False):
            automatic_messages = {
                'ready':'预览准备完成，玩家和起点已核验。可以准备播放，或退出并恢复。',
                'arming_playback':'正在通过受限命令连接核验播放绑定与收起控制台；本次不录制。',
                'play_ready':'播放准备完成。可以播放所选片段；本次不录制。',
                'checking_binding':'正在自动查询播放按键已恢复；不需要打开控制台。'}
            self.preview_status = automatic_messages.get(preview.state,self.preview_status)
        if preview.error: self.preview_status += "\n"+preview.error
        self.task_text = self.preview_status
        try:
            with self.library.db:
                self.library.db.execute("UPDATE sessions SET state=? WHERE id=?",(preview.state,preview.session.name))
        except DatabaseError as error:
            # The file journal remains the recovery source of truth. Stop the
            # owned game instead of leaking an exception from the Qt timer and
            # leaving an active task with an outdated database checkpoint.
            message = "预览状态保存失败：" + str(error)
            if message not in preview.error:
                preview.error += ('\n' if preview.error else '') + message
            # An already blocked pipe cleanup only retries through the public
            # stop action. A failing database timer must not implicitly keep
            # closing retained resources on every poll.
            if not (preview.state == 'recovery_blocked' and self.pipe_cleanup_pending):
                try:
                    preview.stop()
                except Exception as close_error:
                    preview.state = 'recovery_blocked'
                    preview.error += '\n安全收尾未完成，保留备份：' + str(close_error)
            self.preview_status = messages.get(preview.state, "预览状态待确认。") + '\n' + preview.error
            self.task_text = self.preview_status
        if self.pipe_cleanup_pending:
            self._retain_pipe_cleanup()
        elif preview.state in ("complete","recovery_blocked"):
            self._auto_capture_requested = False
            self._preview_timer.stop()
            self._preview = None
            self.set_busy(False)
            if preview.error:
                self.task_text = self.preview_status
            self.refresh_recovery()
        else:
            try:
                if self._auto_capture_requested and preview.state == 'ready':
                    preview.prepare_playback()
                elif self._auto_capture_requested and preview.state == 'play_ready':
                    self.start_recording()
                    self._auto_capture_requested = False
                    return
            except Exception as error:
                # Qt timer exceptions cannot leave a paused preview stranded.
                # Consume the request, retain the cause and use owned cleanup;
                # never retry an uncertain NVIDIA toggle.
                self._auto_capture_requested = False
                message = '自动录制衔接失败：' + str(error)
                preview.error += ('\n' if preview.error else '') + message
                if self._recording_task is not None:
                    self._recording_task.cancel()
                    self.poll_recording()
                    return
                try:
                    preview.stop()
                except Exception as close_error:
                    preview.state = 'recovery_blocked'
                    preview.error += '\n安全收尾未完成，保留备份：' + str(close_error)
                preview._persist()
                self.poll_preview()
                return
            self.changed.emit()

    def _retain_pipe_cleanup(self):
        """Keep the exact held preview for bounded, explicit close retries."""
        if self._preview.state == 'closing':
            # The original owned-process exit deadline still applies. Stopping
            # polling here would strand a close that has not yet observed exit.
            self._preview_timer.start()
        else:
            self._preview_timer.stop()
            message = ('命令通路清理尚未完成，任务保持锁定。请点击“重试安全收尾”；'
                       '应用会重新核验游戏退出和文件恢复。')
            if message not in self.preview_status:
                self.preview_status += '\n' + message
        self.busy = True
        self.task_text = self.preview_status
        self.refresh_recovery()

    def stop_preview(self):
        if self._recording_task is not None:
            self._recording_task.cancel()
            self.poll_recording()
            return
        if self._preview is not None:
            self._preview.stop()
            self.poll_preview()

    def _recording_checkpoint(self, preview, name, value):
        import json
        from dataclasses import is_dataclass
        from cs2pov.storage.transaction import atomic_write
        session = safe_path(self.directory / 'sessions', preview.session / name)
        no_redirection(session)
        raw = asdict(value) if is_dataclass(value) else value
        atomic_write(session, json.dumps(raw, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def _make_recording_task(self, preview):
        from cs2pov.adapters.nvidia import Win32NvidiaHotkey
        from cs2pov.services.recording_workflow import RecordingWorkflow
        from cs2pov.services.recording_task import RecordingTask
        identity = preview.game.verify()
        workflow = RecordingWorkflow(preview.settings.video_directory, preview.settings.hotkey,
            nvidia_path_confirmed=preview.settings.nvidia_path_confirmed,
            draft=preview.draft, expected_identity=identity, game=preview.game,
            preview_reader=lambda: preview.controller.verify_result(preview.draft,
                since=preview.clock()-5, require_foreground=False),
            end_reader=lambda: preview.controller.verify_result(preview.draft,
                since=preview.playback.started_at, at_end=True, require_foreground=False),
            disk_checker=lambda path, minimum: self.disk_check(path),
            hotkey_adapter=Win32NvidiaHotkey(game=preview.game),
            checkpoint=lambda value: self._recording_checkpoint(preview, 'recording-session.json', value),
            workflow_checkpoint=lambda value: self._recording_checkpoint(preview, 'recording-workflow.json', value),
            record_store=self.library, session_clock=preview.clock)
        task = RecordingTask(preview, workflow, clock=preview.clock,
            checkpoint=lambda value: self._recording_checkpoint(preview, 'recording-task.json', value))
        if self.automatic_recording:
            from cs2pov.services.hotkey_recording import HotkeyRecording
            def activate_game():
                import os
                if preview.console.foreground_pid() == os.getpid():
                    task._contract()
                    from cs2pov.adapters.overlay_hotkey import OverlayHotkey
                    OverlayHotkey(preview.game,identity,clock=preview.clock)._activate_owned_game()
            self._recording_automation = HotkeyRecording(task, activate_game=activate_game)
        return task

    def start_recording(self):
        from cs2pov.adapters.nvidia import ensure_playback_hotkey_compatible
        if self._recording_task is not None:
            raise DataError('已有录制任务，不能同时开始另一任务。')
        preview = self._preview
        if not self.busy or preview is None or preview.state != 'play_ready':
            raise DataError('录制前必须完成暂停起点、视角与隐藏控制台的准备。')
        self.refresh_recording_recovery(emit=False)
        if self.nvidia_recovery_items:
            raise DataError('NVIDIA 上次录制状态未知，先检查并停止录制。')
        if not self.settings.nvidia_path_confirmed:
            raise DataError('请先确认 NVIDIA 实际视频保存目录。')
        if preview.settings != self.settings:
            raise DataError('设置与本次预览不一致，请退出并重新准备录制。')
        ensure_playback_hotkey_compatible(self.settings.hotkey)
        self.disk_check(Path(self.settings.video_directory))
        task = self._recording_task_factory(preview)
        self._trial_environment = None
        if self._environment_observation and self._environment_observation.snapshot:
            import time
            from cs2pov.services.environment_receipt import freeze_trial_environment
            observation = self._environment_observation
            snapshot = observation.snapshot
            hud = self.verified_local_resource('hud')
            same_configuration = (Path(snapshot.installation)==Path(self.settings.installation)
                and Path(snapshot.cfg_directory)==Path(self.settings.cfg)
                and Path(snapshot.output_directory)==Path(self.settings.video_directory)
                and Path(snapshot.hud_resource_path)==hud==preview.base)
            if same_configuration and 0 <= time.time()-observation.observed_at <= 300:
                self._trial_environment = freeze_trial_environment(task.task_id,observation.snapshot,
                    observed_at=observation.observed_at)
        self._recording_task = task
        self._recording_database_error = ''
        try:
            task.arm()
        except Exception:
            try:
                task.cancel()
            finally:
                self.poll_recording()
            raise
        self._preview_timer.start()
        self.poll_recording()

    def confirm_recording_state(self, *, task_id, state, step_token):
        task = self._recording_task
        methods = {'awaiting_not_recording': 'confirm_not_recording',
                   'awaiting_started': 'confirm_started', 'awaiting_stopped': 'confirm_stopped'}
        if task is None or state not in methods:
            raise DataError('当前没有可确认的录制步骤。')
        # Dispatch the step displayed by the UI, never silently replace its token.
        getattr(task, methods[state])(task_id=task_id, step_token=step_token)
        self.poll_recording()

    def poll_recording(self):
        task = self._recording_task
        if task is None:
            return
        task.poll()
        runner = self._recording_automation
        if runner is not None:
            runner.poll()
        messages = {
            'awaiting_not_recording': '请检查 NVIDIA 当前未录制，再确认开始本次录制。',
            'awaiting_start_foreground': '请切回 CS2 并松开键鼠；开始热键仅发送一次。',
            'awaiting_started': '请检查 NVIDIA 已开始录制，再确认播放所选片段。',
            'awaiting_play_foreground': '请切回 CS2；检测到游戏前台后播放所选片段。',
            'recording': '正在录制所选片段，等待实际终点暂停。',
            'awaiting_stop_foreground': '终点已暂停；需要游戏前台才能发送一次停止热键。',
            'awaiting_stopped': '请检查 NVIDIA 已停止录制或显示已保存，再确认。',
            'awaiting_end_console': '录制已确认停止。请打开控制台、聚焦空输入框，然后确认核验播放键。',
            'checking_binding': '正在回读临时播放键已恢复；不会再次播放或录制。',
            'closing': '正在退出受管理游戏，并恢复原文件。',
            'complete': '本次录制操作和文件收尾已结束；视频尚需验证。',
            'recovery_blocked': '文件恢复尚未完成，请处理保留的备份。'}
        self.recording_status = messages.get(task.state, '录制状态正在检查。')
        if getattr(task,'dispatch_mode',None) == 'direct_hotkey' and task.state == 'complete':
            self.recording_status = '开始与停止热键已发送，游戏已退出并恢复。请在视频目录查看本次素材。'
        if runner is not None and task.state in ('awaiting_not_recording','awaiting_started','awaiting_stopped'):
            self.recording_status = '按配置的 NVIDIA 快捷键自动开始、播放和停止；不检查浮窗状态。'
        if runner is not None and runner.error: self.recording_status += '\n'+runner.error
        if task.error:
            self.recording_status += '\n' + task.error
        preview = self._preview
        if preview is not None and not self._recording_database_error:
            try:
                with self.library.db:
                    self.library.db.execute('UPDATE sessions SET state=? WHERE id=?',
                                            (preview.state, preview.session.name))
            except DatabaseError as error:
                self._recording_database_error = '录制状态保存失败：' + str(error)
                task.cancel()
        if self._recording_database_error:
            self.recording_status += '\n' + self._recording_database_error
        self.preview_status = self.task_text = self.recording_status
        if task.terminal:
            if runner is not None: runner.cancel()
            self._recording_automation = None
            self._preview_timer.stop()
            if not task.durable:
                warning = '录制检查点未完整保存，结果不能视为成功，请检查 NVIDIA 状态及日志。'
                if warning not in self.warnings:
                    self.warnings.append(warning)
            self._last_recording_task = task
            self._recording_task = None
            if self.pipe_cleanup_pending:
                # Recording is terminal and never receives another hotkey;
                # public stop_preview now owns only this preview's cleanup.
                self._retain_pipe_cleanup()
            else:
                self._preview = None
                self.set_busy(False)
                self.refresh_recovery()
                # No output operation can reinterpret a merely terminal or
                # unknown external recorder as stopped.
                if (not self.automatic_recording and task.durable
                        and task.workflow.session.state == 'stopped'):
                    self.attach_recording_output(task)
        else:
            self.changed.emit()

    def attach_recording_output(self, task):
        """Only a durable, stopped task can become the current output scope."""
        if self.automatic_recording:
            raise DataError('用户端不提供成品视频检查，请直接打开视频目录。')
        self.ensure_idle()
        if (task is not self._last_recording_task or task.terminal is not True
                or task.durable is not True or task.workflow.session.state != 'stopped'):
            raise DataError('录制停止状态或检查点未核验，不能开始视频检查。')
        self._output_workflow = task.workflow
        self._output_restore_status = task.snapshot()['restore_status']
        try:
            self.discover_recording_output()
        except Exception as error:
            self.output_status = '视频检查尚未完成：' + str(error)
            self.changed.emit()

    def discover_recording_output(self):
        if self.automatic_recording:
            raise DataError('用户端不提供成品视频检查，请直接打开视频目录。')
        self.ensure_idle()
        if self._output_workflow is None:
            raise DataError('没有本次已停止且保留完整检查点的录制。')
        request = self._output_workflow.begin_output_discovery()
        self._start_output_worker('discover', request)

    def choose_recording_output(self, path, *, task_id, step_token):
        self.ensure_idle()
        workflow = self._output_workflow
        if workflow is None or (task_id, step_token) != (workflow.task_id, workflow.discovery_token):
            raise DataError('候选视频选择已过期或不属于当前任务。')
        workflow.choose_output(path, task_id=task_id, step_token=step_token)
        self.validate_recording_output()

    def validate_recording_output(self):
        if self.automatic_recording:
            raise DataError('用户端不提供成品视频检查，请直接打开视频目录。')
        self.ensure_idle()
        if self._output_workflow is None:
            raise DataError('没有等待验证的录制视频。')
        request = self._output_workflow.begin_output_validation()
        self._start_output_worker('validate', request)

    def _start_output_worker(self, phase, request):
        import json
        import sys
        from cs2pov.services.output_validation import output_json_value
        from cs2pov.services.output_worker import MAX_WORKER_JSON
        from cs2pov.storage.transaction import atomic_write
        workflow = self._output_workflow
        try:
            encoded = json.dumps(output_json_value(request), ensure_ascii=True, allow_nan=False).encode()
            if len(encoded) > MAX_WORKER_JSON:
                raise DataError('视频检查任务超过大小限制。')
            path = self.directory / 'cache' / ('output-' + request.request_id + '.json')
            no_redirection(path)
            atomic_write(path, encoded)
            process = QProcess(self)
            self._output_generation += 1
            generation = self._output_generation
            self._output_process, self._output_request = process, request
            self._output_stdout, self._output_stderr = bytearray(), bytearray()
            self._output_failure = ''
            env = QProcessEnvironment.systemEnvironment()
            env.insert('PYTHONPATH', str(Path(__file__).resolve().parents[2]))
            process.setProcessEnvironment(env)
            python = Path(sys.executable)
            if python.name.casefold() == 'pythonw.exe': python = python.with_name('python.exe')
            process.setProgram(str(python))
            prefix = ['--output-worker'] if getattr(sys, 'frozen', False) else ['-m', 'cs2pov.services.output_worker']
            args = [*prefix, '--phase', phase, '--request', str(path)]
            self._output_result_path = path.with_name('output-result-' + request.request_id + '.json')
            no_redirection(self._output_result_path)
            if self._output_result_path.exists(): raise DataError('视频检查结果位置已被占用。')
            args += ['--result', str(self._output_result_path)]
            if phase == 'validate': args += ['--probe', str(self.verified_local_resource('probe'))]
            process.setArguments(args)
            process.readyReadStandardOutput.connect(lambda: self._drain_output(process))
            process.readyReadStandardError.connect(lambda: self._drain_output(process))
            process.finished.connect(lambda code, status: self.finish_output_worker(process, generation, phase, request, workflow, code))
            process.errorOccurred.connect(lambda error: self.finish_output_worker(process, generation, phase, request, workflow, -1)
                if error == QProcess.ProcessError.FailedToStart else None)
            self.output_status = ('正在发现本次新视频；不会移动或删除视频。' if phase == 'discover'
                                  else '正在等待视频停止写入并检查视频流、音轨与时长；可以取消。')
            self.set_busy(True, self.output_status)
            process.start()
            QTimer.singleShot(15_000 if phase == 'discover' else 60_000,
                lambda: self._output_timeout(process))
        except Exception as error:
            method = workflow.fail_output_discovery if phase == 'discover' else workflow.fail_output_validation
            method(request, error)
            raise

    def _drain_output(self, process):
        from cs2pov.services.output_worker import MAX_WORKER_JSON
        if self._output_process is not process: return
        for data, output, limit in ((process.readAllStandardOutput(), self._output_stdout, MAX_WORKER_JSON),
                                    (process.readAllStandardError(), self._output_stderr, 131072)):
            if len(output) + len(data) > limit:
                self._output_failure = '视频检查输出超过大小限制。'
                process.kill()
            else:
                output.extend(bytes(data))

    def _output_timeout(self, process):
        if self._output_process is process:
            self._output_failure = '视频检查超时；原视频和恢复证据保留。'
            process.kill()

    def cancel_output(self):
        workflow = self._output_workflow
        if workflow is None or workflow.state == 'verified': return
        self._output_generation += 1
        if self._output_process is not None:
            self._output_process.kill()
        workflow.cancel_output_validation()
        self.output_status = '视频检查已取消；原视频保留，NVIDIA 停止状态不变。'
        self.changed.emit()

    def finish_output_worker(self, process, generation, phase, request, workflow, code):
        from cs2pov.services.output_worker import strict_json
        from cs2pov.services.output_validation import decode_discovery_result, decode_validation_result
        if self._output_process is not process: return
        self._drain_output(process)
        self._output_process = self._output_request = None
        cancelled = generation != self._output_generation or workflow is not self._output_workflow
        auto_validate = False
        try:
            if not cancelled:
                if self._output_failure: raise DataError(self._output_failure)
                from cs2pov.adapters.video import read_data_lease
                with read_data_lease(self._output_result_path) as result_signature:
                    from cs2pov.services.output_worker import MAX_WORKER_JSON
                    if result_signature.bytes > MAX_WORKER_JSON: raise DataError('视频检查结果超过大小限制。')
                    raw = strict_json(self._output_result_path.read_bytes())
                if code != 0 or 'error' in raw:
                    raise DataError(raw.get('error', '视频检查进程未正常完成。'))
                if phase == 'discover':
                    workflow.commit_output_discovery(request, decode_discovery_result(raw))
                    auto_validate = len(workflow.candidates) == 1
                    self.output_status = ('已找到唯一候选视频，继续检查。' if auto_validate else
                                          '发现多个候选视频，请选择本次录制的文件；时间相近不代表属于本任务。')
                else:
                    workflow.commit_output_validation(request, decode_validation_result(raw),
                        restore_status=self._output_restore_status,
                        payload={'trial_environment': self._trial_environment} if self._trial_environment else None)
                    self.output_status = '视频元数据已通过；请打开成片，检查视角、HUD、声音与起止余量。'
        except Exception as error:
            self.output_status = '视频检查失败：' + str(error)
            try:
                if workflow.state == ('discovering_video' if phase == 'discover' else 'validating_video'):
                    (workflow.fail_output_discovery if phase == 'discover' else workflow.fail_output_validation)(request, error)
            except Exception as save_error:
                self.output_status += '\n检查点保存失败：' + str(save_error)
        finally:
            process.deleteLater()
            self.set_busy(False)
            self.refresh_recording_recovery()
        if auto_validate and not cancelled:
            try: self.validate_recording_output()
            except Exception as error:
                self.output_status = '视频检查尚未完成：' + str(error)
                self.changed.emit()

    def confirm_recording_content(self, **values):
        self.ensure_idle()
        if self._output_workflow is None:
            raise DataError('没有等待成片确认的任务。')
        result = self._output_workflow.confirm_content(**values)
        self.output_status = '本次视频元数据和成片检查已通过；游戏恢复结果单独保留。'
        self.changed.emit()
        self.check_environment(issue_receipt=True)
        return result

    def cancel(self):
        # Request only: never pretends NVIDIA stopped or recovery finished.
        if self.busy:
            self.task_text = "已请求取消，等待当前操作安全收尾"
            self.cancellation_requested.emit()
            self.changed.emit()

    def check_environment(self, *, issue_receipt=False):
        receipt_workflow=self._output_workflow if issue_receipt else None
        receipt_record_id=getattr(receipt_workflow,'record_id',None)
        receipt_row=self.library.record(receipt_record_id) if receipt_record_id else None
        from dataclasses import replace
        self.settings = replace(self.settings,output_verified=False)
        self._environment_observation = None
        self._environment_generation += 1
        generation = self._environment_generation
        try:
            snapshot = inspect_installation(Path(self.settings.installation))
            self.environment = (f"CS2 {snapshot.patch_version} · {snapshot.root}\n"
                                "正在后台核验 HUD、NVIDIA 组件与驱动；未知版本不会标为已通过。")
            from cs2pov.services.environment_coordinator import EnvironmentCoordinator
            from cs2pov.services.recording_automation import overlay_executable
            if self._environment_coordinator is None:
                self._environment_coordinator = EnvironmentCoordinator(self,self.directory)
            hud = self.verified_local_resource('hud')
            settings = self.settings
            def completed(result, reason):
                if self.settings != settings or generation != self._environment_generation: return
                self._environment_observation = result
                from dataclasses import replace
                verified=False
                try:
                    if result is None or result.snapshot is None:
                        raise DataError(reason or '当前环境版本未知，试录验证保持未通过。')
                    from cs2pov.services.environment_receipt import ReceiptStore, issue_environment_receipt
                    from cs2pov.adapters.video import current_signature
                    store=ReceiptStore(self.directory/'environment-receipt.json')
                    receipt=store.load()
                    workflow=receipt_workflow
                    if issue_receipt and workflow is not None and workflow.state=='verified':
                        if self._output_workflow is not workflow or workflow.record_id!=receipt_record_id:
                            raise DataError('试录收据请求已不属于当前任务；不能认证另一任务。')
                        row=self.library.record(receipt_record_id)
                        if row!=receipt_row: raise DataError('试录记录在环境检查期间已变化。')
                        signature=current_signature(workflow.metadata.path)
                        receipt=issue_environment_receipt(result.snapshot,row,current_signature=signature)
                        store.save(receipt)
                    if receipt is not None:
                        row=self.library.record(receipt.record_id)
                        if row is not None:
                            signature=current_signature(receipt.video_signature.path)
                            verified=receipt.valid_for(result.snapshot,row,signature)
                    self.environment=(f'CS2 {result.snapshot.cs2_version} · NVIDIA 组件 {result.snapshot.nvidia_version}'
                        f' · 驱动 {result.snapshot.driver_version}\n'+
                        ('当前环境试录收据已核验。' if verified else '当前环境尚无有效的完整试录收据。'))
                except Exception as error:
                    self.environment='环境核验尚未通过：'+str(error)
                candidate_settings=replace(self.settings,output_verified=verified)
                try:
                    self.settings_file.save(asdict(candidate_settings))
                    self.settings=candidate_settings
                except Exception as error:
                    self.settings=replace(self.settings,output_verified=False)
                    self._environment_observation=None
                    self.environment='环境结果保存失败；验证保持未通过：'+str(error)
                self.changed.emit()
            if not self._environment_coordinator.start(settings,hud,overlay_executable(),
                    overlay_executable().with_name('NVIDIA App.exe'),completed):
                self.environment+='\n已有环境检查正在执行，请等待结果。'
        except (OSError, ValueError, RuntimeError) as error:
            self.environment = "CS2 检查未通过：" + str(error)
        self.check_video()

    def restore_session(self, session: Path):
        """Trusted targets derive from configured roots, never journal paths."""
        import json
        import re
        from cs2pov.services.hud_trial import CONFIG_NAMES, TrialCheck, trial_transaction
        self.ensure_idle()
        no_redirection(session)
        if session.parent != self.directory / "sessions" or not session.is_dir():
            raise DataError("请选择当前数据目录内的会话。")
        if not self.settings.installation or not self.settings.cfg:
            raise DataError("请先填写并确认游戏安装目录和用户配置目录。")
        try:
            raw = json.loads(read_small(safe_path(session, session / "trial-context.json")))
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            raise DataError("恢复上下文无法读取，保留文件及备份：" + str(error)) from error
        if not isinstance(raw, dict):
            raise DataError("恢复上下文结构无效，保留文件及备份。")
        if (not isinstance(raw.get("installation"), str)
                or Path(raw["installation"]).absolute() != Path(self.settings.installation).absolute()):
            raise DataError("备份游戏安装目录与当前选择不一致，保留备份并停止恢复。")
        gameinfo_sha256 = raw.get("gameinfo_sha256")
        if not isinstance(gameinfo_sha256, str) or not re.fullmatch("[0-9a-f]{64}", gameinfo_sha256):
            raise DataError("恢复上下文的原文件 SHA256 无效，保留文件及备份。")
        if raw.get("patch_version") is not None and not isinstance(raw["patch_version"], str):
            raise DataError("恢复上下文的游戏版本无效，保留文件及备份。")
        cfg = Path(self.settings.cfg)
        allowed = {str(cfg / name): cfg / name for name in CONFIG_NAMES}
        listed = raw.get("protected_configs")
        if (not isinstance(listed, list) or not all(isinstance(item, str) for item in listed)
                or len(set(listed)) != len(listed) or any(item not in allowed for item in listed)):
            raise DataError("备份用户配置与当前选择不一致，保留备份并停止恢复。")
        check = TrialCheck(Path(self.settings.installation), gameinfo_sha256, Path("unused.dem"),
                           Path(self.settings.video_directory or self.directory), 0,
                           tuple(allowed[item] for item in listed), raw.get("patch_version"))
        transaction = trial_transaction(check, session)
        transaction.load()
        if transaction.data["entries"].get("gameinfo", {}).get("original") != gameinfo_sha256:
            raise DataError("恢复上下文与事务原文件 SHA256 不一致，保留文件及备份。")
        if transaction.data["complete"]:
            # A completed file journal with a stale database row is only an
            # index reconciliation. Never rewrite current files/attributes:
            # they may have been legitimately edited after the original exit.
            self._verify_completed_recovery(transaction, gameinfo_sha256)
            results = {key: "restored" for key in transaction.targets}
        else:
            results = transaction.restore()
        if not transaction.data["complete"]:
            self.refresh_recovery()
            raise DataError("恢复未完成：" + json.dumps(results, ensure_ascii=False))
        try:
            with self.library.db:
                self.library.db.execute("UPDATE sessions SET state='complete' WHERE id=?", (session.name,))
        finally:
            self.refresh_recovery()
        return results

    @staticmethod
    def _verify_completed_recovery(transaction, gameinfo_sha256):
        data = transaction.data
        entries = data["entries"]
        if (data["prepared"] is not True or set(entries) != set(transaction.targets)
                or any(entry["state"] != "restored" for entry in entries.values())
                or entries["gameinfo"]["original"] != gameinfo_sha256):
            raise DataError("已完成事务的白名单或原文件记录不完整，保留备份并停止协调数据库。")
        if not transaction.can_modify():
            raise DataError("CS2 未关闭或进程状态无法确认，禁止协调恢复状态。")
        def verify_files():
            for key, target in transaction.targets.items():
                target = safe_path(transaction.root, target)
                entry = entries[key]
                current = digest(read_small(target)) if target.exists() else None
                if current != entry["original"]:
                    raise DataError(f"已恢复文件后来发生变化：{key}；保留当前内容及备份，不能确认恢复。")
                if current is not None:
                    backup = safe_path(transaction.session, transaction.session / f"{key}.backup")
                    if digest(read_small(backup)) != entry["original"]:
                        raise DataError(f"原文件备份校验失败：{key}；保留文件，不能确认恢复。")
                    if attributes(target) != entry["attributes"]:
                        raise DataError(f"已恢复文件属性后来发生变化：{key}；保留当前属性，不能确认恢复。")
        verify_files()
        if not transaction.can_modify():
            raise DataError("核验后 CS2 状态变化，禁止协调恢复状态。")
        # Recheck the whole set at the completion boundary: a later read or a
        # process-state callback may invalidate an earlier file observation.
        verify_files()
        if not transaction.can_modify():
            raise DataError("恢复收尾时 CS2 状态变化，禁止协调恢复状态。")

    def recovery_details(self, session: Path):
        import json
        no_redirection(session)
        if session.parent != self.directory / "sessions" or not session.is_dir():
            raise DataError("不是当前数据目录内的会话；请检查数据库与备份。")
        raw = json.loads(read_small(safe_path(session, session / "journal.json")))
        entries = raw.get("entries")
        if not isinstance(entries, dict) or not entries:
            raise DataError("事务记录损坏，保留原文件和备份。")
        names = {"gameinfo": "游戏搜索路径", "pov": "临时 HUD 文件"}
        lines = ["以下是日志状态；恢复时仍会重新校验文件和备份。"]
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                raise DataError("事务记录结构损坏，不能据此恢复。")
            name = names.get(key, "用户配置 " + key.removeprefix("config_"))
            lines.append(f"{name}：{entry.get('state', '未知')} · 原文件 SHA256：{entry.get('original') or '原本不存在'}")
        return "\n".join(lines)
