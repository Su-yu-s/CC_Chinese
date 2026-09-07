#!/usr/bin/env python3
"""CC_Chinese（PySide6 版，按 CC_Chinese 设计规范）。

界面：无边框 380×420 黑白灰窗口（QStackedWidget 主界面/设置页）；补丁逻辑全在 core/。
本文件只做界面 + 交互 + 状态展示。core/ 通过 sys.path 顶层名 import，避免改动补丁脚本。
补丁在后台 QThread 运行（Worker 集合保活防闪退），单实例锁防多开。
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

# 提权后子进程的工作目录是 exe 所在目录，不在项目根目录。
# 确保 core/ 在任何 import 之前可被找到。
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QPoint, QPointF, QRectF, QSettings, QSharedMemory, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

CORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "core")
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import detector  # noqa: E402
import installer  # noqa: E402
from best_effort_io import is_windows_admin, is_windowsapps_path  # noqa: E402
import diagnostics as diag  # noqa: E402

APP_ID = "cc-chinese-pyqt"
APP_NAME = "CC_Chinese"
SUB_NAME = APP_NAME
VERSION = "v1.0.0"
AUTHOR = "苏"
GITHUB_URL = "https://github.com/Su-yu-s/CC_Chinese"
FEEDBACK_URL = "https://github.com/Su-yu-s/CC_Chinese/issues"

W, H = 380, 420
PRIMARY = "#1A1A1A"
BORDER = "#EEEEEE"
DIVIDER = "#F0F0F0"
TXT2 = "#888888"
MUTED = "#BBBBBB"
ACCENT_ORANGE = "#F5A623"
ACCENT_BLUE = "#0D6EFD"
ACCENT_GREEN = "#22C55E"
ACCENT_RED = "#EF4444"
FONT = "Microsoft YaHei, Segoe UI, sans-serif"
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
CHECK_URL = os.path.join(ASSETS_DIR, "check.svg").replace("\\", "/")
CHEVRON_URL = os.path.join(ASSETS_DIR, "chevron.svg").replace("\\", "/")

# 状态机：待汉化 / 汉化中 / 恢复中 / 已汉化 / 未找到 / 检测中
STATE_ICON = {"待汉化": "target", "汉化中": "loading", "恢复中": "loading", "已汉化": "check", "未找到": "alert", "检测中": "loading"}
STATE_COLOR = {"待汉化": ACCENT_ORANGE, "汉化中": ACCENT_BLUE, "恢复中": ACCENT_BLUE, "已汉化": ACCENT_GREEN, "未找到": ACCENT_RED, "检测中": MUTED}
STATE_BUTTON = {"待汉化": "一键汉化", "汉化中": "处理中...", "恢复中": "处理中...", "已汉化": "打开 Claude", "未找到": "未检测到", "检测中": "检测中..."}
STATE_ENABLED = {"待汉化": True, "汉化中": False, "恢复中": False, "已汉化": True, "未找到": False, "检测中": False}

_GUARD = None  # 单实例锁

class Worker(QThread):
    """后台线程跑 installer 一个入口，进度经信号回主线程。"""

    progress = Signal(int, str)
    done = Signal(dict)

    def __init__(self, fn, app_dir: str | None):
        super().__init__()
        self._fn = fn
        self._app_dir = app_dir

    def run(self):  # noqa: D102
        try:
            result = self._fn(self._app_dir, progress_cb=lambda p, m: self.progress.emit(p, m))
        except Exception as exc:
            result = {"success": False, "state": "error", "message": str(exc), "log": str(exc)}
        self.done.emit(result)


class _WheelCombo(QComboBox):
    """下拉框：把滚轮转交给外层滚动区。

    QComboBox 默认在鼠标悬停时吞掉滚轮来切换候选项，导致设置页(在 QScrollArea 里)
    停在选项上时页面不滚。这里把滚轮转给最近的 QScrollArea；下拉展开时弹出列表自行
    滚动，不受影响。
    """

    def wheelEvent(self, e):
        sarea = self._nearest_scroll()
        if sarea is not None:
            bar = sarea.verticalScrollBar()
            bar.setValue(bar.value() - e.angleDelta().y())
            e.accept()
        else:
            super().wheelEvent(e)

    def _nearest_scroll(self):
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, QtWidgets.QScrollArea):
                return p
            p = p.parentWidget()
        return None


class TargetIcon(QWidget):
    """状态图标：靶心 / 加载圈 / 对勾 / 感叹号，QPainter 手绘。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(64, 64)
        self._mode = "target"
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    def set_mode(self, mode: str) -> None:
        if mode != self._mode:
            self._mode = mode
            self.update()

    def _tick(self) -> None:
        if self._mode == "loading":
            self._angle = (self._angle + 7) % 360
            self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy, r = 32, 32, 28
        ring = QRectF(cx - r, cy - r, r * 2, r * 2)
        if self._mode == "target":
            p.setPen(QPen(QColor("#E8E8E8"), 1.5))
            p.drawEllipse(ring)
            p.setPen(QPen(QColor(PRIMARY), 1.2))
            p.drawLine(QPointF(cx - r, cy), QPointF(cx - 10, cy))
            p.drawLine(QPointF(cx + r, cy), QPointF(cx + 10, cy))
            p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy - 10))
            p.drawLine(QPointF(cx, cy + r), QPointF(cx, cy + 10))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(PRIMARY))
            p.drawEllipse(QPointF(cx, cy), 4, 4)
        elif self._mode == "loading":
            p.setPen(QPen(QColor(ACCENT_BLUE), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(ring, -self._angle * 16, 90 * 16)
        elif self._mode == "check":
            p.setPen(QPen(QColor(ACCENT_GREEN), 1.5))
            p.drawEllipse(ring)
            p.setPen(QPen(QColor(ACCENT_GREEN), 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath()
            path.moveTo(cx - 14, cy)
            path.lineTo(cx - 3, cy + 11)
            path.lineTo(cx + 15, cy - 12)
            p.drawPath(path)
        elif self._mode == "alert":
            p.setPen(QPen(QColor(ACCENT_RED), 1.5))
            p.drawEllipse(ring)
            p.setPen(QPen(QColor(ACCENT_RED), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 6))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACCENT_RED))
            p.drawEllipse(QPointF(cx, cy + 15), 2, 2)
        p.end()


class PulseDot(QWidget):
    """状态色圆点：处理中脉冲，稳定状态不透明且不启动动画。"""

    def __init__(self, parent=None, color: str | None = None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._color = QColor(color or ACCENT_ORANGE)
        self._phase = 0
        self._active = True
        # ponytail: 图标是给「进程存活/检测中」的脉冲；稳态(待汉化/已汉化/未找到)
        # 无需 40ms 重绘。真机是 WA_TranslucentBackground 分层窗口，子控件每次
        # update() 都会触发整个窗口的 UpdateLayeredWindow，持续脉冲会耗尽响应性。
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self._apply_timer()
            self.update()

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self._apply_timer()

    def hideEvent(self, e):  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def _apply_timer(self) -> None:
        if self._active and self.isVisible():
            self._timer.start(40)
        else:
            self._timer.stop()

    def _tick(self) -> None:
        self._phase = (self._phase + 40) % 2500
        self.update()

    def paintEvent(self, _):  # noqa: N802
        v = self._phase / 2500.0
        frac = 0.675 - 0.325 * math.cos(2 * math.pi * v)
        col = QColor(self._color)
        col.setAlpha(int(255 * frac) if self._active else 255)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(5, 5), 3, 3)
        p.end()


class ProgressBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self._value = 0.0
        self.setVisible(False)

    def set(self, percent: int) -> None:
        self._value = max(0, min(100, percent))
        self.setVisible(True)
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(DIVIDER))
        p.drawRoundedRect(QRectF(0, 1, self.width(), 4), 2, 2)
        p.setBrush(QColor(PRIMARY))
        p.drawRoundedRect(QRectF(0, 1, self.width() * self._value / 100, 4), 2, 2)
        p.end()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._settings = QSettings("CC_Chinese", "CC_Chinese")
        self._auto_detect = self._settings.value("auto_detect", True, type=bool)
        self._target = self._settings.value("target", "auto")
        self._manual_dir = ""
        self._worker = None
        self._workers = set()   # 保活正在运行的 QThread，防止被 GC 提前析构导致闪退
        self._detect_seq = 0    # 检测序号：只采纳最新一次检测结果，防止旧 worker 覆盖新状态
        self._busy = False
        self._closing = False
        self._detecting = True
        self._state = "检测中"
        self._status = {}
        self._toast = None
        self._drag_pos = None
        self._setup_window()
        self._build_ui()
        # 无论"启动自动检测"勾选与否，构造时都做一次静默检测，保证 UI 有真实状态
        # （否则关掉勾选后主界面永远卡"检测中"、按钮不可用）。auto_detect 作为设置保留。
        self._start_detect()

    # ---- 窗口 ----
    def _setup_window(self):
        self.setWindowTitle(APP_NAME)
        # 最大化状态管理(正常=380x420 固定,最大化=屏幕几何)
        self._maximized = False
        self._normal_geo = None
        # 用 min/max 约束模拟固定尺寸:最大化时可解除,正常态不可手动 resize
        self.setMinimumSize(W, H)
        self.setMaximumSize(W, H)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window | Qt.WindowType.WindowMinimizeButtonHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        try:
            self.setWindowIcon(QtGui.QIcon(os.path.join(ASSETS_DIR, "icon.ico")))
        except Exception:
            pass

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)   # 给圆角边框+阴影留边
        root.setSpacing(0)
        self._card = QFrame(self)
        self._card.setObjectName("windowcard")
        cl = QVBoxLayout(self._card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self._stack = QStackedWidget(self._card)
        self._stack.addWidget(self._build_main_page())
        self._settings_page = self._build_settings_page()
        self._stack.addWidget(self._settings_page)
        cl.addWidget(self._stack)
        root.addWidget(self._card)
        # 窗口阴影(0 1px 3px,轻微)
        self._shadow = QtWidgets.QGraphicsDropShadowEffect(self._card)
        self._shadow.setBlurRadius(8)
        self._shadow.setOffset(0, 2)
        self._shadow.setColor(QtGui.QColor(0, 0, 0, 60))
        self._card.setGraphicsEffect(self._shadow)
        # Toast overlay
        self._toast = Toast(self)
        self._apply_state()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if sys.platform == "win32" and QtWidgets.QApplication.platformName() == "windows":
            # The custom titlebar logo is a QLabel, not the taskbar icon.
            # Apply HICONs after Qt has created its frameless native window.
            from window_icon import apply_window_icon
            try:
                apply_window_icon(int(self.winId()), Path(ASSETS_DIR) / "icon.ico")
            except OSError as exc:
                self._native_icon_error = str(exc)

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("mainpage")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_titlebar())
        body = QWidget()
        body.setObjectName("body")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(24, 0, 24, 0)
        bl.setSpacing(0)
        bl.addWidget(self._build_state())
        self._progress = ProgressBar(body)
        self._progress.setVisible(False)
        bl.addWidget(self._progress)
        bl.addSpacing(16)
        self._primary = QPushButton("", body)
        self._primary.setObjectName("primary")
        self._primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self._primary.setFixedHeight(44)
        self._primary.clicked.connect(self._on_primary)
        bl.addWidget(self._primary)
        bl.addSpacing(24)
        div = QFrame(body)
        div.setObjectName("divider")
        div.setFrameShape(QFrame.Shape.HLine)
        bl.addWidget(div)
        bl.addSpacing(24)
        row = QHBoxLayout()
        row.setSpacing(4)
        for text, handler in (("恢复原样", self._confirm_restore),
                              ("设置", self._go_settings)):
            b = QPushButton(text)
            b.setObjectName("secondary")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(36)
            b.clicked.connect(handler)
            row.addWidget(b, 1)
        bl.addLayout(row)
        bl.addSpacing(12)
        version = QLabel(VERSION)
        version.setObjectName("version")
        bl.addWidget(version)
        v.addWidget(body, 1)
        return page

    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("titlebar")
        bar.setFixedHeight(48)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20, 0, 12, 0)
        lay.setSpacing(10)
        logo = QLabel("C")
        logo.setObjectName("logo")
        logo.setFixedSize(20, 20)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(logo)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        lay.addWidget(title)
        lay.addStretch(1)
        # 窗口控制按钮组: − □ ×,间距 4px,右缘 12px
        wins = QHBoxLayout()
        wins.setSpacing(4)
        minb = QPushButton("—")
        minb.setObjectName("minbtn")
        minb.clicked.connect(self.showMinimized)
        wins.addWidget(minb)
        maxb = QPushButton("□")
        maxb.setObjectName("maxbtn")
        maxb.clicked.connect(self._toggle_maximize)
        wins.addWidget(maxb)
        closeb = QPushButton("×")
        closeb.setObjectName("closebtn")
        closeb.clicked.connect(self.close)
        wins.addWidget(closeb)
        lay.addLayout(wins)
        return bar

    def _build_state(self) -> QWidget:
        state = QWidget()
        state.setObjectName("state")
        sv = QVBoxLayout(state)
        sv.setContentsMargins(0, 20, 0, 16)
        sv.setSpacing(4)
        self._icon = TargetIcon(state)
        sv.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignHCenter)
        sv.addSpacing(20)
        pill = QFrame(state)
        pill.setObjectName("pill")
        pl = QHBoxLayout(pill)
        pl.setContentsMargins(6, 2, 12, 2)
        pl.setSpacing(6)
        self._dot = PulseDot(pill)
        pl.addWidget(self._dot)
        self._pill_label = QLabel("待汉化")
        self._pill_label.setObjectName("pilltext")
        pl.addWidget(self._pill_label)
        pill.setFixedHeight(32)
        sv.addWidget(pill, 0, Qt.AlignmentFlag.AlignHCenter)
        self._desc = QLabel("")
        self._desc.setObjectName("desc")
        self._desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._desc.setWordWrap(False)
        sv.addWidget(self._desc)
        return state

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("settingspage")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        # 标题栏：← 返回  CC_Chinese  ×
        bar = QWidget()
        bar.setObjectName("titlebar")
        bar.setFixedHeight(48)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 0, 12, 0)
        bl.setSpacing(10)
        back = QPushButton("← 返回")
        back.setObjectName("backbtn")
        back.clicked.connect(self._go_main)
        bl.addWidget(back)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        bl.addWidget(title)
        bl.addStretch(1)
        closeb = QPushButton("×")
        closeb.setObjectName("closebtn")
        closeb.clicked.connect(self.close)
        bl.addWidget(closeb)
        v.addWidget(bar)
        # 滚动内容区（QScrollArea，细滚动条，返回按钮随内容滚动）
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("settingsscroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        body = QWidget()
        body.setObjectName("settingsbody")
        bv = QVBoxLayout(body)
        bv.setContentsMargins(24, 20, 24, 24)
        bv.setSpacing(0)
        heading = QLabel("设置")
        heading.setObjectName("settingsTitle")
        bv.addWidget(heading)
        bv.addSpacing(24)
        # 安装位置
        bv.addWidget(self._group_label("安装位置"))
        bv.addSpacing(8)
        self._target_select = _WheelCombo()
        self._target_select.addItem("自动检测", "auto")
        self._target_select.addItem("WindowsApps 商店版", "windows-apps")
        self._target_select.addItem("AppData 本地安装版", "app-data")
        self._target_select.addItem("手动指定目录", "manual")
        idx = self._target_select.findData(self._target)
        if idx >= 0:
            self._target_select.setCurrentIndex(idx)
        self._target_select.currentIndexChanged.connect(self._on_target_changed)
        bv.addWidget(self._target_select)
        self._manual_input = QLineEdit()
        self._manual_input.setPlaceholderText("输入 Claude app 目录，如 C:\\...\\Claude\\app")
        self._manual_input.textChanged.connect(lambda t: setattr(self, "_manual_dir", t.strip()))
        self._manual_input.setVisible(self._target == "manual")
        bv.addWidget(self._manual_input)
        browse = QPushButton("选择目录...")
        browse.setObjectName("secondary")
        browse.clicked.connect(self._browse_dir)
        browse.setVisible(self._target == "manual")
        self._browse_btn = browse
        bv.addWidget(browse)
        bv.addSpacing(32)
        # 通用
        bv.addWidget(self._group_label("通用"))
        bv.addSpacing(8)
        self._detect_check = self._opt_row(bv, "启动时自动检测 Claude", self._auto_detect)
        self._detect_check.toggled.connect(self._on_detect_toggled)
        bv.addSpacing(32)
        # 关于
        bv.addWidget(self._group_label("关于"))
        bv.addSpacing(8)
        info = QLabel(f"版本 {VERSION} · {AUTHOR}")
        info.setObjectName("aboutline")
        bv.addWidget(info)
        bv.addSpacing(8)
        links = QHBoxLayout()
        links.setSpacing(16)
        for text, url in (("GitHub", GITHUB_URL), ("反馈问题", FEEDBACK_URL)):
            btn = QPushButton(text)
            btn.setObjectName("linkbtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            links.addWidget(btn)
        bv.addLayout(links)
        bv.addSpacing(24)
        # 返回主界面（200px 居中，随内容滚动）
        back_main = QPushButton("返回主界面")
        back_main.setObjectName("primary")
        back_main.setFixedWidth(200)
        back_main.setFixedHeight(44)
        back_main.clicked.connect(self._go_main)
        bv.addWidget(back_main, 0, Qt.AlignmentFlag.AlignHCenter)
        bv.addSpacing(8)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)
        return page

    def _group_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("groupLabel")
        return lbl

    def _opt_row(self, lay, text: str, checked: bool = False) -> QCheckBox:
        """一行「文字在左 + 黑底白勾复选框在右」，返回复选框。"""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(text)
        lbl.setObjectName("optLabel")
        row.addWidget(lbl)
        row.addStretch(1)
        cb = QCheckBox()
        cb.setChecked(checked)
        row.addWidget(cb)
        lay.addLayout(row)
        return cb

    # ---- 拖拽 ----
    def mousePressEvent(self, e):  # noqa: N802
        # 无边框窗口拖拽交给系统：Windows 原生 startSystemMove，平滑不卡、不与 QStackedWidget 冲突
        # 最大化时锁定，不可拖拽（全屏固定）
        if e.button() == Qt.MouseButton.LeftButton and not self._maximized:
            try:
                win = self.windowHandle()
                if win is not None:
                    win.startSystemMove()
            except Exception:
                pass
            e.accept()

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        # 双击标题栏空白处:切换 正常/最大化(落在按钮上时按钮会先行消费掉事件)
        if e.position().y() < 48:
            self._toggle_maximize()

    def keyPressEvent(self, e):  # noqa: N802
        # Esc 退出全屏(真全屏盖任务栏,必须有键盘退出途径)
        if e.key() == Qt.Key.Key_Escape and self._maximized:
            self._restore_normal()
        else:
            super().keyPressEvent(e)

    # ---- 最大化 / 还原 ----
    def _toggle_maximize(self):
        self._maximize() if not self._maximized else self._restore_normal()

    def _maximize(self):
        if self._maximized:
            return
        self._normal_geo = self.geometry()
        self._maximized = True
        # 最大化:填充屏幕可用工作区(保留任务栏,不覆盖)
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.setGeometry(geo)
        self._apply_maximize_style()

    def _restore_normal(self):
        if not self._maximized:
            return
        self._maximized = False
        self.setMinimumSize(W, H)
        self.setMaximumSize(W, H)
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        if self._normal_geo and self._normal_geo.isValid():
            geo = self._normal_geo
        else:
            c = screen.availableGeometry().center()
            geo = QtCore.QRect(c.x() - W // 2, c.y() - H // 2, W, H)
        self.setGeometry(geo)
        self._apply_maximize_style()

    def _apply_maximize_style(self):
        # 全屏:直角、无边框、无阴影、贴边,内容垂直居中;正常:圆角12+边框+阴影+留边,内容顶部紧凑
        if self._maximized:
            self._card.setStyleSheet("#windowcard { background: #FFFFFF; border: none; border-radius: 0px; }")
            self._shadow.setBlurRadius(0)
            self._shadow.setOffset(0, 0)
        else:
            self._card.setStyleSheet("")   # 回落全局 STYLESHEET 里的 #windowcard(圆角12+边框)
            self._shadow.setBlurRadius(8)
            self._shadow.setOffset(0, 2)
        self.layout().setContentsMargins(*(0, 0, 0, 0) if self._maximized else (6, 6, 6, 6))
        # 主界面内容垂直居中开关(仅全屏)
        if hasattr(self, "_main_layout"):
            self._main_layout.setStretch(1, 1 if self._maximized else 0)
            self._main_layout.setStretch(3, 1 if self._maximized else 0)

    # ---- 状态机 ----
    def _map_state(self, status) -> str:
        if self._busy:
            return getattr(self, "_busy_state", "汉化中")
        if self._detecting:
            return "检测中"
        if not status.get("installed"):
            return "未找到"
        if status.get("localized"):
            return "已汉化"
        return "待汉化"

    def _set_state(self, state: str):
        self._state = state
        self._icon.set_mode(STATE_ICON[state])
        self._dot.set_color(STATE_COLOR[state])
        # ponytail: 只在等待态脉冲，减小分层窗口每 40ms 一次的整体重绘
        self._dot.set_active(state in {"检测中", "汉化中", "恢复中"})
        self._pill_label.setText(state)
        self._primary.setText(STATE_BUTTON[state])
        self._primary.setEnabled(STATE_ENABLED[state] and not self._busy)
        self._primary.setProperty("state", state)
        self._primary.style().unpolish(self._primary)
        self._primary.style().polish(self._primary)

    def _apply_state(self):
        self._set_state(self._state)

    def _launch(self, fn, app_dir, done_cb) -> None:
        """启动并保活一个后台 Worker，防止 QThread 提前被 GC 析构导致闪退。"""
        w = Worker(fn, app_dir)
        self._workers.add(w)
        w.progress.connect(self._on_progress)
        w.done.connect(done_cb)
        w.finished.connect(lambda: self._on_worker_finished(w))
        w.start()
        self._worker = w

    def _on_worker_finished(self, worker) -> None:
        self._workers.discard(worker)
        if self._worker is worker:
            self._worker = None
        if self._closing and not any(item.isRunning() for item in self._workers):
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):  # noqa: N802
        if self._busy:
            event.ignore()
            return
        running = [worker for worker in self._workers if worker.isRunning()]
        if running:
            # Do not block the GUI thread and do not destroy live QThreads.
            # Detection has bounded subprocess timeouts; hide immediately and
            # close for real when the final worker emits finished.
            self._closing = True
            self.hide()
            for worker in running:
                worker.requestInterruption()
            event.ignore()
            return
        super().closeEvent(event)

    # ---- 检测 ----
    def _start_detect(self):
        self._refresh_status()

    def _on_detect_done(self, status, seq=None):
        if seq is not None and seq != self._detect_seq:
            return  # 过期检测结果：丢弃，避免旧 worker 覆盖新状态
        self._detecting = False
        self._status = status
        self._set_state(self._map_state(status))
        self._desc.setText(status.get("message", ""))

    def _refresh_status(self):
        self._detect_seq += 1
        seq = self._detect_seq
        self._detecting = True
        self._set_state("检测中")
        self._launch(
            lambda _a, progress_cb=None: detector.build_status(target=self._target, app_dir=self._manual_dir or None),
            None,
            lambda status: self._on_detect_done(status, seq),
        )

    # ---- 页面切换 ----
    def _release_focus(self) -> None:
        # WA_TranslucentBackground 是分层窗口：若被隐藏页仍持有焦点，Windows 在
        # QStackedWidget 重建页面时「焦点处理 + UpdateLayeredWindow」可能互锁，真机卡死。
        # 切换前先把焦点还给无焦点，让分页重建不再带着一个输入控件。
        fw = QtWidgets.QApplication.focusWidget()
        if fw is not None:
            fw.clearFocus()

    def _go_settings(self):
        self._release_focus()
        self._stack.setCurrentWidget(self._settings_page)

    def _go_main(self):
        self._release_focus()
        self._stack.setCurrentIndex(0)

    # ---- 主按钮 ----
    def _on_primary(self):
        if self._state == "已汉化":
            self._run("open")
        elif self._state == "待汉化":
            self._run("install")

    def _confirm_restore(self):
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("恢复原样")
        msg.setText("从匹配的安全快照恢复官方资源，并还原汉化前的 locale/font 原值？不会删除 Claude 数据。")
        msg.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        msg.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        if msg.exec() == QtWidgets.QMessageBox.StandardButton.Yes:
            self._run("restore")

    def _run(self, command):
        if self._busy:
            return
        app_dir = self._status.get("appDir")
        if command == "install":
            fn = installer.run_install
        else:
            fn = {"restore": installer.run_restore,
                  "open": installer.run_open}.get(command)
        if fn is None:
            return
        if command in {"install", "restore"} and app_dir and is_windowsapps_path(Path(app_dir)):
            operation = fn
            if is_windows_admin():
                fn = lambda target, progress_cb=None: operation(
                    target, progress_cb=progress_cb, elevated=True
                )
            else:
                fn = lambda _target, progress_cb=None: {
                    "success": False,
                    "state": "error",
                    "error_code": "PERMISSION_DENIED",
                    "message": "请以管理员身份重新启动 CC_Chinese。",
                }
        self._busy = True
        self._last_command = command
        self._busy_state = "恢复中" if command == "restore" else "汉化中"
        self._set_state(self._busy_state)
        self._progress.set(0)
        self._launch(fn, app_dir, self._on_action_done)

    def _on_progress(self, percent, message):
        self._progress.set(percent)
        self._desc.setText(message)

    def _on_action_done(self, result):
        self._busy = False
        self._progress.setVisible(False)
        if result.get("success"):
            self._toast.show_message(result.get("message", "完成"))
        else:
            # 失败时写脱敏诊断日志，并展示可复制详情
            context = {"target_id": str(self._status.get("appDir", ""))}
            extra_context = result.get("diagnostic_context")
            if isinstance(extra_context, dict):
                context.update(extra_context)
            report = diag.DiagnosticLogger().record_failure(
                action=self._last_command or "unknown",
                result=result,
                context=context,
            )
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("操作失败")
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            box.setText(f"{result.get('message', '未知错误')}\n\n{report.advice}")
            copy_button = box.addButton("复制详情", QtWidgets.QMessageBox.ButtonRole.ActionRole)
            log_button = box.addButton("打开日志目录", QtWidgets.QMessageBox.ButtonRole.ActionRole)
            box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
            box.exec()
            if box.clickedButton() is copy_button:
                QtWidgets.QApplication.clipboard().setText(report.copy_text())
                self._toast.show_message("已复制故障详情")
            elif box.clickedButton() is log_button:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(report.log_path.parent)))
        # 每次操作完成后都重新检测真实文件状态，不使用结果文件或退出码乐观显示成功
        self._refresh_status()

    # ---- 设置项回调 ----
    def _on_detect_toggled(self, checked):
        self._auto_detect = checked
        self._settings.setValue("auto_detect", checked)

    def _on_target_changed(self):
        self._target = self._target_select.currentData()
        self._settings.setValue("target", self._target)
        is_manual = self._target == "manual"
        self._manual_input.setVisible(is_manual)
        self._browse_btn.setVisible(is_manual)
        if is_manual:
            self._manual_dir = self._manual_input.text().strip()
        self._refresh_status()

    def _browse_dir(self):
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "选择 Claude app 目录")
        if chosen:
            self._manual_input.setText(chosen)
            self._manual_dir = chosen
            self._refresh_status()

