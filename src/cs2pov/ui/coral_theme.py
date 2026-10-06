"""Warm coral materials and a joined navigation surface; no game behavior."""
from PySide6.QtCore import Qt, Property, QPropertyAnimation, QEasingCurve, QRectF
from PySide6.QtGui import QPainter, QColor, QLinearGradient, QPainterPath
from PySide6.QtWidgets import QWidget, QBoxLayout

SURFACE = "#fff9f5"
STYLE = """
QWidget { font-family: 'Microsoft YaHei UI'; font-size: 14px; color: #3d302c; }
QMainWindow, QWidget#canvas { background: #f6d6c8; }
QWidget#page, QStackedWidget { background: #fff9f5; }
QWidget#sidebar, QLabel { background: transparent; }
QFrame#card { background: qlineargradient(x1:0,y1:0,x2:0.8,y2:1,stop:0 #fffefa,stop:1 #fff2e9); border: 1px solid #ffffff; border-radius: 22px; }
QFrame#statusBar { background: #fff2e9; border: 1px solid #ffffff; border-radius: 18px; }
QFrame#emptyPreview { background: #f9e8dd; border: 1px dashed #dfbdae; border-radius: 16px; }
QLabel#heading { font-size: 27px; font-weight: 700; color: #382923; }
QLabel#heroTitle { font-size: 30px; font-weight: 700; color: #382923; }
QLabel#subtitle { font-size: 15px; color: #80665b; }
QLabel#section { font-size: 17px; font-weight: 700; }
QLabel#muted { color: #80665b; font-size: 13px; }
QLabel#eyebrow { color: #a66b56; font-size: 11px; font-weight: 600; }
QLabel#pill { background: #fde5d9; color: #9b4f37; border: 1px solid #ffffff; border-radius: 10px; padding: 7px 10px; font-size: 12px; }
QLabel#emptyTitle { font-size: 16px; font-weight: 600; color: #80665b; }
QLabel#warning { background: #ffe7d6; color: #913b1f; border: 1px solid #efb58f; padding: 12px; border-radius: 12px; }
QPushButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #fffefa,stop:1 #f9e9df); border: 1px solid #ead3c6; border-radius: 12px; padding: 9px 15px; font-size: 13px; }
QPushButton:hover { background: #ffe7db; border-color: #ef9b83; }
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus { border: 2px solid #dd7159; }
QPushButton:pressed { background: #f9d5c6; padding-top: 10px; padding-bottom: 8px; }
QPushButton:disabled { color: #a28d82; background: #f5ebe5; border-color: #eadfd6; }
QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ffb29a,stop:0.18 #f58b72,stop:1 #e8755f); color: #44271e; border: 1px solid #ed8d75; font-weight: 600; }
QPushButton#primary:hover { background: #f69a80; border-color: #df6b52; }
QPushButton#primary:pressed { background: #e97a61; }
QPushButton#primary:disabled { background: #efcabe; color: #8f7063; border-color: #e8c7b8; }
QPushButton#nav { text-align: left; color: #6e493a; border: 1px solid transparent; background: transparent; border-radius: 14px; padding: 13px 16px; font-size: 14px; }
QPushButton#nav:hover { background: rgba(255,255,255,75); }
QPushButton#nav:checked { color: #963f2d; background: transparent; font-weight: 600; }
QPushButton#nav:focus { border: 1px dotted #c86d54; }
QPushButton#mode { padding: 8px 10px; border-radius: 10px; }
QPushButton#mode:checked { background: #f7b39a; color: #633120; border: 1px solid #ed9d80; font-weight: 600; }
QPushButton#quiet { color: #a95740; background: transparent; border: none; padding: 7px 0; text-align: left; }
QPushButton#quiet:hover { color: #85351f; }
QPushButton#quiet:disabled { color: #a28d82; }
QLineEdit, QComboBox, QDoubleSpinBox { background: #fffcf8; border: 1px solid #ead8cb; border-radius: 10px; padding: 8px; selection-background-color: #f9c9b5; selection-color: #382923; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #cda793; border-radius: 5px; background: #fffcf8; }
QCheckBox::indicator:checked { background: #ef9379; border: 2px solid #fff8f0; }
QListWidget { background: #fffcf8; border: 1px solid #ead8cb; border-radius: 14px; padding: 10px; }
QListWidget::item { padding: 9px; border-radius: 8px; }
QListWidget::item:selected { background: #fde1d3; color: #853d2c; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 0; }
QScrollBar::handle:vertical { background: #dfbfae; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollArea { border: none; background: #fff9f5; }
"""

class CoralSidebar(QWidget):
    def __init__(self):
        super().__init__()
        self._selection_y = 0.0
        self.selection_height = 48
        self.buttons = []
        self.selected_index = 0
        self.animation = QPropertyAnimation(self, b"selectionY", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_y(self): return self._selection_y
    def set_y(self, value): self._selection_y = value; self.update()
    selectionY = Property(float, get_y, set_y)

    def select(self, index, animate=True):
        self.selected_index = index
        if not self.buttons: return
        self.layout().activate()
        target = self.buttons[index].geometry()
        self.selection_height = target.height()
        self.animation.stop()
        if animate and self.isVisible():
            self.animation.setStartValue(self._selection_y)
            self.animation.setEndValue(float(target.top()))
            self.animation.start()
        else: self.set_y(float(target.top()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.select(self.selected_index, False)

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor("#f9dece")); gradient.setColorAt(.55, QColor("#f5c3af")); gradient.setColorAt(1, QColor("#fbe7da"))
        p.fillRect(self.rect(), gradient)
        if not self.buttons: return
        y, h, w, r = self._selection_y, self.selection_height, self.width(), 20
        path = QPainterPath()
        path.moveTo(w, y-r); path.quadTo(w, y, w-r, y)
        path.lineTo(28, y); path.quadTo(8, y, 8, y+r)
        path.lineTo(8, y+h-r); path.quadTo(8, y+h, 28, y+h)
        path.lineTo(w-r, y+h); path.quadTo(w, y+h, w, y+h+r)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(SURFACE)); p.drawPath(path)


class ResponsiveSplit(QWidget):
    """Keep the editor wide; stack the helper panel on small windows."""
    def __init__(self):
        super().__init__()
        self.columns = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(18)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        wide = self.width() >= 920
        self.columns.setDirection(QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom)
        self.columns.setStretch(0, 3 if wide else 0)
        self.columns.setStretch(1, 1 if wide else 0)
