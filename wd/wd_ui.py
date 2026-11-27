"""PySide6 主界面：设备发现、配对授权、设置与远程窗口。"""
import os
import subprocess
import threading
import time

from PySide6.QtCore import Qt, QEvent, QSize, Signal, QTimer, QPropertyAnimation
from PySide6.QtGui import QImage, QPixmap, QColor, QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QScrollArea, QFrame, QComboBox, QCheckBox, QSpinBox, QLineEdit,
    QDialog, QDialogButtonBox, QTextEdit, QSizePolicy, QMessageBox, QFileDialog,
)

from . import wd_theme
from .wd_discovery import DiscoveryWorker, primary_ip, run_quiet
from .wd_input import qt_key_to_vk
from .wd_session import ClientSession, ServerSession

QUALITY_PRESETS = ["auto", "office", "sharp", "latency", "power"]
QUALITY_LABELS = ["自动", "清晰办公", "超清", "流畅 60", "节能"]


def network_status() -> str:
    ip = primary_ip()
    ssid = ""
    if os.name == "nt":
        try:
            out = run_quiet(["netsh", "wlan", "show", "interfaces"]).decode("utf-8", "ignore")
            for line in out.splitlines():
                if "SSID" in line and ":" in line:
                    ssid = line.split(":", 1)[1].strip()
                    break
        except (OSError, subprocess.SubprocessError):
            pass
    return f"局域网 · {ip}" + (f" · {ssid}" if ssid and ssid != "N/A" else "")


class DragBar(QWidget):
    def __init__(self, parent_window, object_name="titleBar"):
        super().__init__(parent_window)
        self.window_ref = parent_window
        self.setObjectName(object_name)
        self.setFixedHeight(52)
        self.dragging = False
        self.origin = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.origin = event.globalPosition().toPoint() - self.window_ref.pos()
            self.setCursor(QCursor(Qt.ClosedHandCursor))

    def mouseMoveEvent(self, event):
        if self.dragging and self.origin is not None:
            self.window_ref.move(event.globalPosition().toPoint() - self.origin)

    def mouseReleaseEvent(self, event):
        self.dragging = False
        self.origin = None
        self.unsetCursor()


class DeviceRow(QWidget):
    connect_requested = Signal(dict)

    def __init__(self, device: dict, paired: bool, can_control: bool):
        super().__init__()
        self.device = dict(device)
        self.setObjectName("deviceRow")
        self.setFixedHeight(76)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 14, 10)
        layout.setSpacing(14)

        icon = QLabel("▣")
        icon.setStyleSheet("font-size:26px; color:#67A9FF;")
        icon.setFixedWidth(28)
        layout.addWidget(icon)

        info = QVBoxLayout()
        info.setSpacing(3)
        name = QLabel(self.device.get("name", "未知设备"))
        name.setStyleSheet("font-weight:600; font-size:16px;")
        sub = QLabel(f"{self.device.get('ip', '')}:{self.device.get('port', '')}")
        sub.setObjectName("subtext")
        info.addWidget(name)
        info.addWidget(sub)
        layout.addLayout(info, 1)

        color = "#55C99B" if paired else "#E8B86B"
        bg = "rgba(85,201,155,0.12)" if paired else "rgba(232,184,107,0.12)"
        state = QLabel("已配对" if paired else "未配对")
        state.setStyleSheet(
            f"color:{color}; background:{bg}; border-radius:10px; padding:3px 10px; font-size:14px;"
        )
        layout.addWidget(state)

        self.button = QPushButton("连接桌面 →" if paired else "配对连接 →")
        self.button.setObjectName("primary")
        self.button.setFixedWidth(112)
        self.button.setEnabled(can_control)
        self.button.clicked.connect(lambda: self.connect_requested.emit(self.device))
        layout.addWidget(self.button)

    def set_connecting(self, connecting: bool):
        self.button.setEnabled(not connecting)
        self.button.setText("连接中…" if connecting else self.button.text())


