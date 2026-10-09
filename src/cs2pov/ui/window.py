"""Five pages backed by one Workspace. No game inputs in the view layer."""
from dataclasses import replace
import json
from pathlib import Path
from PySide6.QtCore import Qt, QUrl, QSize, QTimer
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QApplication, QPushButton, QLabel, QStackedWidget, QFileDialog, QLineEdit, QFormLayout,
    QComboBox, QDoubleSpinBox, QCheckBox, QMessageBox, QListWidget, QScrollArea,
    QFrame, QListWidgetItem, QSizePolicy)
from cs2pov.storage.settings import HudPreset, Settings, DataError
from cs2pov.storage.library import decode_record_payload
from cs2pov.storage.transaction import no_redirection
from cs2pov.adapters.video import VIDEO_SUFFIXES
from cs2pov.ui.icons import outline_icon
from cs2pov.ui.clip_timeline import ClipTimeline
from cs2pov.ui.seconds_spin import SecondsSpinBox
from cs2pov.services.highlights import candidates

ICON = Path(__file__).resolve().parents[1] / "resources/shrimp.ico"

from cs2pov.ui.coral_theme import STYLE, CoralSidebar, ResponsiveSplit



def label(text, name=None):
    item = QLabel(text)
    item.setWordWrap(True)
    item.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        item.setObjectName(name)
    return item


def button(text, action, primary=False):
    item = QPushButton(text)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        item.setObjectName("primary")
    item.clicked.connect(action)
    return item


def card(parent, title):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(24, 22, 24, 22)
    layout.setSpacing(14)
    layout.addWidget(label(title, "section"))
    parent.addWidget(frame)
    return layout


