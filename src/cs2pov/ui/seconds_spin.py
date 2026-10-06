"""Whole-second presentation; retain imported Demo tick positions internally."""
from PySide6.QtWidgets import QDoubleSpinBox


class SecondsSpinBox(QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDecimals(6)
        self.setSingleStep(1)

    def textFromValue(self, value):
        return f'{value:.0f}'

    def valueFromText(self, text):
        return round(super().valueFromText(text))
