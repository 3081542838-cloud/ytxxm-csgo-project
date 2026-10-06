"""Small consistent outline icons painted locally, without font dependencies."""
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor, QPolygonF


def outline_icon(name, color="#80665b", size=24):
    image = QPixmap(size * 2, size * 2)
    image.setDevicePixelRatio(2)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24, size / 24)
    painter.setPen(QPen(QColor(color), 1.7, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if name == "home":
        painter.drawPolyline(QPolygonF([QPointF(3, 10), QPointF(12, 3), QPointF(21, 10)]))
        painter.drawPolyline(QPolygonF([QPointF(5, 9), QPointF(5, 21), QPointF(19, 21), QPointF(19, 9)]))
        painter.drawRect(10, 14, 4, 7)
    elif name == "film":
        painter.drawRoundedRect(4, 3, 16, 18, 2, 2)
        painter.drawLine(8, 3, 8, 21); painter.drawLine(16, 3, 16, 21)
        for y in (8, 13, 18):
            painter.drawLine(4, y, 8, y); painter.drawLine(16, y, 20, y)
    elif name == "monitor":
        painter.drawRoundedRect(3, 4, 18, 13, 2, 2)
        painter.drawLine(12, 17, 12, 21); painter.drawLine(8, 21, 16, 21)
    elif name == "clock":
        painter.drawEllipse(3, 3, 18, 18)
        painter.drawLine(12, 7, 12, 12); painter.drawLine(12, 12, 16, 14)
    elif name == "settings":
        painter.drawEllipse(5, 5, 14, 14); painter.drawEllipse(9, 9, 6, 6)
        for x1, y1, x2, y2 in ((12, 2, 12, 5), (12, 19, 12, 22), (2, 12, 5, 12),
                                (19, 12, 22, 12), (5, 5, 7, 7), (17, 17, 19, 19),
                                (5, 19, 7, 17), (17, 7, 19, 5)):
            painter.drawLine(x1, y1, x2, y2)
    elif name == "folder":
        painter.drawPolyline(QPolygonF([QPointF(3, 8), QPointF(3, 5), QPointF(9, 5),
                                        QPointF(11, 8), QPointF(21, 8), QPointF(21, 20),
                                        QPointF(3, 20), QPointF(3, 8), QPointF(21, 8)]))
    elif name == "shield":
        painter.drawPolygon(QPolygonF([QPointF(12, 3), QPointF(20, 6), QPointF(19, 15),
                                       QPointF(12, 21), QPointF(5, 15), QPointF(4, 6)]))
        painter.drawPolyline(QPolygonF([QPointF(8, 12), QPointF(11, 15), QPointF(16, 9)]))
    elif name == "arrow":
        painter.drawLine(4, 12, 20, 12)
        painter.drawPolyline(QPolygonF([QPointF(14, 6), QPointF(20, 12), QPointF(14, 18)]))
    painter.end()
    return QIcon(image)