class MainWindow(QMainWindow):
    def __init__(self, workspace):
        super().__init__()
        QApplication.instance().setStyle("Fusion")
        self.workspace = workspace
        self.setAcceptDrops(True)
        self.setWindowTitle("虾米 POV · CS2 本地回放录制")
        self.setWindowIcon(QIcon(str(ICON)))
        self.resize(1280, 850)
        self.setMinimumSize(860, 620)
        self.setStyleSheet(STYLE)
        self.editor_widgets = []
        root = QWidget()
        root.setObjectName("canvas")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        side = CoralSidebar()
        self.sidebar_surface = side
        side.setObjectName("sidebar")
        side.setFixedWidth(220)
        sidebar = QVBoxLayout(side)
        sidebar.setContentsMargins(16, 28, 8, 24)
        sidebar.setSpacing(8)
        brand = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(QIcon(str(ICON)).pixmap(48, 48))
        brand.addWidget(icon)
        brand.addWidget(label("虾米 POV", "section"))
        sidebar.addLayout(brand)
        sidebar.addSpacing(28)
        self.nav = []
        self.stack = QStackedWidget()
        names = ("首页", "Demo 与片段", "HUD 设置", "录制记录", "设置与恢复")
        for index, name in enumerate(names):
            nav = button(name, lambda checked=False, i=index: self.navigate(i))
            nav.setObjectName("nav")
            nav.setCheckable(True)
            nav.setIcon(outline_icon(("home", "film", "monitor", "clock", "settings")[index]))
            nav.setIconSize(QSize(21, 21))
            sidebar.addWidget(nav)
            self.nav.append(nav)
            content = QWidget()
            content.setObjectName("page")
            body = QVBoxLayout(content)
            body.setContentsMargins(32, 28, 32, 28)
            body.setSpacing(18)
            body.addWidget(label(name, "heading"))
            getattr(self, ("build_home", "build_demo", "build_hud", "build_records", "build_settings")[index])(body)
            body.addStretch()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(content)
            self.stack.addWidget(scroll)
        sidebar.addStretch()
        sidebar.addWidget(label("本地 Demo · NVIDIA 录制\n临时修改，校验恢复", "muted"))
        side.buttons = self.nav
        layout.addWidget(side)
        layout.addWidget(self.stack, 1)
        workspace.changed.connect(self.refresh)
        self.refresh()
        self.navigate(0)

    def navigate(self, index):
        self.stack.setCurrentIndex(index)
        self.sidebar_surface.select(index)
        for number, nav in enumerate(self.nav):
            nav.setChecked(number == index)
            nav.setIcon(outline_icon(("home", "film", "monitor", "clock", "settings")[number],
                                    "#ad513a" if number == index else "#805847"))

    def call(self, action):
        try:
            return action()
        except Exception as error:
            # Present errors without resets, fabricated success or lost backups.
            QMessageBox.warning(self, "操作未完成", str(error))
            return None

    def build_home(self, body):
        self.recovery_banner = label("", "warning")
        body.addWidget(self.recovery_banner)
        self.recovery_link = button("查看恢复问题", lambda: self.navigate(4))
        body.addWidget(self.recovery_link)
        hero = QVBoxLayout()
        hero.setSpacing(12)
        hero.addWidget(label("把精彩回合，录成你的 POV", "heroTitle"))
        hero.addWidget(label("导入 Demo，选择你的视角与片段，用 NVIDIA 留下精彩。", "subtitle"))
        row = QHBoxLayout()
        self.home_import = button("导入 Demo", self.pick_demo, True)
        self.home_import.setIcon(outline_icon("folder", "#ffffff"))
        self.home_import.setIconSize(QSize(20, 20))
        self.continue_draft = button("继续草稿", lambda: self.navigate(1))
        self.continue_draft.setIcon(outline_icon("clock"))
        row.addWidget(self.home_import)
        row.addWidget(self.continue_draft)
        row.addStretch()
        hero.addLayout(row)
        body.addLayout(hero)

        status = QFrame()
        status.setObjectName("statusBar")
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(20, 16, 20, 16)
        self.cs2_status = label("CS2 · 待检查", "muted")
        self.nvidia_status = label("NVIDIA · 待确认", "muted")
        self.restore_status = label("恢复 · 无待处理事务", "muted")
        for name, item in (("monitor", self.cs2_status), ("film", self.nvidia_status),
                           ("shield", self.restore_status)):
            image = QLabel()
            image.setPixmap(outline_icon(name).pixmap(20, 20))
            status_layout.addWidget(image)
            status_layout.addWidget(item, 1)
        body.addWidget(status)

        middle = QHBoxLayout()
        middle.setSpacing(20)
        current = card(middle, "当前任务")
        preview = QFrame()
        preview.setObjectName("emptyPreview")
        preview.setMinimumHeight(158)
        preview_body = QVBoxLayout(preview)
        preview_body.setContentsMargins(20, 20, 20, 20)
        preview_body.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setPixmap(outline_icon("film", "#b89a8a", 42).pixmap(42, 42))
        preview_body.addWidget(image)
        title = label("导入 Demo，开启你的 POV", "emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_body.addWidget(title)
        self.preview_title = title
        hint = label("选择玩家与片段，准备后录制。", "muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_body.addWidget(hint)
        current.addWidget(preview)
        self.draft_summary = label("", "muted")
        current.addWidget(self.draft_summary)
        self.current_task = label("")
        current.addWidget(self.current_task)
        self.cancel_button = button("取消当前任务", self.workspace.cancel)
        self.cancel_button.setObjectName("quiet")
        current.addWidget(self.cancel_button)
        current.addStretch()

        hud = card(middle, "画面与保存")
        badge = label("仿实战 HUD", "pill")
        hud.addWidget(badge)
        hud.addWidget(label("保留血量、弹药、准星与击杀提示。\n雷达可选；隐藏观战提示与回放控制条。", "muted"))
        edit = button("调整 HUD 预设", lambda: self.navigate(2))
        edit.setIcon(outline_icon("settings"))
        hud.addWidget(edit)
        self.home_video = label("", "muted")
        hud.addWidget(self.home_video)
        self.home_environment = label("", "muted")
        self.home_environment.hide()
        hud.addWidget(self.home_environment)
        self.cs2_status.setToolTip(self.workspace.environment)
        paths = button("配置路径与检查空间", lambda: self.navigate(4))
        paths.setObjectName("quiet")
        paths.setIcon(outline_icon("arrow", "#3c65a8"))
        hud.addWidget(paths)
        hud.addStretch()
        middle.setStretch(0, 3)
        middle.setStretch(1, 2)
        body.addLayout(middle)

        recent = card(body, "最近录制")
        self.recent_list = label("", "muted")
        recent.addWidget(self.recent_list)
        view = button("查看全部记录", lambda: self.navigate(3))
        view.setObjectName("quiet")
        recent.addWidget(view)

    def build_demo(self, body):
        selected = card(body, "本地 Demo")
        selected.setSpacing(8)
        selected.setContentsMargins(24, 16, 24, 16)
        self.demo_import = button("选择 .dem 文件", self.pick_demo, True)
        row = QHBoxLayout()
        self.parse_button = button("解析 / 重试", lambda: self.call(self.workspace.start_parse))
        self.remove_demo_button = button("移除引用", lambda: self.call(self.workspace.remove_demo))
        for action in (self.demo_import, self.parse_button, self.remove_demo_button):
            row.addWidget(action)
        row.addStretch()
        selected.addLayout(row)
        self.demo_summary = label("")
        # A long Windows path must not force the whole page past the viewport.
        self.demo_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        selected.addWidget(self.demo_summary)
        self.demo_split = ResponsiveSplit()
        body.addWidget(self.demo_split)
        clip = card(self.demo_split.columns, "片段剪辑")
        clip.addWidget(label("选定玩家后可选择回合、连续时间段或多杀片段。多杀默认前后各 10 秒，拖动边界调整。", "muted"))
        form = QFormLayout()
        self.player_choice, self.range_mode, self.round_choice = QComboBox(), QComboBox(), QComboBox()
        self.range_mode.addItem("多杀片段", "highlight")
        self.range_mode.addItem("一个回合", "round")
        self.range_mode.addItem("连续时间段", "time")
        self.highlight_choice = QComboBox()
        self.highlight_note = label("", "muted")
        self.highlight_duration = label("", "muted")
        self.timeline = ClipTimeline()
        self.range_start, self.range_end = SecondsSpinBox(), SecondsSpinBox()
        for spin in (self.range_start, self.range_end):
            spin.setRange(0, 100_000)
            spin.setDecimals(6)
            spin.setSuffix(" 秒")
        self.clip_form = form
        form.addRow("玩家（稳定 ID）", self.player_choice)
        self.mode_segments = QWidget()
        mode_layout = QHBoxLayout(self.mode_segments)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(6)
        self.mode_buttons = []
        for mode_index in range(self.range_mode.count()):
            mode_button = button(self.range_mode.itemText(mode_index), lambda checked=False, i=mode_index: self.range_mode.setCurrentIndex(i))
            mode_button.setObjectName("mode")
            mode_button.setCheckable(True)
            mode_layout.addWidget(mode_button)
            self.mode_buttons.append(mode_button)
        self.range_mode.setParent(self.mode_segments)
        self.range_mode.hide()
        form.addRow("片段方式", self.mode_segments)
        form.addRow("回合", self.round_choice)
        form.addRow("多杀候选", self.highlight_choice)
        form.addRow("开始位置", self.range_start)
        form.addRow("结束位置", self.range_end)
        clip.addLayout(form)
        clip.addWidget(self.highlight_note)
        clip.addWidget(self.timeline)
        clip.addWidget(self.highlight_duration)
        self.save_clip = button("保存片段草稿", self.save_clip_draft, True)
        clip.addWidget(self.save_clip)
        self.clip_summary = label("解析后可选择玩家与范围。", "muted")
        self.clip_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        clip.addWidget(self.clip_summary)
        self.clip_edit_status = label("", "warning")
        clip.addWidget(self.clip_edit_status)
        self.range_mode.currentIndexChanged.connect(self.update_range_controls)
        self.player_choice.currentIndexChanged.connect(self.refresh_highlights)
        self.highlight_choice.currentIndexChanged.connect(self.choose_highlight)
        self.timeline.rangeChanged.connect(self.timeline_changed)
        self.range_start.valueChanged.connect(lambda value: self.numeric_highlight(0, value))
        self.range_end.valueChanged.connect(lambda value: self.numeric_highlight(1, value))
        self.round_choice.currentIndexChanged.connect(self.update_range_controls)
        self.range_start.valueChanged.connect(self.update_prepare)
        self.range_end.valueChanged.connect(self.update_prepare)
        self.clip_controls = [self.player_choice, self.range_mode, self.round_choice,
                              self.range_start, self.range_end, self.save_clip, self.highlight_choice, self.timeline, *self.mode_buttons]
        self.prepare = button("开始录制" if self.workspace.automatic_recording else "准备并预览",
            lambda: self.call(self.workspace.start_capture if self.workspace.automatic_recording
                             else self.workspace.start_preview), True)
        self.prepare.setEnabled(False)
        self.prepare.setToolTip("需要完成 Demo 解析和回放控制后才能准备。")
        clip.addWidget(self.prepare)
        self.preview_status = label("", "muted")
        clip.addWidget(self.preview_status)
        self.preview_confirm = button("控制台已打开且输入框为空",lambda:self.call(self.confirm_preview_console))
        clip.addWidget(self.preview_confirm)
        self.preview_arm = button('准备播放（收起控制台）',lambda:self.call(self.workspace.prepare_preview_playback))
        clip.addWidget(self.preview_arm)
        self.preview_play = button('画面已检查，播放片段（不录制）',lambda:self.call(self.workspace.play_preview_clip),True)
        clip.addWidget(self.preview_play)
        self.recording_start = button('画面已检查，准备 NVIDIA 录制',lambda:self.call(self.workspace.start_recording),True)
        clip.addWidget(self.recording_start)
        self.recording_confirm = button('',lambda:self.call(self.confirm_recording_state),True)
        clip.addWidget(self.recording_confirm)
        self.preview_binding = button('核验播放按键恢复',lambda:self.call(self.workspace.check_preview_binding))
        clip.addWidget(self.preview_binding)
        self.preview_stop = button("退出并恢复",lambda:self.call(self.workspace.stop_preview))
        clip.addWidget(self.preview_stop)
        info = card(self.demo_split.columns, "画面与录制")
        info.addWidget(label("仿实战 HUD", "pill"))
        info.addWidget(label("保留血量、弹药、准星与击杀提示。\n雷达可选；隐藏观战提示与回放控制条。", "muted"))
        info.addWidget(button("调整 HUD", lambda: self.navigate(2)))
        info.addWidget(label("视频保存位置", "section"))
        self.demo_output_path = label("未设置", "muted")
        self.demo_output_path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        info.addWidget(self.demo_output_path)
        info.addWidget(button("设置与检查", lambda: self.navigate(4)))
        info.addWidget(label("保存片段草稿后再开始录制。运行状态与恢复提示会显示在片段操作区。", "muted"))
        info.addStretch()
        self.editor_widgets += [self.parse_button, self.remove_demo_button, *self.clip_controls]

    def confirm_preview_console(self):
        confirmation = getattr(self, "_console_confirmation", None)
        if confirmation is None:
            raise DataError("当前没有可确认的控制台步骤。")
        task_id, session_id, step_token = confirmation
        self.workspace.confirm_preview_console(session_id=session_id, step_token=step_token, task_id=task_id)

    def confirm_recording_state(self):
        confirmation = getattr(self, '_recording_confirmation', None)
        if confirmation is None:
            raise DataError('当前没有可确认的 NVIDIA 步骤。')
        task_id, state, step_token = confirmation
        self.workspace.confirm_recording_state(task_id=task_id, state=state, step_token=step_token)

    def reveal_preview_step(self, step):
        # A queued layout update must not scroll to a step that already ended.
        if step != getattr(self, "_displayed_preview_step", None) or self.stack.currentIndex() != 1:
            return
        preview = self.workspace._preview
        if preview is None:
            return
        task = self.workspace._recording_task
        if task is not None:
            target = (self.recording_confirm if self.recording_confirm.isVisible()
                      else self.preview_confirm if self.preview_confirm.isVisible() else self.preview_stop)
        else:
            target = {"awaiting_console": self.preview_confirm,
                  "awaiting_demo_console": self.preview_confirm,
                  "awaiting_play_console": self.preview_confirm,
                  "awaiting_end_console": self.preview_confirm,
                  "ready": self.preview_arm, "play_ready": self.preview_play,
                      "play_ended": self.preview_binding}.get(preview.state)
        if target is not None and target.isVisible():
            self.stack.widget(1).ensureWidgetVisible(target, 0, 24)

    def refresh_highlights(self, _=None):
        blocked = self.highlight_choice.blockSignals(True)
        self.highlight_choice.clear()
        analysis = self.workspace.analysis
        self._highlights = candidates(analysis, self.player_choice.currentData()) if analysis else []
        for item in self._highlights:
            suffix = "" if item["available"] else " · 不可录"
            self.highlight_choice.addItem(f"第 {item['number']} 回合 · {len(item['kill_ticks'])} 杀{suffix}", item["id"])
        self.highlight_choice.blockSignals(blocked)
        self.choose_highlight()

    def choose_highlight(self, _=None):
        item = next((c for c in getattr(self, "_highlights", []) if c["id"] == self.highlight_choice.currentData()), None)
        self.timeline.clear()
        self.highlight_duration.clear()
        analysis = self.workspace.analysis
        issues = [i for i in (analysis or {}).get("kill_issues", []) if i["player"] == self.player_choice.currentData()]
        warning = f"部分击杀无法确认（{len(issues)} 条），候选可能不完整。\n" if issues else ""
        text = "当前玩家没有可确认的同回合多杀。"
        if item:
            text = item["reason"] if not item["available"] else "；".join(item["reasons"])
            if item["available"]:
                ticks = [p[0] for p in analysis["timeline"] if item["minimum"] <= p[0] <= item["maximum"]]
                self.timeline.configure(ticks, item["kill_ticks"], item["start_tick"], item["end_tick"],
                                        rate=analysis["tick_rate"], origin=analysis["timeline"][0][0])
                origin, rate = analysis["timeline"][0][0], analysis["tick_rate"]
                text = "击杀时间：" + " / ".join(f"{(t-origin)/rate:.0f}s" for t in item["kill_ticks"]) + "\n" + text
                if self.range_mode.currentData() == "highlight":
                    self.timeline_changed(item["start_tick"], item["end_tick"])
        self.highlight_note.setText(warning + text)
        self.update_range_controls()

    def timeline_changed(self, start, end):
        self.timeline.refresh_labels()
        self.timeline.update()
        analysis = self.workspace.analysis
        if not analysis:
            return
        origin, rate = analysis["timeline"][0][0], analysis["tick_rate"]
        for spin, tick in ((self.range_start, start), (self.range_end, end)):
            spin.blockSignals(True)
            spin.setValue((tick-origin)/rate)
            spin.blockSignals(False)
        if self.timeline.kills:
            self.highlight_duration.setText(f"第一杀前 {(self.timeline.kills[0]-start)/rate:.0f} 秒 · 最后一杀后 {(end-self.timeline.kills[-1])/rate:.0f} 秒 · 总长 {(end-start)/rate:.0f} 秒")
        self.update_prepare()

    def update_prepare(self, _=None):
        if not hasattr(self, "prepare"):
            return
        w = self.workspace
        if hasattr(self, "demo_output_path"):
            self.demo_output_path.setText(w.settings.video_directory or "未设置视频目录")
        saved = (w.library.draft() or {}).get('selection')
        same = bool(saved) and self.player_choice.currentData() == saved['player_id'] and self.range_mode.currentData() == saved['mode']
        if same and saved['mode'] == 'round':
            same = self.round_choice.currentData() == saved['round_id']
        elif same and saved['mode'] == 'time':
            same = all(spin.value() == round(saved[field], spin.decimals())
                       for spin, field in ((self.range_start, 'start_seconds'), (self.range_end, 'end_seconds')))
        elif same and saved['mode'] == 'highlight':
            same = (self.highlight_choice.currentData() == saved['candidate_id']
                    and bool(self.timeline.ticks)
                    and (self.timeline.start, self.timeline.end) == (saved['start_tick'], saved['end_tick']))
        editable = w.analysis is not None and not w.busy and not w.recovery and not w.nvidia_recovery_items
        dirty = bool(saved) and w.analysis is not None and not same
        self.prepare.setEnabled(editable and bool(saved) and same)
        self.clip_edit_status.setText('有未保存的玩家或片段调整，请先保存片段草稿再准备预览。' if dirty else '')
        self.clip_edit_status.setVisible(dirty)
        self.prepare.setToolTip('先保存当前调整，避免预览旧片段。' if dirty else '启动受管理的本地回放，核验起点与玩家视角；不启动录制。')

    def numeric_highlight(self, handle, value):
        if self.range_mode.currentData() == "highlight" and self.timeline.ticks:
            self.timeline.active = handle
            analysis = self.workspace.analysis
            self.timeline.move_handle(analysis["timeline"][0][0] + value*analysis["tick_rate"])

    def update_range_controls(self, _=None):
        enabled = self.workspace.analysis is not None and not self.workspace.busy and not self.workspace.recovery
        mode = self.range_mode.currentData()
        highlight = mode == "highlight"
        for i, mode_button in enumerate(self.mode_buttons):
            mode_button.setChecked(self.range_mode.currentIndex() == i)
        self.round_choice.setEnabled(enabled and mode == "round")
        self.clip_form.setRowVisible(self.round_choice, mode == "round")
        self.clip_form.setRowVisible(self.highlight_choice, highlight)
        self.highlight_note.setVisible(highlight)
        self.timeline.setVisible(highlight)
        self.highlight_duration.setVisible(False)
        self.highlight_choice.setEnabled(enabled and highlight)
        valid = enabled and (not highlight or bool(self.timeline.ticks))
        if mode == 'round':
            valid = valid and any(item['id'] == self.round_choice.currentData() and item['complete']
                                  for item in (self.workspace.analysis or {}).get('rounds', []))
        self.range_start.setEnabled(valid and mode != "round")
        self.range_end.setEnabled(valid and mode != "round")
        self.save_clip.setEnabled(valid)
        self.timeline.setEnabled(enabled and highlight)
        if highlight and self.timeline.ticks:
            self.timeline_changed(self.timeline.start, self.timeline.end)
        self.update_prepare()

    def save_clip_draft(self):
        options = dict(round_id=self.round_choice.currentData(),
                       start_seconds=self.range_start.value(), end_seconds=self.range_end.value())
        if self.range_mode.currentData() == "highlight":
            options.update(candidate_id=self.highlight_choice.currentData(),
                           start_tick=self.timeline.start, end_tick=self.timeline.end)
        self.call(lambda: self.workspace.save_selection(self.player_choice.currentData(),
                                                       self.range_mode.currentData(), **options))

    def restore_clip_controls(self, saved):
        """Restore one saved selection without firing intermediate edit handlers."""
        widgets = (self.player_choice, self.range_mode, self.round_choice,
                   self.highlight_choice, self.range_start, self.range_end)
        blocked = [widget.blockSignals(True) for widget in widgets]
        try:
            analysis = self.workspace.analysis
            self.player_choice.clear()
            self.round_choice.clear()
            self.range_mode.setCurrentIndex(self.range_mode.findData("highlight"))
            if analysis:
                for player in analysis["players"]:
                    self.player_choice.addItem(f"{player['name']} · {player['id']}", player["id"])
                for round in analysis["rounds"]:
                    self.round_choice.addItem(f"第 {round['number']} 回合" + (" · 边界不完整" if not round["complete"] else ""), round["id"])
                duration = (analysis["timeline"][-1][0] - analysis["timeline"][0][0]) / analysis["tick_rate"]
                self.range_start.setMaximum(duration)
                self.range_end.setMaximum(duration)
                if saved:
                    self.player_choice.setCurrentIndex(self.player_choice.findData(saved["player_id"]))
                    self.range_mode.setCurrentIndex(self.range_mode.findData(saved["mode"]))
                    self.round_choice.setCurrentIndex(self.round_choice.findData(saved.get("round_id")))
            self.refresh_highlights()
            if analysis and saved:
                if saved.get("mode") == "highlight" and saved.get("content_sha256") == (self.workspace.library.draft().get("fingerprint") or {}).get("sha256"):
                    index = self.highlight_choice.findData(saved.get("candidate_id"))
                    self.highlight_choice.setCurrentIndex(index)
                    self.choose_highlight()
                    if index >= 0 and self.timeline.ticks:
                        from cs2pov.services.highlights import select_highlight
                        try:
                            select_highlight(analysis, saved["player_id"], saved["candidate_id"], saved["start_tick"], saved["end_tick"])
                        except DataError:
                            self.highlight_note.setText("旧片段不再符合当前分析，请重新选择范围。")
                        else:
                            self.timeline.start, self.timeline.end = saved["start_tick"], saved["end_tick"]
                            self.timeline_changed(self.timeline.start, self.timeline.end)
                # Candidate rebuilding may set its defaults. Restore ordinary
                # time fields last, after the saved mode has been selected.
                if saved.get("mode") != "highlight":
                    self.range_start.setValue(saved["start_seconds"])
                    self.range_end.setValue(saved["end_seconds"])
            elif not saved and self.range_mode.currentData() != "highlight":
                self.range_start.setValue(0)
                self.range_end.setValue(0)
        finally:
            for widget, previous in zip(widgets, blocked):
                widget.blockSignals(previous)

    def pick_demo(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择本地 Demo", "", "CS2 Demo (*.dem)")
        if path:
            if self.call(lambda: self.workspace.import_demo(Path(path))) is None:
                self.navigate(1)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if (len(urls) == 1 and urls[0].isLocalFile()
                and Path(urls[0].toLocalFile()).suffix.casefold() == ".dem"
                and not self.workspace.busy and not self.workspace.recovery):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            event.ignore()
            return
        try:
            self.workspace.import_demo(Path(urls[0].toLocalFile()))
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "导入未完成", str(error))
            event.ignore()
            return
        self.navigate(1)
        event.acceptProposedAction()

    def build_hud(self, body):
        controls = card(body, "仿实战 HUD")
        controls.addWidget(label("保留血量、护甲、弹药、准星、比分和击杀提示。雷达可选；隐藏观战提示、回放控制条及 X 光。", "muted"))
        controls.addWidget(label("参数先保存在本地；生效和兼容性将在实际回放预览中验证。", "muted"))
        self.preset_choice = QComboBox()
        self.preset_choice.currentIndexChanged.connect(self.load_preset)
        controls.addWidget(self.preset_choice)
        form = QFormLayout()
        self.preset_name = QLineEdit()
        self.crosshair = QLineEdit()
        self.crosshair.setPlaceholderText("CSGO 分享码；留空使用当前游戏准星")
        form.addRow("预设名称", self.preset_name)
        form.addRow("准星分享码", self.crosshair)
        self.hud_fields = {}
        for key, text, low, high, step in (("hud_scale", "HUD 比例", .5, 1, .05),
                ("viewmodel_fov", "持枪视野", 54, 68, 1), ("viewmodel_x", "持枪横向", -2, 2.5, .1),
                ("viewmodel_y", "持枪前后", -2, 2, .1), ("viewmodel_z", "持枪高度", -2, 2, .1)):
            spin = QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setSingleStep(step)
            spin.setDecimals(2)
            self.hud_fields[key] = spin
            form.addRow(text, spin)
        controls.addLayout(form)
        self.radar_option = QCheckBox("显示官方方形雷达")
        controls.addWidget(self.radar_option)
        controls.addWidget(label("使用 CS2 原生 Demo 雷达，可能显示双方位置。默认隐藏；退出后恢复原游戏配置。修改后重新保存片段草稿才会应用。", "muted"))
        actions = QHBoxLayout()
        self.hud_save = button("保存预设", self.save_preset, True)
        self.hud_copy = button("复制", lambda: self.preset_action("copy"))
        self.hud_default = button("设为默认", lambda: self.preset_action("set_default"))
        self.hud_delete = button("删除自建预设", lambda: self.preset_action("remove"))
        self.hud_reset = button("恢复默认值", self.reset_preset)
        for action in (self.hud_save, self.hud_copy, self.hud_default, self.hud_delete, self.hud_reset):
            actions.addWidget(action)
        controls.addLayout(actions)
        self.editor_widgets += [self.preset_choice, self.preset_name, self.crosshair, self.radar_option, *self.hud_fields.values(),
                                self.hud_save, self.hud_copy, self.hud_default, self.hud_delete, self.hud_reset]

    def load_preset(self, _=None):
        id = self.preset_choice.currentData()
        item = next((p for p in self.workspace.presets.items if p.id == id), None)
        if item:
            self.preset_name.setText(item.name)
            self.crosshair.setText(item.crosshair)
            self.radar_option.setChecked(item.show_radar)
            for key, spin in self.hud_fields.items():
                spin.setValue(getattr(item, key))
            self.hud_delete.setEnabled(id != "builtin" and not self.workspace.busy)

    def save_preset(self):
        id = self.preset_choice.currentData()
        self.call(lambda: self.workspace.save_preset(HudPreset(id=id, name=self.preset_name.text(),
                    crosshair=self.crosshair.text(), show_radar=self.radar_option.isChecked(),
                    **{key: spin.value() for key, spin in self.hud_fields.items()})))

    def preset_action(self, operation):
        result = self.call(lambda: self.workspace.preset_action(operation, self.preset_choice.currentData()))
        if isinstance(result, HudPreset):
            self.preset_choice.setCurrentIndex(self.preset_choice.findData(result.id))

    def reset_preset(self):
        default = HudPreset()
        self.crosshair.setText(default.crosshair)
        self.radar_option.setChecked(default.show_radar)
        for key, spin in self.hud_fields.items():
            spin.setValue(getattr(default, key))
        self.save_preset()

    def build_records(self, body):
        if not self.workspace.automatic_recording:
            self._build_diagnostic_output_review(body)
        else:
            body.addWidget(button('打开视频目录', lambda: self.call(
                lambda: self.open_path(self.workspace.settings.video_directory))))
        content = card(body, "本地录制记录")
        self.records_list = QListWidget()
        self.records_list.setMinimumHeight(230)
        self.records_list.currentItemChanged.connect(lambda *_: self.show_record_details())
        content.addWidget(self.records_list)
        self.record_details = label("选择一条记录查看视频路径、元数据和恢复结果。", "muted")
        self.record_details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content.addWidget(self.record_details)
        self.record_open_video = button("打开视频", self.open_selected_record_video)
        self.record_open_folder = button("打开所在目录", self.open_selected_record_folder)
        self.record_reprepare = button("重新准备", self.reprepare_selected_record)
        self.record_remove = button("移除记录（不删视频）", self.remove_selected_record)
        for group in ((self.record_open_video, self.record_open_folder),
                      (self.record_reprepare, self.record_remove)):
            actions = QHBoxLayout()
            for action in group:
                actions.addWidget(action)
            actions.addStretch()
            content.addLayout(actions)
        content.addWidget(label("视频结果与游戏恢复结果分别显示；移除记录不会删除视频文件。试验录像不会自动伪装成应用录制记录。", "muted"))

    def build_settings(self, body):
        paths = card(body, "路径与录制")
        form = QFormLayout()
        self.path_edits = {}
        self.path_buttons = []
        for key, text in (("installation", "CS2 安装目录"), ("cfg", "游戏用户配置目录"),
                          ("video_directory", "视频保存目录")):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit()
            choose = button("选择…", lambda checked=False, k=key: self.pick_directory(k))
            row_layout.addWidget(edit, 1)
            row_layout.addWidget(choose)
            self.path_edits[key] = edit
            self.path_buttons.append(choose)
            form.addRow(text, row)
        self.hotkey = QLineEdit()
        form.addRow("NVIDIA 录制热键", self.hotkey)
        paths.addLayout(form)
        self.nvidia_confirm = QCheckBox("我已在 NVIDIA App 中将保存目录设为上面的目录")
        paths.addWidget(self.nvidia_confirm)
        paths.addWidget(label("在 NVIDIA 录制设置中选择同一保存目录，再勾选确认。目录选择窗口可创建文件夹。", "muted"))
        paths.addWidget(label("最低空间为 10GB；高画质或长片段需要更多空间。应用不会更改 NVIDIA 全局画质和音频设置。", "muted"))
        actions = QHBoxLayout()
        self.settings_save = button("保存设置", self.save_settings, True)
        self.space_refresh = button("检查环境与空间", lambda: self.call(self.workspace.check_environment))
        self.open_video_dir = button("打开视频目录", lambda: self.open_path(self.workspace.settings.video_directory))
        for action in (self.settings_save, self.space_refresh, self.open_video_dir):
            actions.addWidget(action)
        paths.addLayout(actions)
        self.settings_status = label("")
        paths.addWidget(self.settings_status)
        self.editor_widgets += [*self.path_edits.values(), *self.path_buttons, self.hotkey,
                                self.nvidia_confirm, self.settings_save]
        resources = card(body, '本地录制资源')
        resources.addWidget(label('便携版已附带 HUD，启动无需联网。通常无需更换；仅用于本地 Demo，录制结束后自动恢复游戏文件。', 'muted'))
        resource_form = QFormLayout()
        self.local_resource_buttons = {}
        self.local_resource_labels = {}
        roles = [('hud', 'HUD VPK', '选择 HUD VPK…')]
        if not self.workspace.automatic_recording:
            roles.append(('probe', '视频检查工具', '选择 ffprobe.exe…'))
        for role, title, action_text in roles:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            path = label('未设置', 'muted')
            choose = button(action_text, lambda checked=False, current=role: self.pick_local_resource(current))
            row_layout.addWidget(path, 1)
            row_layout.addWidget(choose)
            resource_form.addRow(title, row)
            self.local_resource_buttons[role] = choose
            self.local_resource_labels[role] = path
            self.editor_widgets.append(choose)
        resources.addLayout(resource_form)
        self.local_resource_status = label('', 'muted')
        resources.addWidget(self.local_resource_status)
        restore = card(body, "恢复与本地数据")
        restore.addWidget(label(str(self.workspace.directory), "muted"))
        self.recovery_list = QListWidget()
        restore.addWidget(self.recovery_list)
        self.recovery_details = label("选择会话查看修改与恢复状态。", "muted")
        restore.addWidget(self.recovery_details)
        self.recovery_list.currentItemChanged.connect(self.show_recovery_details)
        row = QHBoxLayout()
        self.recovery_refresh = button("重新检查", self.workspace.refresh_recovery)
        self.restore_button = button("恢复选中会话", self.restore_selected)
        row.addWidget(self.recovery_refresh)
        row.addWidget(self.restore_button)
        row.addWidget(button("打开备份", lambda: self.open_path(str(self.workspace.directory / "sessions"))))
        row.addWidget(button("打开日志", lambda: self.open_path(str(self.workspace.directory / "logs"))))
        restore.addLayout(row)
        restore.addWidget(label("恢复前需要关闭所有 CS2。冲突或备份损坏会保留文件并阻断新任务；重置设置不会删除备份和事务。", "muted"))
        self.reset_settings_button = button("重置应用设置", self.reset_settings)
        restore.addWidget(self.reset_settings_button)
        self.editor_widgets.append(self.reset_settings_button)
        nvidia = card(body, 'NVIDIA 录制状态检查')
        nvidia.addWidget(label('游戏文件恢复与 NVIDIA 停止状态分别检查。上次录制状态未知时，先打开 NVIDIA 覆盖层，检查并停止录制，再确认下面对应的任务。', 'muted'))
        self.nvidia_recovery_list = QListWidget()
        nvidia.addWidget(self.nvidia_recovery_list)
        self.nvidia_recovery_list.currentItemChanged.connect(self.show_nvidia_recovery)
        self.nvidia_recovery_details = label('', 'warning')
        nvidia.addWidget(self.nvidia_recovery_details)
        self.nvidia_recovery_resolve = button('我已检查 NVIDIA，确认现在未录制', self.resolve_nvidia_recovery)
        nvidia.addWidget(self.nvidia_recovery_resolve)

    def show_nvidia_recovery(self, item=None, _previous=None):
        current = self.nvidia_recovery_list.currentItem()
        target = current.data(Qt.ItemDataRole.UserRole) if current else None
        valid = target is not None and target in self.workspace.nvidia_recovery_items
        self.nvidia_recovery_details.setText(target.reason if valid else '没有需要人工检查的 NVIDIA 遗留任务。')
        self.nvidia_recovery_resolve.setEnabled(bool(valid and target.digest) and not self.workspace.busy)

    def resolve_nvidia_recovery(self):
        item = self.nvidia_recovery_list.currentItem()
        target = item.data(Qt.ItemDataRole.UserRole) if item else None
        if target is None:
            raise DataError('请先选择需要检查的 NVIDIA 任务。')
        self.call(lambda: self.workspace.resolve_recording_recovery(target,
                  task_id=target.task_id, checkpoint_digest=target.digest))

    def pick_directory(self, key):
        value = QFileDialog.getExistingDirectory(self, "选择本机文件夹（可新建文件夹）", self.path_edits[key].text())
        if value:
            self.path_edits[key].setText(value)
            if key == "video_directory":
                self.nvidia_confirm.setChecked(False)

    def _build_diagnostic_output_review(self, body):
        output = card(body, '本次录制的视频检查')
        self.output_status = label('', 'muted')
        self.output_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        output.addWidget(self.output_status)
        self.output_candidates = QComboBox()
        output.addWidget(self.output_candidates)
        actions = QHBoxLayout()
        self.output_choose = button('检查所选视频', lambda: self.call(self.choose_output_candidate))
        self.output_retry = button('重新查找本次视频', lambda: self.call(self.workspace.discover_recording_output))
        self.output_cancel = button('取消视频检查', lambda: self.call(self.workspace.cancel_output))
        for action in (self.output_choose, self.output_retry, self.output_cancel): actions.addWidget(action)
        output.addLayout(actions)
        self.content_box = QWidget()
        content_body = QVBoxLayout(self.content_box)
        content_body.setContentsMargins(0, 0, 0, 0)
        content_body.addWidget(label('元数据不能证明画面内容。首次试录请播放成片，逐项核对；片头和片尾各最多 2 秒。', 'muted'))
        self.content_open = button('打开本次成片', lambda: self.call(self.open_current_output))
        content_body.addWidget(self.content_open)
        self.content_checks = {}
        for name, text in (('target_view_confirmed', '全程是所选玩家的第一人称视角'),
                           ('hud_confirmed', '血量、弹药、准星和击杀提示符合预设'),
                           ('audio_confirmed', '视频实际能听到正常游戏声音'),
                           ('range_confirmed', '所选区间完整，结束前没有切到其他玩家'),
                           ('clean_picture_confirmed', '没有控制台、回放控制条或跳转画面')):
            check = QCheckBox(text)
            check.toggled.connect(self.update_content_confirmation)
            self.content_checks[name] = check
            content_body.addWidget(check)
        form = QFormLayout()
        self.content_head, self.content_tail = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.content_head, self.content_tail):
            spin.setRange(-1, 60); spin.setDecimals(3); spin.setSuffix(' 秒')
            spin.setSpecialValueText('尚未检查'); spin.setValue(-1)
            spin.valueChanged.connect(self.update_content_confirmation)
        form.addRow('实测片头余量', self.content_head)
        form.addRow('实测片尾余量', self.content_tail)
        content_body.addLayout(form)
        self.content_confirm = button('成片检查全部通过', lambda: self.call(self.confirm_output_content), True)
        content_body.addWidget(self.content_confirm)
        output.addWidget(self.content_box)

    def pick_local_resource(self, role):
        def select():
            if role not in self.local_resource_buttons:
                raise DataError('请选择 HUD 或视频检查工具。')
            if self.workspace.busy:
                raise DataError('任务执行中，不能更改本地录制资源。')
            current = self.workspace.local_resource_path(role)
            title, file_filter = (('选择本机已审查 HUD VPK', 'HUD VPK (*.vpk)') if role == 'hud' else
                                  ('选择本机已审查 ffprobe.exe', 'ffprobe.exe (ffprobe.exe)'))
            value, _ = QFileDialog.getOpenFileName(self, title, str(current) if current else '', file_filter)
            if not value:
                return
            # Native file dialogs process events. A task may have started
            # while this dialog was open; that late selection cannot mutate it.
            if self.workspace.busy:
                raise DataError('任务已经开始，不能提交本地资源选择。')
            self.workspace.register_local_resource(role, Path(value))
            self.refresh_local_resources()
        return self.call(select)

    def refresh_local_resources(self):
        missing = []
        for role, item in self.local_resource_labels.items():
            try:
                path = self.workspace.local_resource_path(role)
                text = str(path) if path else '未设置'
            except DataError as error:
                path, text = None, '未设置 · ' + str(error)
            item.setText(text)
            item.setToolTip(text if path else '')
            if path is None:
                missing.append(role)
        messages = []
        if 'hud' in missing:
            messages.append('未设置 HUD 资源，暂时无法准备预览；请从本机选择已审查的 VPK。')
        if 'probe' in missing:
            messages.append('未设置可选 ffprobe：可以录制，暂时不能自动检查成品视频；需要时选择本机已审查版本。')
        self.local_resource_status.setText('\n'.join(messages) if messages else
            '资源路径已设置；HUD 与当前游戏的兼容性仍需试录检查。')

    def save_settings(self):
        self.call(lambda: self.workspace.save_settings(replace(self.workspace.settings,
            **{key: edit.text().strip() for key, edit in self.path_edits.items()},
            hotkey=self.hotkey.text().strip(),
            nvidia_path_confirmed=self.nvidia_confirm.isChecked())))

    def reset_settings(self):
        self.call(lambda: self.workspace.save_settings(Settings()))

    def restore_selected(self):
        item = self.recovery_list.currentItem()
        target = item.data(Qt.ItemDataRole.UserRole) if item else None
        if target is not None and target in self.workspace.recovery_items and target.directory is not None:
            self.call(lambda: self.workspace.restore_session(target.directory))

    def show_recovery_details(self, item, _previous=None):
        target = item.data(Qt.ItemDataRole.UserRole) if item else None
        valid = target is not None and target in self.workspace.recovery_items and target.directory is not None
        self.restore_button.setEnabled(bool(valid) and not self.workspace.busy)
        try:
            text = self.workspace.recovery_details(target.directory) if valid else "选择会话查看修改与恢复状态。"
        except (OSError, ValueError, RuntimeError) as error:
            text = str(error)
        self.recovery_details.setText(text)

    def open_path(self, value):
        path = Path(value) if value else None
        if path is None or not path.exists():
            QMessageBox.warning(self, "位置不可用", "文件或目录不存在，请检查设置。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "无法打开", "系统没有成功打开该位置。")

    def refresh(self):
        w = self.workspace
        if hasattr(self, "demo_output_path"):
            self.demo_output_path.setText(w.settings.video_directory or "未设置视频目录")
        for widget in self.editor_widgets:
            widget.setEnabled(not w.busy)
        if getattr(self, "_displayed_settings", None) != w.settings:
            for key, edit in self.path_edits.items():
                edit.setText(getattr(w.settings, key))
            self.hotkey.setText(w.settings.hotkey)
            self.nvidia_confirm.setChecked(w.settings.nvidia_path_confirmed)
            self._displayed_settings = w.settings
        self.refresh_local_resources()
        selected = self.preset_choice.currentData() or w.presets.default
        self.preset_choice.blockSignals(True)
        self.preset_choice.clear()
        for preset in w.presets.items:
            suffix = " · 默认" if preset.id == w.presets.default else ""
            self.preset_choice.addItem(preset.name + suffix, preset.id)
        index = self.preset_choice.findData(selected)
        self.preset_choice.setCurrentIndex(max(0, index))
        self.preset_choice.blockSignals(False)
        preset_state = (tuple(w.presets.items), w.presets.default)
        if getattr(self, "_displayed_presets", None) != preset_state:
            self.load_preset()
            self._displayed_presets = preset_state
        self.recovery_banner.setVisible(bool(w.recovery or w.nvidia_recovery_items or w.warnings))
        self.recovery_banner.setText("\n".join([*( ["存在恢复问题，不能准备新任务。"] if w.recovery else []),
            *(['NVIDIA 上次录制状态未知，先检查并停止录制。'] if w.nvidia_recovery_items else []), *w.warnings]))
        self.recovery_link.setVisible(bool(w.recovery or w.nvidia_recovery_items))
        selected_nv = self.nvidia_recovery_list.currentItem()
        selected_nv = selected_nv.data(Qt.ItemDataRole.UserRole) if selected_nv else None
        self.nvidia_recovery_list.blockSignals(True)
        self.nvidia_recovery_list.clear()
        for target in w.nvidia_recovery_items:
            item = QListWidgetItem((target.session_id or '未核验会话目录') + ' · ' + target.state)
            item.setData(Qt.ItemDataRole.UserRole, target)
            self.nvidia_recovery_list.addItem(item)
        if not w.nvidia_recovery_items:
            self.nvidia_recovery_list.addItem('没有 NVIDIA 状态未知的遗留任务')
        selected_row_nv = next((row for row, target in enumerate(w.nvidia_recovery_items)
                                if target == selected_nv), 0)
        self.nvidia_recovery_list.setCurrentRow(selected_row_nv)
        self.nvidia_recovery_list.blockSignals(False)
        self.show_nvidia_recovery()
        current = self.recovery_list.currentItem()
        selected_recovery = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.recovery_list.blockSignals(True)
        self.recovery_list.clear()
        for target in w.recovery_items:
            item = QListWidgetItem(target.label)
            item.setData(Qt.ItemDataRole.UserRole, target)
            self.recovery_list.addItem(item)
        if not w.recovery_items:
            self.recovery_list.addItem("没有待恢复事务")
        selected_row = next((row for row, target in enumerate(w.recovery_items)
                             if target == selected_recovery), 0)
        self.recovery_list.setCurrentRow(selected_row)
        self.recovery_list.blockSignals(False)
        self.show_recovery_details(self.recovery_list.currentItem())
        self.home_import.setEnabled(not w.busy and not w.recovery)
        self.demo_import.setEnabled(not w.busy and not w.recovery)
        draft = w.library.draft()
        status_names = {"awaiting_parse": "待解析", "parsing": "解析中", "parsed": "已解析",
                        "cancelled": "已取消，可重试", "failed": "解析失败，可重试"}
        summary = (f"{draft['demo']}\n文件已引用 · {status_names.get(draft.get('status'), '请重新解析')}"
                   if draft else "还没有导入 Demo。从这里开始录制你的一个片段。")
        if draft and draft.get("error"):
            summary += "\n" + draft["error"]
        self.draft_summary.setText(summary)
        self.preview_title.setText((draft.get("map", "比赛文件已选") + " · " + status_names.get(draft.get("status"), "待解析"))
                                  if draft else "导入 Demo，开启你的 POV")
        self.demo_summary.setText(summary)
        self.parse_button.setEnabled(bool(draft) and not w.busy and not w.recovery)
        self.remove_demo_button.setEnabled(bool(draft) and not w.busy)
        saved = (draft or {}).get("selection") or {}
        if (getattr(self, "_displayed_analysis", None) is not w.analysis
                or getattr(self, "_displayed_selection", {}) != saved):
            self.restore_clip_controls(saved)
            self._displayed_analysis = w.analysis
            self._displayed_selection = saved
        for control in self.clip_controls:
            control.setEnabled(w.analysis is not None and not w.busy and not w.recovery)
        self.update_range_controls()
        saved = (draft or {}).get("selection")
        self.update_prepare()
        self.preview_status.setText(w.preview_status)
        preview = w._preview
        task = w._recording_task
        console_states = ('awaiting_console','awaiting_demo_console','awaiting_play_console','awaiting_end_console')
        self._console_confirmation = ((task.task_id if task else None, preview.session.name, getattr(preview, 'console_step_token', None))
            if preview is not None and preview.state in console_states and not preview.console_confirmed else None)
        self.preview_confirm.setEnabled(preview is not None and preview.state in console_states and not preview.console_confirmed)
        self.preview_confirm.setVisible(preview is not None and preview.state in console_states)
        for widget,state in ((self.preview_arm,'ready'),(self.preview_play,'play_ready'),(self.preview_binding,'play_ended')):
            widget.setEnabled(task is None and preview is not None and preview.state==state)
            widget.setVisible(task is None and preview is not None and preview.state==state)
        recording_ready = task is None and preview is not None and preview.state == 'play_ready'
        self.recording_start.setVisible(recording_ready)
        self.recording_start.setEnabled(recording_ready and w.settings.nvidia_path_confirmed and not w.nvidia_recovery_items)
        self.recording_start.setToolTip('请先在设置中确认 NVIDIA 保存目录。' if not w.settings.nvidia_path_confirmed else '按设置中的快捷键自动开始和停止录制。')
        confirmation_text = {'awaiting_not_recording': 'NVIDIA 当前未录制，开始本次录制',
            'awaiting_started': 'NVIDIA 已开始录制，播放所选片段',
            'awaiting_stopped': 'NVIDIA 已停止／已保存，检查结果'}
        self._recording_confirmation = ((task.task_id, task.state, task.confirmation_token)
            if task and task.state in confirmation_text and task.confirmation_token
                and w._recording_automation is None else None)
        self.recording_confirm.setText(confirmation_text.get(task.state if task else '', ''))
        self.recording_confirm.setVisible(self._recording_confirmation is not None)
        self.recording_confirm.setEnabled(self._recording_confirmation is not None)
        cleanup_pending = (preview is not None and preview.state == 'recovery_blocked'
                           and getattr(preview,'pipe_cleanup_pending',False) is True)
        self.preview_stop.setText('重试安全收尾' if cleanup_pending else '取消录制并安全收尾' if task else '退出并恢复')
        self.preview_stop.setEnabled(preview is not None and (cleanup_pending or preview.state not in ('closing','complete','recovery_blocked')))
        self.preview_stop.setVisible(preview is not None)
        step = (None if preview is None else (preview.session.name, preview.state, getattr(preview, 'console_step_token', None))) if task is None else (task.task_id,task.state,task.confirmation_token,getattr(preview,'console_step_token',None))
        if step != getattr(self, '_displayed_preview_step', None):
            self._displayed_preview_step = step
            QTimer.singleShot(0, lambda current=step: self.reveal_preview_step(current))
        self.clip_summary.setText((f"{saved['player']} · {saved['map']} · {saved['start_seconds']:.0f}–{saved['end_seconds']:.0f} 秒"
            f"\n预计 {saved['duration']:.0f} 秒"
            + (" · 官方方形雷达" if saved.get('hud', {}).get('show_radar', False) else " · 雷达隐藏")
            + ("\n" + "；".join(saved["reasons"]) if saved["reasons"] else "")) if saved else "解析后选择玩家与范围，保存时会锁定当前默认 HUD 参数。")
        self.continue_draft.setEnabled(bool(draft) and not w.busy and not w.recovery)
        self.current_task.setText(w.task_text)
        self.cancel_button.setEnabled(w.busy)
        self.home_environment.setText(w.environment)
        self.cs2_status.setText("CS2 · " + ("检查未通过" if "未通过" in w.environment else
                               "已检查" if " · " in w.environment else "待检查"))
        self.cs2_status.setToolTip(w.environment)
        self.nvidia_status.setText("NVIDIA · " + ('状态需处理' if w.nvidia_recovery_items else "目录已确认" if w.settings.nvidia_path_confirmed else "待确认"))
        self.restore_status.setText("恢复 · " + ("需处理" if w.recovery else "无待处理事务"))
        path = w.settings.video_directory or "未选择"
        space = (f"可用 {w.disk.available_bytes / 1_000_000_000:.2f} GB · 满足最低空间要求" if w.disk else w.disk_error)
        verified = ("NVIDIA 保存目录已确认" if w.settings.nvidia_path_confirmed else "请确认 NVIDIA 保存目录一致") if w.automatic_recording else ("试录路径已验证" if w.settings.output_verified else "试录路径未验证")
        text = f"视频目录：{path}\n{space}\n{verified}"
        self.home_video.setText(text)
        self.settings_status.setText(text)
        selected_record = self.records_list.currentItem()
        selected_record_id = selected_record.data(Qt.ItemDataRole.UserRole) if selected_record else None
        self.records_list.blockSignals(True)
        self.records_list.clear()
        records = w.library.records()
        self.recent_list.setText("暂无录制记录" if not records else "\n".join(self.record_text(row) for row in records[:5]))
        for row in records:
            item = QListWidgetItem(self.record_text(row))
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.records_list.addItem(item)
        if not records:
            self.records_list.addItem("暂无录制记录。完成应用录制后，结果会显示在这里。")
        self.records_list.blockSignals(False)
        if records:
            selected_index = next((index for index, row in enumerate(records) if row["id"] == selected_record_id), 0)
            self.records_list.setCurrentRow(selected_index)
        self.show_record_details()
        self.refresh_output_controls()
        self.show_recording_failure()

    def show_recording_failure(self):
        task = self.workspace._last_recording_task
        if (not self.workspace.automatic_recording or task is None
                or not task.terminal or not task.error
                or getattr(self, '_reported_recording_failure', None) == task.task_id):
            return
        self._reported_recording_failure = task.task_id
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle('录制未完成')
        dialog.setText(task.error)
        dialog.setInformativeText('应用已尝试退出本次游戏并恢复文件。请查看设置与恢复中的实际恢复结果。')
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()

    def refresh_output_controls(self):
        if self.workspace.automatic_recording:
            return
        w = self.workspace
        if hasattr(self, "demo_output_path"):
            self.demo_output_path.setText(w.settings.video_directory or "未设置视频目录")
        workflow = w._output_workflow
        state = workflow.state if workflow else None
        self.output_status.setText(w.output_status)
        scope = (workflow.task_id, workflow.discovery_token) if workflow else None
        items = tuple(workflow.candidates) if workflow else ()
        if (scope, items) != getattr(self, '_displayed_output_candidates', None):
            self.output_candidates.clear()
            for item in items:
                self.output_candidates.addItem(f'{item.path.name} · {item.bytes:,} 字节', str(item.path))
                self.output_candidates.setItemData(self.output_candidates.count()-1, str(item.path), Qt.ItemDataRole.ToolTipRole)
            self._displayed_output_candidates = scope, items
        self._output_selection_scope = scope
        choosing = state == 'awaiting_video_selection'
        self.output_candidates.setVisible(choosing)
        self.output_choose.setVisible(choosing)
        self.output_choose.setEnabled(choosing and not w.busy)
        retry = state in ('awaiting_video', 'video_error', 'record_error', 'output_cancelled')
        self.output_retry.setVisible(retry)
        self.output_retry.setEnabled(retry and not w.busy)
        cancellable = workflow is not None and state not in ('verified', 'output_cancelled')
        self.output_cancel.setVisible(cancellable)
        self.output_cancel.setEnabled(cancellable and (w._output_process is not None or not w.busy))
        content_scope = (workflow.task_id, workflow.candidate_id, workflow.content_token) if state == 'awaiting_content' else None
        if content_scope != getattr(self, '_content_confirmation_scope', None):
            for check in self.content_checks.values(): check.setChecked(False)
            self.content_head.setValue(-1); self.content_tail.setValue(-1)
        self._content_confirmation_scope = content_scope
        self.content_box.setVisible(content_scope is not None)
        self.content_box.setEnabled(not w.busy)
        self.update_content_confirmation()

    def update_content_confirmation(self, *_):
        if not hasattr(self, 'content_confirm'): return
        ready = (getattr(self, '_content_confirmation_scope', None) is not None
                 and not self.workspace.busy and all(c.isChecked() for c in self.content_checks.values())
                 and all(0 <= spin.value() <= 2 for spin in (self.content_head, self.content_tail)))
        self.content_confirm.setEnabled(ready)

    def choose_output_candidate(self):
        scope = self._output_selection_scope
        path = self.output_candidates.currentData()
        if scope is None or not path: raise DataError('没有当前任务的有效候选。')
        self.workspace.choose_recording_output(Path(path), task_id=scope[0], step_token=scope[1])

    def open_current_output(self):
        from cs2pov.adapters.video import current_signature
        workflow = self.workspace._output_workflow
        scope = self._content_confirmation_scope
        if workflow is None or scope != (workflow.task_id, workflow.candidate_id, workflow.content_token):
            raise DataError('本次成片确认已过期，请重新检查。')
        metadata = workflow.metadata
        if current_signature(metadata.path, directory=workflow.output_directory) != metadata.signature:
            raise DataError('视频已经变化，请重新查找和检查。')
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(metadata.path))):
            raise DataError('没有成功打开本次成片，请检查默认视频播放器。')

    def confirm_output_content(self):
        scope = self._content_confirmation_scope
        if scope is None: raise DataError('当前没有可确认的成片。')
        self.workspace.confirm_recording_content(task_id=scope[0], candidate_id=scope[1], step_token=scope[2],
            **{key: check.isChecked() for key, check in self.content_checks.items()},
            head_seconds=self.content_head.value(), tail_seconds=self.content_tail.value())

    @staticmethod
    def record_text(row):
        try:
            raw = decode_record_payload(row)
        except DataError:
            return f"记录 {row.get('id', '未知')} · 记录不可用（内容损坏）"
        return f"{raw.get('player', '未知玩家')} · {raw.get('map', '未知地图')} · 视频 {raw.get('video_result', raw.get('result', '未知'))} · 恢复 {row['restore_status']}"

    def selected_record(self):
        item = self.records_list.currentItem()
        if item is None:
            return None
        record_id = item.data(Qt.ItemDataRole.UserRole)
        return self.workspace.library.record(record_id) if record_id else None

    def show_record_details(self):
        row = self.selected_record()
        self.record_open_video.setEnabled(False)
        self.record_open_folder.setEnabled(False)
        self.record_reprepare.setEnabled(False)
        self.record_remove.setEnabled(bool(row) and not self.workspace.busy)
        if not row:
            self.record_details.setText("选择一条记录查看视频路径、元数据和恢复结果。")
            return
        try:
            raw = decode_record_payload(row)
        except DataError:
            self.record_details.setText("记录内容损坏，保留数据库并停止打开。")
            return
        lines = [f"记录 ID：{row['id']}", f"视频：{raw.get('video', '未关联')}",
                 f"结果：{raw.get('video_result', raw.get('result', '未知'))} · 恢复：{row['restore_status']}"]
        if raw.get("duration") is not None:
            lines.append(f"时长：{raw['duration']:.3f} 秒 · 分辨率：{raw.get('width', '?')}×{raw.get('height', '?')} · 音轨：{raw.get('audio_streams', '?')}")
        if "metadata_result" in raw:
            lines.append(f"文件检查：{raw['metadata_result']}")
        if "content_result" in raw:
            lines.append(f"画面与声音检查：{raw['content_result']}")
        try:
            self.record_video_path(row)
        except (OSError, ValueError, RuntimeError) as error:
            lines.append("视频位置不可用：" + str(error))
        else:
            self.record_open_video.setEnabled(not self.workspace.busy)
            self.record_open_folder.setEnabled(not self.workspace.busy)
        self.record_reprepare.setEnabled(isinstance(raw.get("draft_snapshot"), dict)
            and callable(getattr(self.workspace, "reprepare_record", None))
            and not self.workspace.busy and not self.workspace.recovery
            and not self.workspace.nvidia_recovery_items)
        self.record_details.setText("\n".join(lines))

    @staticmethod
    def record_video_path(row):
        raw = decode_record_payload(row)
        value = raw.get("video")
        if not value:
            raise DataError("记录尚未关联视频。")
        path = Path(value)
        if path.suffix.casefold() not in VIDEO_SUFFIXES:
            raise DataError("记录引用的不是受支持的视频文件。")
        no_redirection(path)
        if not path.is_file():
            raise DataError("视频文件不存在，记录引用保留。")
        return path

    def open_record_location(self, *, folder=False):
        row = self.selected_record()
        if row is None:
            self.show_record_details()
            return
        try:
            self.workspace.ensure_idle()
            # Always reread the selected database row and current filesystem;
            # displayed text and a previously enabled button are not authority.
            path = self.record_video_path(row)
            self.open_path(str(path.parent if folder else path))
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "记录不可用", str(error))
        finally:
            self.show_record_details()

    def open_selected_record_video(self):
        self.open_record_location()

    def open_selected_record_folder(self):
        self.open_record_location(folder=True)

    def reprepare_selected_record(self):
        row = self.selected_record()
        if row:
            result = self.call(lambda: self.workspace.reprepare_record(row["id"]))
            if result is not None:
                self.navigate(1)

    def remove_selected_record(self):
        row = self.selected_record()
        if row:
            self.call(lambda: self.workspace.remove_record(row["id"]))

    def closeEvent(self, event):
        if self.workspace.busy:
            QMessageBox.warning(self, "任务仍在进行", "请先取消并等待收尾，确认录制状态和游戏恢复结果。")
            event.ignore()
        else:
            event.accept()