class PairDialog(QDialog):
    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("配对请求")
        self.setModal(True)
        self.auto_trusted = False
        self.setFixedWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        title = QLabel(f"{info.get('name')} 请求控制本机")
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"来源：{info.get('ip', '')}"))
        layout.addWidget(QLabel("请核对双方显示的认证短码："))
        code = QLabel(info.get("code", ""))
        code.setObjectName("mono")
        layout.addWidget(code)
        self.auto_box = QCheckBox("可信设备：以后免确认自动连接")
        self.auto_box.setEnabled(info.get("auto_available", False))
        layout.addWidget(self.auto_box)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("允许连接")
        buttons.button(QDialogButtonBox.Cancel).setText("拒绝")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        self.auto_trusted = self.auto_box.isChecked()
        super().accept()


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("设置")
        self.setFixedWidth(520)
        self._build()
        self._load()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(6)
        layout.addWidget(QLabel("常规"))
        self.role_combo = QComboBox()
        self.role_combo.addItems(["控制端", "被控端", "双向模式"])
        layout.addWidget(QLabel("运行角色"))
        layout.addWidget(self.role_combo)
        self.allow_box = QCheckBox("允许其他电脑控制本机（被控端/双向模式有效）")
        layout.addWidget(self.allow_box)
        self.auth_combo = QComboBox()
        self.auth_combo.addItems(["每次确认（免密码）", "可信设备自动连接", "免密直连（自动信任）"])
        layout.addWidget(QLabel("授权模式"))
        layout.addWidget(self.auth_combo)
        self.auth_hint = QLabel("")
        self.auth_hint.setObjectName("hint")
        self.auth_hint.setWordWrap(True)
        self.auth_combo.currentIndexChanged.connect(self._update_auth_hint)
        layout.addWidget(self.auth_hint)

        layout.addWidget(QLabel("显示与性能"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(QUALITY_LABELS)
        layout.addWidget(QLabel("性能预设"))
        layout.addWidget(self.quality_combo)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["深色", "浅色"])
        layout.addWidget(QLabel("主题"))
        layout.addWidget(self.theme_combo)

        layout.addWidget(QLabel("网络"))
        ports = QHBoxLayout()
        self.discovery_port = QSpinBox()
        self.discovery_port.setRange(1024, 65535)
        self.service_port = QSpinBox()
        self.service_port.setRange(1024, 65535)
        ports.addWidget(QLabel("发现端口"))
        ports.addWidget(self.discovery_port)
        ports.addWidget(QLabel("服务端口"))
        ports.addWidget(self.service_port)
        layout.addLayout(ports)

        layout.addWidget(QLabel("可信设备"))
        self.trusted = QTextEdit()
        self.trusted.setReadOnly(True)
        self.trusted.setFixedHeight(76)
        layout.addWidget(self.trusted)
        revoke_row = QHBoxLayout()
        self.revoke_id = QLineEdit()
        self.revoke_id.setPlaceholderText("输入设备 ID 撤销授权")
        revoke = QPushButton("撤销信任")
        revoke.clicked.connect(self._revoke)
        revoke_row.addWidget(self.revoke_id)
        revoke_row.addWidget(revoke)
        layout.addLayout(revoke_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load(self):
        c = self.config
        role_map = {"controller": 0, "controlled": 1, "both": 2}
        self.role_combo.setCurrentIndex(role_map.get(c.role, 0))
        self.allow_box.setChecked(bool(c.get("allow_connection")))
        auth_index = {"confirm": 0, "trusted": 1, "auto": 2}.get(c.get("auth_mode"), 0)
        self.auth_combo.setCurrentIndex(auth_index)
        quality_map = {name: index for index, name in enumerate(QUALITY_PRESETS)}
        self.quality_combo.setCurrentIndex(quality_map.get(c.get("quality", "auto"), 0))
        self.theme_combo.setCurrentIndex(1 if c.get("theme") == "light" else 0)
        self.discovery_port.setValue(int(c.get("discovery_port", 47381)))
        self.service_port.setValue(int(c.get("service_port", 47382)))
        self._update_auth_hint()
        self._refresh_trusted()

    def _update_auth_hint(self):
        hints = [
            "首次连接需被控端点“允许连接”；可保存为可信设备。",
            "首次确认后，勾选自动信任的设备以后无需确认。",
            "只要本机“允许连接”开启，局域网内设备点击即可连，不再弹确认。仅建议在可信 WiFi 使用。",
        ]
        self.auth_hint.setText(hints[self.auth_combo.currentIndex()])

    def _refresh_trusted(self):
        known = self.config.get("known_devices", {})
        lines = []
        for device_id, info in known.items():
            mark = "自动" if info.get("auto") else "每次确认"
            lines.append(f"{info.get('name', '?')}  [{mark}]\n  {device_id}")
        self.trusted.setPlainText("\n".join(lines) if lines else "暂无可信设备")

    def _revoke(self):
        device_id = self.revoke_id.text().strip()
        if device_id:
            self.config.forget(device_id)
            self.revoke_id.clear()
            self._refresh_trusted()

    def accept(self):
        c = self.config
        c.set("role", ["controller", "controlled", "both"][self.role_combo.currentIndex()])
        c.set("allow_connection", self.allow_box.isChecked())
        c.set("auth_mode", ["confirm", "trusted", "auto"][self.auth_combo.currentIndex()])
        c.set("quality", QUALITY_PRESETS[self.quality_combo.currentIndex()])
        c.set("theme", "light" if self.theme_combo.currentIndex() == 1 else "dark")
        c.set("discovery_port", self.discovery_port.value())
        c.set("service_port", self.service_port.value())
        super().accept()


try:  # 只在真正显示画面时才需要，缺了也不该影响主界面启动
    import cv2
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None


def _display_pixmap(img: QImage, logical: QSize, dpr: float, sharpen: bool = True) -> QPixmap:
    """把解码帧缩放到显示尺寸。

    远程画面被缩小显示时，H.264 解码本身偏软，再叠一次双线性重采样，
    小字边缘就被抹平了——看起来就是"糊"。这里改成面积平均缩放（保留更多
    高频细节）并补一档轻度锐化，同样的带宽下文字会干净不少。
    """
    if logical.width() <= 0 or logical.height() <= 0:
        return QPixmap.fromImage(img)
    target = QSize(max(1, int(round(logical.width() * dpr))),
                   max(1, int(round(logical.height() * dpr))))
    shrink = target.width() < img.width() - 2 or target.height() < img.height() - 2
    pix = None
    if shrink and cv2 is not None and np is not None and img.format() == QImage.Format_RGB888:
        try:
            rows = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(
                img.height(), img.bytesPerLine())
            src = rows[:, : img.width() * 3].reshape(img.height(), img.width(), 3)
            small = cv2.resize(src, (target.width(), target.height()),
                               interpolation=cv2.INTER_AREA)
            amount = min(0.5, 0.15 + 0.5 * (1.0 - target.width() / max(1, img.width())))
            if sharpen and amount > 0.05:
                blur = cv2.GaussianBlur(small, (0, 0), 0.8)
                small = cv2.addWeighted(small, 1.0 + amount, blur, -amount, 0)
            view = QImage(small.data, target.width(), target.height(),
                          target.width() * 3, QImage.Format_RGB888)
            pix = QPixmap.fromImage(view)
        except Exception:
            pix = None
    if pix is None:
        pix = QPixmap.fromImage(
            img.scaled(target, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
    # 标明物理像素比，避免系统再做一次放大（高 DPI 下这是最常见的发糊原因）。
    pix.setDevicePixelRatio(dpr)
    return pix


class RemoteView(QLabel):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#000000;")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        # 指到画面里就藏掉本地箭头：远端画面里已经画了它自己的指针，
        # 两个箭头错位看着很难受。
        self.setCursor(Qt.BlankCursor)

    def mousePressEvent(self, event):
        self.owner.mouse_press(event)

    def mouseReleaseEvent(self, event):
        self.owner.mouse_release(event)

    def mouseMoveEvent(self, event):
        self.owner.mouse_move(event)

    def wheelEvent(self, event):
        self.owner.mouse_wheel(event)

    def keyPressEvent(self, event):
        self.owner.key_press(event)

    def keyReleaseEvent(self, event):
        self.owner.key_release(event)

    def focusOutEvent(self, event):
        self.owner.release_all()
        super().focusOutEvent(event)


class RemoteWindow(QWidget):
    def __init__(self, client: ClientSession, device: dict, config, info: dict, main=None):
        super().__init__()
        self.main = main
        self.client = client
        self.device = device
        self.config = config
        self.canvas = QImage()
        self._last_view_at = 0.0
        self.scale = 1.0
        self.offset = (0, 0)
        self.zoom = "fit"
        self.pressed_keys = set()
        self.reconnect_attempts = 0
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._try_reconnect)
        self.setWindowTitle(f"WiFiDesk · {device.get('name', '远程桌面')}")
        self.setMinimumSize(640, 420)
        self.setAcceptDrops(True)
        try:
            remote_w = max(320, min(7680, int(info.get("w", 1280))))
            remote_h = max(240, min(4320, int(info.get("h", 720))))
        except (TypeError, ValueError):
            remote_w, remote_h = 1280, 720
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        # 窗口尽量开满可用区域：缩放比例越接近 1:1，远处画面就越清楚。
        # 以前固定封顶 1600x1000，1080p 被控端只能缩到 0.8 左右，文字明显发虚。
        toolbar_h, frame_h = 48, 32
        if avail is not None:
            max_w = max(640, avail.width() - 8)
            max_h = max(420, avail.height() - frame_h - 4)
        else:
            max_w, max_h = 1920, 1080
        factor = min(1.0, (max_w - 2) / remote_w, (max_h - toolbar_h) / remote_h)
        factor = max(0.4, factor)
        if factor >= 0.999:
            self.zoom = "one"
        win_w = max(900, int(round(remote_w * factor)) + 2)
        win_h = max(600, int(round(remote_h * factor)) + toolbar_h)
        self.resize(win_w, win_h)
        self._build()
        self.zoom_combo.setCurrentIndex(1 if self.zoom == "one" else 0)
        self.client.frame_ready.connect(self.set_frame)
        self.client.stats.connect(self.on_stats)
        self.client.file_progress.connect(self.on_file_progress)
        self.client.cursor_visible.connect(self.on_cursor_visible)
        self.client.connected.connect(self._on_reconnected)
        self.client.disconnected.connect(self._on_disconnected)
        self.show()
        self.set_frame()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.toolbar = QWidget()
        self.toolbar.setObjectName("toolbar")
        self.toolbar.setFixedHeight(48)
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(12, 6, 12, 6)
        bar.setSpacing(8)
        bar.addWidget(QLabel(self.device.get("name", "远程桌面")))
        self.mode_label = QLabel("模式：等待首帧")
        self.mode_label.setObjectName("subtext")
        bar.addWidget(self.mode_label)
        bar.addStretch(1)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(QUALITY_LABELS)
        preset_index = QUALITY_PRESETS.index(self.client.quality) if self.client.quality in QUALITY_PRESETS else 0
        self.quality_combo.setCurrentIndex(preset_index)
        self.quality_combo.currentIndexChanged.connect(
            lambda i: self.client.set_quality(QUALITY_PRESETS[i]))
        self.zoom_combo = QComboBox()
        self.zoom_combo.addItems(["适应窗口", "1:1"])
        self.zoom_combo.currentIndexChanged.connect(
            lambda i: setattr(self, "zoom", "fit" if i == 0 else "one") or self.update_view())
        quality_label = QLabel("画质")
        zoom_label = QLabel("缩放")
        stats_btn = QPushButton("状态")
        stats_btn.setCheckable(True)
        stats_btn.clicked.connect(self.toggle_stats)
        file_btn = QPushButton("发送文件")
        file_btn.setToolTip("也可以把文件直接拖进窗口")
        file_btn.clicked.connect(self.pick_files)
        self.transfer_label = QLabel("")
        self.transfer_label.setObjectName("subtext")
        self.transfer_label.hide()
        free_btn = QPushButton("释放键鼠")
        free_btn.clicked.connect(self.release_all)
        self.full_btn = QPushButton("全屏")
        self.full_btn.clicked.connect(self.toggle_fullscreen)
        close_btn = QPushButton("断开")
        close_btn.setObjectName("danger")
        close_btn.clicked.connect(self.close)
        for widget in (quality_label, self.quality_combo, zoom_label, self.zoom_combo,
                       stats_btn, file_btn, self.transfer_label, free_btn):
            bar.addWidget(widget)
        bar.addWidget(self.full_btn)
        bar.addWidget(close_btn)
        self.toolbar_extras = [quality_label, self.quality_combo, zoom_label, self.zoom_combo,
                               stats_btn, file_btn, self.transfer_label, free_btn]
        self.toolbar_timer = QTimer(self)
        self.toolbar_timer.setSingleShot(True)
        self.toolbar_timer.timeout.connect(self._hide_fullscreen_toolbar)
        root.addWidget(self.toolbar)

        self.view = RemoteView(self)
        root.addWidget(self.view, 1)
        self.status_overlay = QLabel("连接中断，正在重连…", self)
        self.status_overlay.setAlignment(Qt.AlignCenter)
        self.status_overlay.setStyleSheet(
            "background:rgba(10,12,16,200); color:#EDF2F8; border-radius:10px; padding:16px;"
        )
        self.status_overlay.hide()

        self.stats_panel = QFrame()
        self.stats_panel.setObjectName("card")
        self.stats_panel.setFixedHeight(64)
        self.stats_panel.hide()
        box = QHBoxLayout(self.stats_panel)
        self.stat_text = QLabel("FPS 0 · RTT 0 ms · 0 kbps")
        self.stat_text.setObjectName("mono")
        box.addWidget(self.stat_text)
        root.addWidget(self.stats_panel)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isFullScreen() and self.toolbar.isVisible():
            self.toolbar.resize(self.width(), 48)
            self.toolbar.move(0, 0)
        self._place_overlay()
        self.update_view()

    def _place_overlay(self):
        if self.status_overlay.isVisible():
            rect = self.view.geometry()
            width = min(420, max(240, rect.width() - 48))
            self.status_overlay.setGeometry(
                rect.x() + (rect.width() - width) // 2,
                rect.y() + (rect.height() - 84) // 2,
                width, 84,
            )

    def set_frame(self):
        if self.sender() not in (None, self.client):
            return
        img = self.client.take_frame()
        if img is None or img.isNull():
            return
        self.canvas = img
        self.update_view()

    def update_view(self):
        if self.canvas.isNull():
            return
        area = self.view.size()
        if area.width() < 8 or area.height() < 8:
            return
        dpr = max(1.0, self.view.devicePixelRatioF())
        logical = (self.canvas.size() if self.zoom == "one"
                   else self.canvas.size().scaled(area, Qt.KeepAspectRatio))
        now = time.perf_counter()
        interval = now - self._last_view_at
        self._last_view_at = now
        # 60 FPS 档省掉锐化：画面在动时流畅比边缘更重要，也避免占满 UI 线程。
        pix = _display_pixmap(self.canvas, logical, dpr, sharpen=interval > 0.022)
        self.view.setPixmap(pix)
        shown_w = pix.width() / dpr
        shown_h = pix.height() / dpr
        self.scale = shown_w / max(1, self.canvas.width())
        self.offset = ((area.width() - shown_w) / 2.0, (area.height() - shown_h) / 2.0)

    def native_pos(self, pos):
        if self.canvas.isNull():
            return None
        x = (pos.x() - self.offset[0]) / max(0.001, self.scale)
        y = (pos.y() - self.offset[1]) / max(0.001, self.scale)
        if x < 0 or y < 0 or x >= self.canvas.width() or y >= self.canvas.height():
            return None
        return x / self.canvas.width(), y / self.canvas.height()

    def mouse_press(self, event):
        self.view.setFocus()
        self._peek_toolbar()
        pos = self.native_pos(event.position().toPoint())
        if not pos:
            return
        nx, ny = pos
        self.client.send_input({"a": "move", "nx": nx, "ny": ny})
        button = {Qt.LeftButton: "left", Qt.RightButton: "right", Qt.MiddleButton: "middle"}.get(event.button())
        if button:
            self.client.send_input({"a": "down", "button": button})

    def mouse_release(self, event):
        button = {Qt.LeftButton: "left", Qt.RightButton: "right", Qt.MiddleButton: "middle"}.get(event.button())
        if button:
            self.client.send_input({"a": "up", "button": button})

    def mouse_move(self, event):
        self._peek_toolbar()
        pos = self.native_pos(event.position().toPoint())
        if pos:
            self.client.send_input({"a": "move", "nx": pos[0], "ny": pos[1]})

    def mouse_wheel(self, event):
        self._peek_toolbar()
        delta = event.angleDelta().y()
        if delta:
            self.client.send_input({"a": "wheel", "delta": 120 if delta > 0 else -120})

    def key_press(self, event):
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
            return
        if event.key() == Qt.Key_V and (event.modifiers() & Qt.ControlModifier):
            # 粘贴前把本机剪贴板文本推过去，被控端才能粘贴到本机复制的内容。
            self.client.push_clipboard()
        if event.key() in (Qt.Key_C, Qt.Key_X) and (event.modifiers() & Qt.ControlModifier):
            # 复制/剪切后把被控端的剪贴板取回来，本地也能粘贴。
            self.client.request_clipboard()
        vk = qt_key_to_vk(event.key(), event.text())
        if vk:
            self.pressed_keys.add(vk)
            self.client.send_input({"a": "key", "vk": vk, "down": True})

    def on_cursor_visible(self, visible: bool):
        # 远端自己把指针藏起来时（全屏视频、某些拖动场景），保留本地箭头，
        # 否则画面上一个指针都看不到。
        self.view.setCursor(Qt.BlankCursor if visible else Qt.ArrowCursor)

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送到被控端的文件")
        if paths:
            self.client.send_files(paths)

    def on_file_progress(self, info):
        name = str(info.get("name", ""))
        size = max(1, int(info.get("size", 1) or 1))
        sent = int(info.get("sent", 0) or 0)
        if sent >= size:
            self.transfer_label.setText(f"已发送 {name}")
            QTimer.singleShot(4000, self.transfer_label.hide)
        else:
            self.transfer_label.setText(f"发送 {name} {sent * 100 // size}%")
        self.transfer_label.show()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        files = [path for path in paths if os.path.isfile(path)]
        if files:
            event.acceptProposedAction()
            self.client.send_files(files)
        else:
            super().dropEvent(event)

    def key_release(self, event):
        vk = qt_key_to_vk(event.key(), event.text())
        if vk:
            self.pressed_keys.discard(vk)
            self.client.send_input({"a": "key", "vk": vk, "down": False})

    def release_all(self):
        self.pressed_keys.clear()
        self.client.send_input({"a": "reset"})

    def _peek_toolbar(self):
        if not self.isFullScreen():
            return
        self.toolbar.show()
        self.toolbar.raise_()
        self.toolbar.resize(self.width(), 48)
        self.toolbar.move(0, 0)
        self.toolbar_timer.start(2500)

    def _hide_fullscreen_toolbar(self):
        if self.isFullScreen():
            self.toolbar.hide()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.toolbar_timer.stop()
            self.showNormal()
            self.toolbar.setParent(None)
            self.layout().insertWidget(0, self.toolbar)
            self.toolbar.show()
            for widget in self.toolbar_extras:
                widget.show()
            self.full_btn.setText("全屏")
            return
        self.showFullScreen()
        self.stats_panel.hide()
        for widget in self.toolbar_extras:
            widget.hide()
        self.layout().removeWidget(self.toolbar)
        self.toolbar.setParent(self)
        self.toolbar.resize(self.width(), 48)
        self.toolbar.move(0, 0)
        self.toolbar.show()
        self.toolbar.raise_()
        self.full_btn.setText("退出全屏")
        self.toolbar_timer.start(1200)

    def toggle_stats(self, checked):
        self.stats_panel.setVisible(bool(checked))

    def on_stats(self, data: dict):
        qos_pct = int(float(data.get("qos", 1.0)) * 100)
        qf = int(data.get("qf", 0) or 0)
        codec = str(data.get("codec", "") or "")
        detail = f" · {codec}" if codec else ""
        remote_w, remote_h = int(data.get("w", 0) or 0), int(data.get("h", 0) or 0)
        stream_w, stream_h = int(data.get("sw", 0) or 0), int(data.get("sh", 0) or 0)
        # 服务端降了编码分辨率就直接标出来，否则用户只会觉得"莫名很糊"。
        scale_txt = ""
        if stream_w and remote_w and stream_w < remote_w:
            scale_txt = f"（画面 {stream_w}×{stream_h}）"
        self.mode_label.setText(
            f"模式：{data.get('mode', '-')}{detail} · {data.get('fps', 0)} FPS · "
            f"{remote_w}×{remote_h}{scale_txt} · 自适应 {qos_pct}%"
        )
        dec = float(data.get("dec", 0) or 0)
        dec_txt = f"   解码 {dec:.0f} ms" if dec else ""
        self.stat_text.setText(
            f"FPS {data.get('fps', 0)}   RTT {data.get('rtt', 0)} ms   "
            f"{data.get('kbps', 0)} kbps{dec_txt}   JPEG 质量 {qf or '-'}   "
            f"自适应 {qos_pct}%   {data.get('mode', '-')}{detail}"
        )

    def _on_reconnected(self, _info=None):
        if self.sender() is not self.client:
            return
        self.reconnect_timer.stop()
        self.reconnect_attempts = 0
        self.status_overlay.hide()

    def _on_disconnected(self, reason):
        if self.sender() is not self.client or not self.isVisible() or self.client.manual:
            return
        self.release_all()
        delay_ms = min(2000 * (2 ** min(self.reconnect_attempts, 4)), 30000)
        self.reconnect_attempts += 1
        self.status_overlay.setText(
            f"连接中断：{reason}\n{delay_ms // 1000} 秒后自动重连…")
        self.status_overlay.show()
        self.status_overlay.raise_()
        self._place_overlay()
        self.reconnect_timer.start(delay_ms)

    def bind_client(self, client):
        old = self.client
        for signal, slot in (
            (old.frame_ready, self.set_frame), (old.stats, self.on_stats),
            (old.connected, self._on_reconnected), (old.disconnected, self._on_disconnected),
            (old.file_progress, self.on_file_progress), (old.cursor_visible, self.on_cursor_visible),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        old.stop()
        self.client = client
        client.frame_ready.connect(self.set_frame)
        client.stats.connect(self.on_stats)
        client.connected.connect(self._on_reconnected)
        client.disconnected.connect(self._on_disconnected)
        client.file_progress.connect(self.on_file_progress)
        client.cursor_visible.connect(self.on_cursor_visible)

    def _try_reconnect(self):
        if not self.isVisible() or self.client.manual:
            return
        self.status_overlay.setText("正在自动重连…")
        if self.main is not None:
            self.main.reconnect_client(self.device, self.client)
        else:
            client = ClientSession(self.config, self.device, self.client.quality,
                                   resume_token=self.client.resume_token)
            self.bind_client(client)
            client.start()

    def closeEvent(self, event):
        self.reconnect_timer.stop()
        self.release_all()
        self.client.stop()
        super().closeEvent(event)


class MainWindow(QWidget):
    network_ready = Signal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.setWindowTitle("WiFiDesk")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint)
        self.setWindowOpacity(0.0)
        self._fade_anim = None
        self.resize(820, 540)
        self.setMinimumSize(720, 460)
        self.devices = {}
        self.remote_windows = []
        self.client = None
        self.connecting_id = None
        self.discovery = DiscoveryWorker(config)
        self.server = ServerSession(config)
        self._build()
        self._wire()
        self.apply_role()
        self.discovery.start()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        bar = DragBar(self)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 0, 12, 0)
        icon_label = QLabel()
        app_icon = QApplication.instance().windowIcon()
        if not app_icon.isNull():
            icon_label.setPixmap(app_icon.pixmap(26, 26))
        bar_layout.addWidget(icon_label)
        brand = QLabel("WiFiDesk")
        brand.setObjectName("brand")
        bar_layout.addWidget(brand)
        bar_layout.addStretch(1)
        self.net_label = QLabel("●  局域网检测中…")
        self.net_label.setObjectName("statusOk")
        self.network_ready.connect(lambda text: self.net_label.setText("●  " + text))
        threading.Thread(target=lambda: self.network_ready.emit(network_status()),
                         daemon=True, name="wd-network").start()
        bar_layout.addWidget(self.net_label)
        settings = QPushButton("⚙  设置")
        settings.setObjectName("windowButton")
        settings.setToolTip("角色、授权与性能设置")
        settings.clicked.connect(self.open_settings)
        bar_layout.addWidget(settings)
        minimize = QPushButton("─")
        minimize.setObjectName("windowButton")
        minimize.clicked.connect(self.showMinimized)
        bar_layout.addWidget(minimize)
        close = QPushButton("×")
        close.setObjectName("windowButtonClose")
        close.clicked.connect(self.close)
        bar_layout.addWidget(close)
        root.addWidget(bar)

        info = QFrame()
        info.setObjectName("card")
        info_layout = QHBoxLayout(info)
        info_layout.setContentsMargins(16, 12, 16, 12)
        info_layout.setSpacing(14)
        local = QVBoxLayout()
        local.setSpacing(4)
        name_row = QHBoxLayout()
        dot = QLabel("●")
        dot.setObjectName("statusOk")
        dot.setStyleSheet("font-size:12px;")
        name_row.addWidget(dot)
        name_label = QLabel(self.config.device_name)
        name_label.setStyleSheet("font-weight:600; font-size:16px;")
        name_row.addWidget(name_label)
        name_row.addStretch(1)
        local.addLayout(name_row)
        self.state_label = QLabel("未允许远控 · 仅可连接其他设备")
        self.state_label.setObjectName("subtext")
        local.addWidget(self.state_label)
        info_layout.addLayout(local, 1)
        role_label = QLabel("角色")
        role_label.setObjectName("subtext")
        info_layout.addWidget(role_label)
        self.role_combo = QComboBox()
        self.role_combo.addItems(["控制端", "被控端", "双向模式"])
        self.role_combo.setFixedWidth(108)
        info_layout.addWidget(self.role_combo)
        self.allow_button = QPushButton("允许连接：关")
        self.allow_button.setCheckable(True)
        self.allow_button.setFixedWidth(122)
        info_layout.addWidget(self.allow_button)
        root.addWidget(info)

        head = QHBoxLayout()
        title = QLabel("附近的设备")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        self.count_label = QLabel("0 台在线")
        self.count_label.setObjectName("subtext")
        head.addWidget(self.count_label)
        head.addStretch(1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.discovery.probe_now)
        head.addWidget(refresh)
        root.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_widget)
        root.addWidget(self.scroll, 1)

        foot = QHBoxLayout()
        diag = QPushButton("网络诊断")
        diag.setCheckable(True)
        diag.toggled.connect(self.toggle_diagnostics)
        foot.addWidget(diag)
        foot.addStretch(1)
        root.addLayout(foot)
        self.diag_text = QTextEdit()
        self.diag_text.setReadOnly(True)
        self.diag_text.setFixedHeight(110)
        self.diag_text.hide()
        root.addWidget(self.diag_text)

    def _wire(self):
        role_map = {"controller": 0, "controlled": 1, "both": 2}
        self.role_combo.setCurrentIndex(role_map.get(self.config.role, 0))
        self.allow_button.setChecked(bool(self.config.get("allow_connection")))
        self._update_allow_text()
        self.role_combo.currentIndexChanged.connect(self.on_role_changed)
        self.allow_button.toggled.connect(self.on_allow_toggled)
        self.discovery.device_found.connect(self.on_device_found)
        self.discovery.device_lost.connect(self.on_device_lost)
        self.discovery.log.connect(self.append_log)
        self.server.auth_required.connect(self.on_auth_required)
        self.server.session_started.connect(self.on_session_started)
        self.server.session_ended.connect(self.on_session_ended)
        self.server.stats.connect(self.on_server_stats)
        self.server.log.connect(self.append_log)

    def append_log(self, text):
        self.diag_text.append(str(text))

    def on_device_found(self, device):
        self.devices[device["id"]] = device
        self.refresh_devices()

    def on_device_lost(self, device_id):
        self.devices.pop(device_id, None)
        self.refresh_devices()

    def refresh_devices(self):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        known = self.config.get("known_devices", {})
        devices = sorted(self.devices.values(),
                         key=lambda d: (d["id"] not in known, d.get("name", "").lower()))
        can_control = self.config.may_control()
        for device in devices:
            row = DeviceRow(device, device["id"] in known, can_control)
            if self.connecting_id == device["id"]:
                row.set_connecting(True)
            row.connect_requested.connect(self.on_connect)
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        self.count_label.setText(f"{len(devices)} 台在线")
        if not devices:
            empty = QWidget()
            empty_layout = QVBoxLayout(empty)
            empty_layout.setAlignment(Qt.AlignCenter)
            empty_layout.setSpacing(8)
            title = QLabel("附近还没有设备")
            title.setStyleSheet("font-size:17px; font-weight:600;")
            title.setAlignment(Qt.AlignCenter)
            hint = QLabel("请确认两端在同一局域网，被控端已打开“允许连接”。\n如使用防火墙，请放行 UDP 47381。")
            hint.setObjectName("hint")
            hint.setAlignment(Qt.AlignCenter)
            hint.setWordWrap(True)
            empty_layout.addWidget(title)
            empty_layout.addWidget(hint)
            self.list_layout.insertWidget(0, empty)

    def on_connect(self, device, window=None, resume_token=None):
        if self.client:
            self.client.stop()
        self.connecting_id = device["id"]
        quality = window.client.quality if window else self.config.get("quality", "auto")
        token = resume_token if resume_token is not None else (
            window.client.resume_token if window else "")
        client = ClientSession(self.config, device, quality, resume_token=token)
        self.client = client
        client.connected.connect(self.on_connected)
        client.disconnected.connect(self.on_disconnected)
        client.state.connect(self.append_log)
        client.log.connect(self.append_log)
        if window:
            window.bind_client(client)
        client.start()
        self.append_log(f"正在连接 {device.get('name')} …")
        self.refresh_devices()

    def on_connected(self, info):
        if self.sender() is not self.client:
            return
        self.connecting_id = None
        self.refresh_devices()
        if any(w.client is self.client and w.isVisible() for w in self.remote_windows):
            return
        window = RemoteWindow(self.client, self.client.device, self.config, info, self)
        self.remote_windows.append(window)

    def reconnect_client(self, device, old_client):
        if self.client is not old_client:
            return
        latest = self.devices.get(device["id"])
        if latest and latest.get("fp") == device.get("fp"):
            device = latest
        for window in self.remote_windows:
            if window.client is old_client and window.isVisible():
                self.on_connect(device, window, resume_token=old_client.resume_token)
                return

    def on_disconnected(self, reason):
        if self.sender() is not self.client:
            return
        self.connecting_id = None
        self.append_log(f"连接断开：{reason}")
        self.refresh_devices()

    def on_auth_required(self, info):
        dialog = PairDialog(info, self)
        result = dialog.exec()
        self.server.resolve_auth(info["token"], result == QDialog.Accepted, dialog.auto_trusted)

    def on_session_started(self, info):
        self.state_label.setText(f"●  正在被 {info.get('name')} 控制")
        self.state_label.setStyleSheet("color:#F27682;")
        suffix = "（安全重连）" if info.get("resumed") else ""
        self.append_log(f"已授权 {info.get('name')} @ {info.get('ip')}，短码 {info.get('code')}{suffix}")

    def on_session_ended(self, reason):
        self.state_label.setText("未允许远控 · 仅可连接其他设备")
        self.state_label.setStyleSheet("")
        self.append_log(reason)

    def on_server_stats(self, data):
        codec = str(data.get("codec", "") or "")
        detail = f" · {codec}" if codec else ""
        enc = float(data.get("enc", 0) or 0)
        cap = float(data.get("cap", 0) or 0)
        scale = int(data.get("scale", 100) or 100)
        extra = ""
        if cap:
            extra += f" · 采集 {cap:.0f}ms"
        if enc:
            extra += f" · 编码 {enc:.0f}ms"
        if scale < 100:
            extra += f" · 编码 {scale}%"
        bitrate = int(data.get("bitrate", 0) or 0)
        if bitrate:
            extra += f" · 目标 {bitrate // 1_000_000}M"
        self.state_label.setText(
            f"正在被控 · {data.get('mode')}{detail} · {data.get('fps')} FPS · {data.get('kbps')} kbps"
            f"{extra} · 自适应 {int(float(data.get('qos', 1.0)) * 100)}%"
        )

    def on_role_changed(self, index):
        role = ["controller", "controlled", "both"][index]
        self.config.set("role", role)
        self.apply_role()

    def on_allow_toggled(self, checked):
        self.config.set("allow_connection", checked)
        self._update_allow_text()
        self.apply_role()

    def _update_allow_text(self):
        checked = self.allow_button.isChecked()
        self.allow_button.setText(f"允许连接：{'开' if checked else '关'}")
        self.allow_button.setObjectName("primary" if checked else "")
        self.allow_button.style().polish(self.allow_button)

    def apply_role(self):
        role_map = {"controller": 0, "controlled": 1, "both": 2}
        self.role_combo.blockSignals(True)
        self.role_combo.setCurrentIndex(role_map.get(self.config.role, 0))
        self.role_combo.blockSignals(False)
        self.allow_button.setEnabled(self.config.may_be_controlled())
        if self.config.may_be_controlled() and self.config.get("allow_connection"):
            self.server.start()
        else:
            self.server.stop()
            self.state_label.setText("未允许远控 · 仅可连接其他设备")
        self.refresh_devices()

    def open_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            wd_theme.apply_theme(QApplication.instance(), str(self.config.get("theme", "dark")))
            self.allow_button.setChecked(bool(self.config.get("allow_connection")))
            self._update_allow_text()
            self.apply_role()
            self.append_log("设置已保存")

    def toggle_diagnostics(self, checked):
        self.diag_text.setVisible(bool(checked))

    def showEvent(self, event):
        super().showEvent(event)
        if self._fade_anim is None:
            self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
            self._fade_anim.setDuration(160)
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()

    def closeEvent(self, event):
        if self.client:
            self.client.stop()
        for window in list(self.remote_windows):
            window.close()
        self.server.stop()
        self.discovery.stop()
        super().closeEvent(event)
