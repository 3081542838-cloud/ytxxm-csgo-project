"""First-run portable resource preparation, outside the GUI process."""
import json
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout, QProgressBar

from cs2pov.storage.transaction import no_redirection


class ResourceSetupDialog(QDialog):
    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        from cs2pov.ui.window import ICON
        self.setWindowIcon(QIcon(str(ICON)))
        self.setWindowTitle('虾米pov · 首次准备资源')
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        self.message = QLabel('正在从固定来源准备 HUD 和视频检查工具。\n首次需要联网，约下载 36 MB；完成后可离线使用。')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.progress = QProgressBar(); self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.retry = QPushButton('重试'); self.retry.setEnabled(False)
        self.retry.clicked.connect(self.start)
        layout.addWidget(self.retry)
        self.cancel = QPushButton('取消并关闭')
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(self.cancel)
        self.process = QProcess(self)
        self.process.finished.connect(self.worker_finished)
        self.process.errorOccurred.connect(self.failed)
        self.deadline = QTimer(self); self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self.timed_out)
        self.result = None
        self.cancelled = False
        self.expired = False
        QTimer.singleShot(0, self.start)

    def start(self):
        if self.cancelled: return
        if self.process.state() != QProcess.ProcessState.NotRunning: return
        self.retry.setEnabled(False)
        self.expired = False
        self.progress.setRange(0, 0)
        self.message.setText('正在从固定来源下载并校验资源，请稍等。\n完成前不会启动游戏或录制。')
        self.result = self.root / 'data' / ('resource-result-' + uuid.uuid4().hex + '.json')
        no_redirection(self.result)
        arguments = ['--resource-worker', '--root', str(self.root), '--result', str(self.result)]
        if not getattr(sys, 'frozen', False):
            arguments.insert(0, str(Path(__file__).resolve().parents[1] / 'app.py'))
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert('PYINSTALLER_RESET_ENVIRONMENT', '1')
        self.process.setProcessEnvironment(environment)
        self.process.setProgram(sys.executable)
        self.process.setArguments(arguments)
        self.process.start()
        self.deadline.start(180000)

    def worker_finished(self, code, _status):
        self.deadline.stop()
        if self.cancelled: return
        if self.expired:
            self.show_error('本次下载超时，请重试。'); return
        try:
            no_redirection(self.result)
            if not self.result.is_file() or self.result.stat().st_size > 1_048_576:
                raise RuntimeError('资源准备进程未返回结果。')
            report = json.loads(self.result.read_text(encoding='utf-8'))
            if code != 0 or report.get('ok') is not True:
                raise RuntimeError(report.get('error', '资源准备未完成。'))
            from cs2pov.services.resource_setup import resources_ready
            if not resources_ready(self.root):
                raise RuntimeError('资源准备结果不完整，请重试。')
        except Exception as error:
            self.show_error(str(error)); return
        self.accept()

    def show_error(self, text):
        if self.cancelled: return
        self.deadline.stop()
        self.progress.setRange(0, 1); self.progress.setValue(0)
        self.message.setText('资源准备未完成：' + text + '\n检查网络后可重试，已有配置和文件保留。')
        self.retry.setEnabled(self.process.state() == QProcess.ProcessState.NotRunning)

    def failed(self, _error):
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self.show_error('准备进程无法启动：' + self.process.errorString())

    def timed_out(self):
        self.expired = True
        self.process.kill()
        self.show_error('本次下载超时，请重试。')

    def reject(self):
        self.cancelled = True
        self.deadline.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        super().reject()


def ensure_resources(root):
    from cs2pov.services.resource_setup import resources_ready
    if resources_ready(root): return True
    dialog = ResourceSetupDialog(root)
    return dialog.exec() == QDialog.DialogCode.Accepted
