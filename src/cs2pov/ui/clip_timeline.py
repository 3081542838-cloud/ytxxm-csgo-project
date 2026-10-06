"""Two tick-snapped handles, with an immutable kill core."""
from bisect import bisect_left, bisect_right
import math
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import QWidget, QPushButton, QLabel, QHBoxLayout, QVBoxLayout


class ClipTimeline(QWidget):
    rangeChanged = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ticks = []
        self.kills = []
        self.start = self.end = 0
        self.active = 0
        self.dragging = False
        self.rate, self.origin = 1.0, 0
        self.setMinimumHeight(254)
        self.setMouseTracking(True)
        self.setStyleSheet("ClipTimeline { background: #fffcf8; } QPushButton { color: #ce6e55; background: #fff4ec; border: 1px solid #ead0c0; border-radius: 6px; padding: 5px 10px; } QPushButton:hover { background: #fbd9c8; } QPushButton:focus { border: 2px solid #ce6e55; } QPushButton:disabled { color: #8e9aa6; background: #f9eee6; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 8)
        top = QHBoxLayout()
        self.start_label, self.end_label = QLabel(), QLabel()
        self.controls = []
        for handle, label in ((0, self.start_label), (1, self.end_label)):
            if handle: top.addStretch()
            top.addWidget(label)
            for direction, caption in ((-1, "−"), (1, "+")):
                control = QPushButton(caption)
                control.setFixedSize(34, 32)
                control.setAccessibleName(("开始" if handle == 0 else "结束") + ("提前" if direction < 0 else "延后") + " 1 秒")
                control.setToolTip(control.accessibleName())
                control.clicked.connect(lambda checked=False, h=handle, d=direction: self.nudge(h, d))
                top.addWidget(control); self.controls.append(control)
        layout.addLayout(top)
        layout.addStretch()
        bottom = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setStyleSheet("color: #80665b; font-size: 12px;")
        bottom.addWidget(self.summary); bottom.addStretch()
        self.reset_button = QPushButton("恢复前后 10 秒")
        self.reset_button.setStyleSheet("QPushButton { background: #ce6e55; color: white; border: none; padding: 8px 12px; } QPushButton:hover { background: #a94e37; }")
        self.reset_button.clicked.connect(self.reset_padding)
        bottom.addWidget(self.reset_button); layout.addLayout(bottom)
        self.refresh_labels()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("多杀时间轴：Tab 切换左右手柄，方向键调整一个 tick")

    def configure(self, ticks, kills, start, end, *, rate=1.0, origin=0):
        self.ticks, self.kills = list(ticks), list(kills)
        self.start, self.end = start, end
        self.rate, self.origin = rate, origin
        self.refresh_labels()
        self.dragging = False
        self.update()

    def clear(self):
        self.configure([], [], 0, 0)

    def x_for_tick(self, tick):
        if not self.ticks or self.ticks[-1] == self.ticks[0]:
            return 20.0
        return 20 + (self.width()-40) * (tick-self.ticks[0]) / (self.ticks[-1]-self.ticks[0])

    def move_handle(self, value):
        if not self.ticks:
            return
        index = min(bisect_left(self.ticks, value), len(self.ticks)-1)
        if index and abs(self.ticks[index-1]-value) < abs(self.ticks[index]-value):
            index -= 1
        value = self.ticks[index]
        if self.active == 0:
            maximum = min(self.kills[0], self.end-1)
            index = min(index, bisect_left(self.ticks, maximum+1)-1)
            self.start = self.ticks[max(0, index)]
        else:
            minimum = max(self.kills[-1], self.start+1)
            index = max(index, bisect_left(self.ticks, minimum))
            self.end = self.ticks[min(len(self.ticks)-1, index)]
        self.rangeChanged.emit(self.start, self.end)
        self.refresh_labels()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.ticks:
            self.setFocus()
            x = event.position().x()
            self.active = int(abs(x-self.x_for_tick(self.end)) < abs(x-self.x_for_tick(self.start)))
            self.dragging = True
            self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragging and self.ticks:
            fraction = (event.position().x()-20) / max(1, self.width()-40)
            self.move_handle(self.ticks[0] + fraction*(self.ticks[-1]-self.ticks[0]))

    def mouseReleaseEvent(self, event):
        self.dragging = False

    def focusNextPrevChild(self, forward):
        if self.ticks and ((forward and self.active == 0) or (not forward and self.active == 1)):
            self.active = 1 if forward else 0
            self.update()
            return True
        self.active = 0 if forward else 1
        return super().focusNextPrevChild(forward)

    def keyPressEvent(self, event):
        if self.ticks and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            current = self.start if self.active == 0 else self.end
            index = bisect_left(self.ticks, current) + (-1 if event.key() == Qt.Key.Key_Left else 1)
            self.move_handle(self.ticks[max(0, min(index, len(self.ticks)-1))])
        else:
            super().keyPressEvent(event)

    def format_tick(self, tick):
        seconds = max(0, (tick-self.origin)/self.rate)
        minutes, seconds = divmod(seconds, 60)
        return f"{int(minutes):02d}:{seconds:04.1f}"

    def refresh_labels(self):
        available = bool(self.ticks and self.kills)
        for control in [*self.controls, self.reset_button]:
            control.setEnabled(available)
        self.start_label.setText("开始  " + (self.format_tick(self.start) if available else "—"))
        self.end_label.setText("结束  " + (self.format_tick(self.end) if available else "—"))
        self.summary.setText((f"前置 {(self.kills[0]-self.start)/self.rate:.1f} 秒 · 后置 {(self.end-self.kills[-1])/self.rate:.1f} 秒 · 总长 {(self.end-self.start)/self.rate:.1f} 秒" if available else "选择多杀候选后调整片段"))

    def nudge(self, handle, direction):
        if self.ticks:
            self.active = handle
            self.move_handle((self.start if handle == 0 else self.end) + direction*self.rate)

    def reset_padding(self):
        if not self.ticks or not self.kills:
            return
        left = max(0, bisect_right(self.ticks, self.kills[0]-10*self.rate)-1)
        right = min(len(self.ticks)-1, bisect_left(self.ticks, self.kills[-1]+10*self.rate))
        self.start, self.end = self.ticks[left], self.ticks[right]
        self.refresh_labels()
        self.rangeChanged.emit(self.start, self.end)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fffcf8"))
        if not self.ticks:
            painter.setPen(QColor("#a28475"))
            painter.drawText(QRectF(20, 60, self.width()-40, 100), Qt.AlignmentFlag.AlignCenter, "选择一个可录多杀片段后调整范围")
            return
        y, height = 76, 54
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f9eee6"))
        painter.drawRoundedRect(QRectF(20, y, self.width()-40, height), 7, 7)
        left, right = self.x_for_tick(self.start), self.x_for_tick(self.end)
        painter.setBrush(QColor("#fbd9c8"))
        painter.drawRoundedRect(QRectF(left, y, right-left, height), 5, 5)
        font = QFont(self.font()); font.setPointSize(9); painter.setFont(font)
        count = max(2, min(10, (self.width()-40)//100))
        seconds_low = (self.ticks[0]-self.origin)/self.rate
        seconds_high = (self.ticks[-1]-self.origin)/self.rate
        ideal = max(0.001, (seconds_high-seconds_low)/count)
        magnitude = 10**math.floor(math.log10(ideal))
        step = next(multiplier*magnitude for multiplier in (1, 2, 5, 10) if multiplier*magnitude >= ideal)
        second = math.ceil(seconds_low/step)*step
        while second <= seconds_high:
            tick = self.origin + second*self.rate
            x = self.x_for_tick(tick)
            painter.setPen(QPen(QColor("#cfb6a5"), 1))
            painter.drawLine(int(x), y+height+5, int(x), y+height+13)
            painter.setPen(QColor("#917366"))
            label_x = max(20, min(self.width()-83, x-31))
            painter.drawText(QRectF(label_x, y+height+16, 63, 22), Qt.AlignmentFlag.AlignCenter, self.format_tick(tick))
            second += step
        last_label_right = -100
        for number, tick in enumerate(self.kills, 1):
            x = self.x_for_tick(tick)
            painter.setPen(QPen(QColor("#a96125"), 2))
            painter.drawLine(int(x), y-9, int(x), y+height+1)
            # Keep every marker; omit overlapping captions in dense clusters.
            label_x = max(20, min(self.width()-135, x-55))
            if label_x >= last_label_right + 8:
                painter.drawText(QRectF(label_x, y+height+43, 115, 22), Qt.AlignmentFlag.AlignCenter, f"{number} 杀 · {self.format_tick(tick)}")
                last_label_right = label_x+115
        for index, x in enumerate((left, right)):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#a94e37" if self.hasFocus() and self.active == index else "#ce6e55"))
            painter.drawRoundedRect(QRectF(x-10, y-7, 20, height+14), 6, 6)
            painter.setPen(QPen(QColor("#fffcf8"), 2))
            for offset in (-3, 3):
                painter.drawLine(int(x+offset), y+19, int(x+offset), y+35)
        painter.setPen(QColor("#917366"))
        painter.drawText(20, self.height()-53, "拖动珊瑚色手柄调整范围 · Tab 切换手柄 · 方向键精调")
