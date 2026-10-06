from PySide6.QtCore import QPoint, Qt
from cs2pov.ui.clip_timeline import ClipTimeline


def test_mouse_drag_cannot_cut_kills_or_cross_handles(qtbot):
    widget = ClipTimeline(); qtbot.addWidget(widget)
    widget.resize(640, 100); widget.show()
    widget.configure(range(1001), [300, 600], 100, 800)
    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(int(widget.x_for_tick(100)), 40))
    qtbot.mouseMove(widget, QPoint(900, 40))
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton)
    assert widget.start == 300 and widget.end == 800
    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(int(widget.x_for_tick(800)), 40))
    qtbot.mouseMove(widget, QPoint(-50, 40))
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton)
    assert widget.start == 300 and widget.end == 600


def test_keyboard_tab_and_sparse_ticks(qtbot):
    widget = ClipTimeline(); qtbot.addWidget(widget); widget.show()
    widget.configure([10, 20, 30, 40, 50], [30, 30], 10, 50)
    widget.setFocus()
    qtbot.keyClick(widget, Qt.Key.Key_Right)
    assert widget.start == 20
    qtbot.keyClick(widget, Qt.Key.Key_Tab)
    qtbot.keyClick(widget, Qt.Key.Key_Left)
    assert widget.active == 1 and widget.end == 40
    qtbot.keyClick(widget, Qt.Key.Key_Left)
    qtbot.keyClick(widget, Qt.Key.Key_Left)
    assert widget.end == 30 and widget.start == 20
    widget.active = 0; widget.move_handle(100)
    assert widget.start == 20  # Positive duration even when all kills share a tick.
    widget.resize(120, 96)
    assert widget.x_for_tick(30) == widget.width()/2  # Controls enforce a usable minimum width.
    widget.clear()
    qtbot.keyClick(widget, Qt.Key.Key_Right)
    assert not widget.ticks


def test_second_nudges_clamp_at_kills_and_reset_outward_on_sparse_ticks(qtbot):
    widget = ClipTimeline(); qtbot.addWidget(widget)
    widget.configure(range(0, 1001, 3), [300, 600], 150, 750, rate=10, origin=0)
    changed = []
    widget.rangeChanged.connect(lambda a, b: changed.append((a, b)))
    widget.nudge(0, 1)
    assert widget.start == 159
    widget.nudge(1, -1)
    assert widget.end == 741
    widget.active = 0; widget.move_handle(500)
    widget.active = 1; widget.move_handle(400)
    assert widget.start == 300 and widget.end == 600
    widget.reset_button.click()
    assert (widget.start, widget.end) == (198, 702)
    assert changed[-1] == (198, 702)
    assert '10.2' in widget.summary.text()


def test_reset_respects_available_bounds_and_empty_state(qtbot):
    widget = ClipTimeline(); qtbot.addWidget(widget)
    widget.configure(range(250, 650), [300, 600], 280, 620, rate=10, origin=0)
    widget.reset_padding()
    assert (widget.start, widget.end) == (250, 649)
    assert widget.format_tick(649) == '01:04.9'
    widget.clear()
    assert not widget.reset_button.isEnabled()
    assert all(not button.isEnabled() for button in widget.controls)
    widget.reset_padding()
    assert (widget.start, widget.end) == (0, 0)
