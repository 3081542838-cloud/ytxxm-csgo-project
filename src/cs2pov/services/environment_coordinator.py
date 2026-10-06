"""Asynchronous, one-at-a-time coordination of current environment checks."""
import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QThread, QTimer, Signal

from cs2pov.adapters.video import read_data_lease
from cs2pov.services.environment_worker import (MAX_WORKER_JSON, _write_new_result,
    decode_observation_result, make_observation_request, strict_json)
from cs2pov.storage.settings import DataError
from cs2pov.storage.transaction import no_redirection


class EnvironmentCoordinator(QObject):
    """Observe in a disposable worker and deliver on the owning Qt thread."""
    finished = Signal(object, str)
    TIMEOUT_MS = 15_000

    def __init__(self, owner, data_directory):
        super().__init__(owner)
        self.directory = Path(data_directory).absolute()
        no_redirection(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cache = self.directory / "cache"
        no_redirection(self.cache)
        self.cache.mkdir(exist_ok=True)
        self._process = self._request = self._timer = None
        self._generation = 0
        self._active_generation = None
        self._callback = None
        self._result_path = None
        self._stdout, self._stderr = bytearray(), bytearray()
        self._failure = self.reason = ""
        self._kill_requested = False
        self._current_result = None
        self._started_wall = None

    @property
    def busy(self):
        return self._process is not None

    @property
    def current_result(self):
        return self._current_result

    @property
    def snapshot(self):
        return self._current_result.snapshot if self._current_result is not None else None

    def start(self, settings, hud_resource, overlay_executable, nvidia_app_executable, on_result):
        if self.busy:
            return False
        if QThread.currentThread() != self.thread():
            return False
        self._current_result = None
        self.reason = "正在只读观察当前环境；试录状态尚未验证。"
        callback = on_result if callable(on_result) else None
        process = None
        try:
            if callback is None:
                raise DataError("环境观察需要有效完成回调。")
            request = make_observation_request(settings, hud_resource, overlay_executable,
                                               nvidia_app_executable=nvidia_app_executable)
            encoded = json.dumps(request.to_dict(), ensure_ascii=True, allow_nan=False).encode("utf-8")
            strict_json(encoded)
            request_path = self.cache / ("environment-request-" + request.request_id + ".json")
            result_path = self.cache / ("environment-result-" + request.request_id + ".json")
            no_redirection(request_path)
            no_redirection(result_path)
            if result_path.exists():
                raise DataError("环境观察结果位置已被占用，未启动检查。")
            _write_new_result(request_path, encoded)
            # Validate the exact file version before handing its path to the
            # worker. Only JSON leases avoid blocking unrelated cache writes.
            with read_data_lease(request_path) as signature:
                if signature.bytes > MAX_WORKER_JSON:
                    raise DataError("环境观察请求超过大小限制。")
                with request_path.open("rb") as stream:
                    if strict_json(stream.read(MAX_WORKER_JSON + 1)) != request.to_dict():
                        raise DataError("环境观察请求在启动前变化。")
            process = QProcess(self)
            self._generation += 1
            generation = self._generation
            self._process, self._request = process, request
            self._active_generation = generation
            self._callback, self._result_path = callback, result_path
            self._stdout, self._stderr = bytearray(), bytearray()
            self._failure, self._kill_requested = "", False
            environment = QProcessEnvironment.systemEnvironment()
            environment.insert("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
            process.setProcessEnvironment(environment)
            executable = Path(sys.executable)
            if executable.name.casefold() == "pythonw.exe":
                executable = executable.with_name("python.exe")
            process.setProgram(str(executable))
            prefix = ["--environment-worker"] if getattr(sys, "frozen", False) else ["-m", "cs2pov.services.environment_worker"]
            process.setArguments([*prefix, "--mode", "observe", "--request", str(request_path), "--result", str(result_path)])
            process.readyReadStandardOutput.connect(lambda: self._drain(process))
            process.readyReadStandardError.connect(lambda: self._drain(process))
            process.finished.connect(lambda code, status: self._finish(process, generation, code, status))
            process.errorOccurred.connect(lambda error: self._error(process, generation, error))
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self.TIMEOUT_MS)
            timer.timeout.connect(lambda: self._timeout(process, generation))
            self._timer = timer
            self._started_wall = time.time()
            timer.start()
            process.start()
            return True
        except Exception as error:
            reason = "环境观察未完成：" + str(error)
            if self._process is process and process is not None:
                self._failure = reason
                if process.state() != QProcess.ProcessState.NotRunning:
                    self._kill(process)
                else:
                    self._finish(process, self._active_generation, -1, QProcess.ExitStatus.NormalExit)
                return True
            if process is not None:
                process.deleteLater()
            self._deliver(None, reason, callback)
            return False

    def _kill(self, process):
        if process is self._process and not self._kill_requested:
            self._kill_requested = True
            process.kill()

    def cancel(self):
        if QThread.currentThread() != self.thread():
            return
        self._current_result = None
        if self._process is None:
            return
        self._generation += 1
        if not self._failure:
            self._failure = "环境观察已取消；晚到结果不能用于试录。"
        self.reason = self._failure
        process = self._process
        self._kill(process)
        if process.state() == QProcess.ProcessState.NotRunning:
            self._finish(process, self._active_generation, -1, QProcess.ExitStatus.NormalExit)

    def _drain(self, process):
        if process is not self._process:
            return
        for value, output in ((process.readAllStandardOutput(), self._stdout),
                              (process.readAllStandardError(), self._stderr)):
            value = bytes(value)
            if len(output) + len(value) > MAX_WORKER_JSON:
                if not self._failure:
                    self._failure = "环境观察进程输出超过大小限制。"
                self._kill(process)
            else:
                output.extend(value)

    def _timeout(self, process, generation):
        if process is not self._process or generation != self._active_generation:
            return
        if not self._failure:
            self._failure = "环境观察超过 15 秒已超时；结果保持未知。"
        self._kill(process)
        if process.state() == QProcess.ProcessState.NotRunning:
            self._finish(process, generation, -1, QProcess.ExitStatus.NormalExit)

    def _error(self, process, generation, error):
        if process is not self._process:
            return
        if not self._failure:
            self._failure = "环境观察进程失败：" + process.errorString()
        if error == QProcess.ProcessError.FailedToStart or process.state() == QProcess.ProcessState.NotRunning:
            self._finish(process, generation, -1, QProcess.ExitStatus.CrashExit)

    def _finish(self, process, generation, code, status):
        if process is not self._process or generation != self._active_generation:
            return
        self._drain(process)
        callback, result, reason = self._callback, None, ""
        try:
            if generation != self._generation:
                raise DataError(self._failure or "环境观察已取消。")
            if self._failure:
                raise DataError(self._failure)
            if code != 0 or status != QProcess.ExitStatus.NormalExit:
                raise DataError("环境观察进程未正常完成；结果保持未知。")
            with read_data_lease(self._result_path) as signature:
                if signature.bytes > MAX_WORKER_JSON:
                    raise DataError("环境观察结果超过大小限制。")
                with self._result_path.open("rb") as stream:
                    raw = strict_json(stream.read(MAX_WORKER_JSON + 1))
            if "error" in raw:
                raise DataError(str(raw["error"])[:1024])
            result = decode_observation_result(raw, request=self._request)
            now = time.time()
            if (result.started_at < self._started_wall or result.observed_at > now
                    or result.observed_at - self._started_wall > self.TIMEOUT_MS / 1000):
                raise DataError("环境观察时间不属于本次请求或已过期。")
            self._current_result = result
            if result.snapshot is None:
                reason, result = result.reason, None
        except Exception as error:
            self._current_result = None
            result = None
            reason = "环境观察未完成：" + str(error)
        finally:
            if self._timer is not None:
                self._timer.stop(); self._timer.deleteLater()
            self._process = self._request = self._timer = None
            self._active_generation = None
            self._callback = None
            process.deleteLater()
        self._deliver(result, reason, callback)

    def _deliver(self, result, reason, callback):
        self.reason = reason
        try:
            if callback is not None:
                callback(result, reason)
        except Exception as error:
            self._current_result = None
            result, reason = None, "环境观察完成回调失败：" + str(error)
            self.reason = reason
        self.finished.emit(result, reason)