class Toast(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toast")
        self._lbl = QLabel("", self)
        self._lbl.setWordWrap(True)
        lay = QHBoxLayout(self)
        lay.addWidget(self._lbl)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, ms: int = 2400):
        self._timer.stop()
        self._lbl.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        if parent:
            self.move(max(8, (parent.width() - self.width()) // 2), parent.height() - self.height() - 24)
        self.show()
        self.raise_()
        self._timer.start(ms)


STYLESHEET = f"""
* {{ font-family: {FONT}; color: {PRIMARY}; }}
#mainpage, #settingspage, #body, #state, #titlebar, #settingsbody {{ background: transparent; }}
#windowcard {{ background: #FFFFFF; border: 1px solid #E8E8E8; border-radius: 12px; }}
#logo {{ background: {PRIMARY}; color: #FFFFFF; border-radius: 4px; font-size: 13px; font-weight: 700; }}
#title {{ color: {PRIMARY}; font-size: 13px; font-weight: 500; }}
/* 标题栏窗口控制按钮:统一 36x36 */
#minbtn, #maxbtn, #closebtn {{ background: transparent; color: #666666; border: none;
                      width: 36px; height: 36px; border-radius: 6px; font-size: 20px; }}
#maxbtn {{ font-size: 16px; }}
#minbtn {{ font-size: 16px; padding-bottom: 2px; }}
#minbtn:hover, #maxbtn:hover {{ background: #F0F0F0; color: #1A1A1A; }}
#maxbtn:pressed {{ background: #E0E0E0; }}
#closebtn:hover {{ background: #E81123; color: #FFFFFF; }}
#backbtn {{ background: transparent; color: #666666; border: none; font-size: 13px; padding: 4px 8px; }}
#backbtn:hover {{ color: {PRIMARY}; }}
#pill {{ background: #FAFAFA; border: 1px solid {BORDER}; border-radius: 16px; }}
#pilltext {{ color: {PRIMARY}; font-size: 14px; font-weight: 600; }}
#desc {{ color: {TXT2}; font-size: 13px; }}
#primary {{ background: {PRIMARY}; color: #FFFFFF; border: none; border-radius: 8px; font-size: 14px; font-weight: 500; }}
#primary:hover {{ background: #333333; }}
#primary:pressed {{ background: #000000; }}
#primary:disabled {{ background: #DDDDDD; color: #999999; }}
#secondary {{ background: #FFFFFF; color: #666666; border: 1px solid {BORDER}; border-radius: 6px; font-size: 12px; padding: 0 4px; }}
#secondary:hover {{ color: {PRIMARY}; border-color: #CCCCCC; }}
#secondary:disabled {{ background: #FFFFFF; color: #666666; border: 1px solid {BORDER}; }}
#divider {{ background: {DIVIDER}; max-height: 1px; }}
#footer {{ background: #FFFFFF; border-top: 1px solid {DIVIDER}; }}
#version {{ color: {MUTED}; font-size: 11px; }}
#settingsTitle {{ font-size: 17px; font-weight: 700; }}
#groupLabel {{ color: {PRIMARY}; font-size: 13px; font-weight: 600; }}
#optLabel {{ color: {PRIMARY}; font-size: 14px; }}
#subheading {{ color: {PRIMARY}; font-size: 13px; font-weight: 600; }}
#aboutline {{ color: {TXT2}; font-size: 13px; padding: 2px 0; }}
#toast {{ background: #FFFFFF; border: 1px solid #E0E0E0; border-radius: 8px; }}
#toast QLabel {{ color: #1A1A1A; padding: 8px 14px; font-size: 12px; }}
QComboBox, QLineEdit {{ height: 36px; min-height: 36px; padding: 0 12px; border: 1px solid {BORDER}; border-radius: 6px; background: #FFFFFF; font-size: 13px; }}
QComboBox:focus, QLineEdit:focus {{ border-color: {PRIMARY}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url("{CHEVRON_URL}"); width: 12px; height: 12px; margin-right: 8px; }}
QCheckBox {{ font-size: 13px; spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border: 1.5px solid #DDDDDD; border-radius: 4px; background: #FFFFFF; }}
QCheckBox::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; image: url("{CHECK_URL}"); }}
#linkbtn {{ background: transparent; border: none; color: #999999; font-size: 12px; padding: 4px 6px; }}
#linkbtn:hover {{ color: {PRIMARY}; }}
QScrollBar:vertical {{ width: 6px; background: transparent; border: none; margin: 0; }}
QScrollBar::handle:vertical {{ background: #DDDDDD; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #BBBBBB; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def _release_instance():
    global _GUARD
    if _GUARD is not None:
        try:
            _GUARD.destroy()
        except Exception:
            pass


def _handle_elevation_cli(argv: list[str]) -> int | None:
    """Parse elevation-broker CLI flags and run the action directly; returns None to fall through to GUI."""
    action = target_hint = nonce = None
    i = 0
    while i < len(argv):
        if argv[i] == "--elevated-action" and i + 1 < len(argv):
            action = argv[i + 1]
            i += 2
        elif argv[i] == "--target-hint" and i + 1 < len(argv):
            target_hint = argv[i + 1]
            i += 2
        elif argv[i] == "--nonce" and i + 1 < len(argv):
            nonce = argv[i + 1]
            i += 2
        else:
            i += 1
    if action and target_hint and nonce:
        import elevation as _e
        return _e.run_elevated_action(action, target_hint, nonce)
    return None


def _configure_application_icon(app):
    icon = QtGui.QIcon(os.path.join(ASSETS_DIR, "icon.ico"))
    # Decode before the first native window/taskbar button is created.
    sizes = (16, 32, 48, 256)
    decoded = all(not icon.pixmap(size, size).isNull() for size in sizes)
    app.setWindowIcon(icon)
    return decoded


def _run_self_test(argv: list[str]) -> int | None:
    """Read-only frozen-build smoke test used by the release pipeline."""
    if "--self-test" not in argv:
        return None
    source_check = installer.patch_json.validate_source_resources()
    native_icon_test = "--native-icon" in argv and sys.platform == "win32"
    os.environ["QT_QPA_PLATFORM"] = "windows" if native_icon_test else "offscreen"
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([argv[0]])
    if not source_check["success"] or not _configure_application_icon(app):
        return 2
    if native_icon_test:
        from window_icon import apply_window_icon
        # Native HWND, but never show this diagnostic window or modify Claude.
        window = QtWidgets.QWidget()
        try:
            apply_window_icon(int(window.winId()), Path(ASSETS_DIR) / "icon.ico")
        except OSError:
            return 3
        finally:
            window.close()
    return 0


def main():
    global _GUARD
    # 提权后进程：跳过 GUI，直接执行操作
    result = _handle_elevation_cli(sys.argv)
    if result is not None:
        sys.exit(result)
    self_test = _run_self_test(sys.argv)
    if self_test is not None:
        return self_test
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CC_Chinese.Desktop")
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    _configure_application_icon(app)
    app.setQuitOnLastWindowClosed(True)
    app.setStyleSheet(STYLESHEET)

    guard = QSharedMemory(APP_ID)
    if not guard.create(1):
        print("已在运行，本次不再重复启动。")
        _GUARD = guard
        return 0
    _GUARD = guard

    window = MainWindow()
    window.show()
    center = app.primaryScreen().availableGeometry().center()
    window.move(center.x() - window.width() // 2, center.y() - window.height() // 2)
    sys.exit(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
