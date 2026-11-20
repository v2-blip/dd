"""V2 interface (Windows).

Replaces the previous Win32-drawn menu while keeping its settings contract.
The runtime continues to read lunar_menu_settings.json and hot-reloads changes.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from ctypes import wintypes

if sys.platform != "win32":
    raise SystemExit("V2 funciona somente no Windows.")

try:
    from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, Qt, QTimer, QPoint, QPointF
    from PySide6.QtGui import QColor, QFont, QPainter, QPen, QLinearGradient, QBrush, QPixmap, QIcon
    from PySide6.QtWidgets import (
        QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFrame,
        QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton, QSlider,
        QStackedWidget, QVBoxLayout, QWidget, QScrollArea, QSizePolicy,
    )
except ImportError as exc:
    raise SystemExit(
        "PySide6 não está instalado. Execute install_gui.bat ou: "
        "py -3.13 -m pip install PySide6"
    ) from exc

USER32 = ctypes.WinDLL("user32", use_last_error=True)
USER32.GetAsyncKeyState.argtypes = [ctypes.c_int]
USER32.GetAsyncKeyState.restype = ctypes.c_short
USER32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
USER32.GetCursorPos.restype = wintypes.BOOL
USER32.GetForegroundWindow.restype = wintypes.HWND
USER32.SetForegroundWindow.argtypes = [wintypes.HWND]
USER32.SetForegroundWindow.restype = wintypes.BOOL
USER32.IsWindowVisible.argtypes = [wintypes.HWND]
USER32.IsWindowVisible.restype = wintypes.BOOL

VK_ESCAPE = 0x1B
VK_INSERT = 0x2D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06
MOUSE_VKS = (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2)
RESERVED_MENU_KEYS = (0x23, 0x2E)  # END and DELETE

ROOT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT_DIR / "lunar_menu_settings.json"
LEGACY_SETTINGS_FILE = Path(__file__).resolve().parent / "lunar_menu_settings.json"
FRIEND_DIR = ROOT_DIR / "friends"
FRIEND_PREVIEW = FRIEND_DIR / "friend_preview.png"
FRIEND_REFERENCES = FRIEND_DIR / "friend_references.json"

# Visual identity adapted from the supplied PySide6 GUI base.
ACCENT = "#ff4d6d"
ACCENT_2 = "#ff8a5c"
BG = "#0d0e13"
SIDEBAR = "#11121a"
CARD = "#171923"
CARD_HOVER = "#1d2030"
BORDER = "#252838"
TEXT = "#eceef6"
MUTED = "#7d8199"
GREEN = "#4ade80"

QSS = f"""
* {{ font-family: 'Segoe UI', 'Inter', sans-serif; color: {TEXT}; outline: none; }}
QWidget#root {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 18px; }}
QWidget#sidebar {{ background: {SIDEBAR}; border-top-left-radius: 18px; border-bottom-left-radius: 18px; border-right: 1px solid {BORDER}; }}
QLabel#logo {{ font-size: 21px; font-weight: 800; letter-spacing: 3px; color: {TEXT}; }}
QLabel#version {{ color: {MUTED}; font-size: 11px; }}
QLabel#pageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#pageSub {{ color: {MUTED}; font-size: 12px; }}
QLabel#cardTitle {{ color: {ACCENT}; font-size: 11px; font-weight: 800; letter-spacing: 2px; }}
QLabel#rowTitle {{ font-size: 13px; font-weight: 600; }}
QLabel#rowSub {{ color: {MUTED}; font-size: 11px; }}
QLabel#valueBadge {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 8px; padding: 3px 9px; color: {TEXT}; font-weight: 700; min-width: 44px; }}
QLabel#status {{ color: {GREEN}; font-size: 11px; }}
QLabel#hint {{ color: {MUTED}; font-size: 11px; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#divider {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QPushButton#navBtn {{ background: transparent; border: none; border-radius: 11px; text-align: left; padding: 11px 14px; font-size: 13px; font-weight: 600; color: {MUTED}; }}
QPushButton#navBtn:hover {{ background: {CARD}; color: {TEXT}; }}
QPushButton#navBtn:checked {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 rgba(255,77,109,70), stop:1 rgba(255,77,109,8)); color: {TEXT}; border-left: 3px solid {ACCENT}; border-radius: 4px; }}
QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2}); border: none; border-radius: 10px; padding: 10px 20px; font-weight: 700; color: white; }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff6783, stop:1 #ffa079); }}
QPushButton#ghost {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; padding: 9px 15px; font-weight: 600; color: {TEXT}; }}
QPushButton#ghost:hover {{ border: 1px solid {ACCENT}; background: {CARD_HOVER}; }}
QPushButton#choice {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 9px; padding: 8px 16px; font-weight: 700; }}
QPushButton#choice:checked {{ background: rgba(255,77,109,35); border: 1px solid {ACCENT}; color: white; }}
QPushButton#winBtn, QPushButton#closeBtn {{ background: transparent; border: none; border-radius: 8px; font-size: 14px; color: {MUTED}; min-width: 30px; max-width: 30px; min-height: 28px; max-height: 28px; }}
QPushButton#winBtn:hover {{ background: {CARD_HOVER}; color: {TEXT}; }}
QPushButton#closeBtn:hover {{ background: {ACCENT}; color: white; }}
QPushButton#bind {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 9px; padding: 8px 14px; min-width: 130px; font-weight: 700; }}
QPushButton#bind:hover {{ border: 1px solid {ACCENT}; }}
QPushButton#bind[capturing="true"] {{ border: 1px solid {ACCENT}; background: rgba(255,77,109,35); }}
QComboBox {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 10px; min-width: 130px; font-weight: 600; }}
QComboBox:hover {{ border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; selection-color: white; padding: 4px; }}
QSlider::groove:horizontal {{ height: 6px; background: {BG}; border: 1px solid {BORDER}; border-radius: 4px; }}
QSlider::sub-page:horizontal {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2}); border-radius: 4px; }}
QSlider::handle:horizontal {{ background: white; width: 16px; height: 16px; margin: -6px 0; border-radius: 9px; border: 3px solid {ACCENT}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{ color: {TEXT}; background: {CARD}; border: 1px solid {BORDER}; padding: 5px; }}
"""


def _default_config() -> dict:
    return {
        "toggle_binding": {"type": "keyboard", "vk": VK_INSERT},
        "activation_binding": {"type": "mouse", "vk": VK_MBUTTON},
        "friend_capture_binding": {"type": "keyboard", "vk": 0x2E},
        "friend_filter_enabled": True,
        "aim_enabled": False,
        "aim_target_mode": "all",
        "trigger_enabled": False,
        "trigger_box_left": 50,
        "trigger_box_right": 50,
        "trigger_box_top": 50,
        "trigger_box_bottom": 50,
        "trigger_box_size": 100,
        "trigger_delay_ms": 50,
        "color_on": "#35D49A",
        "color_off": "#E45D70",
        "preview_on": False,
        "dot_corner": "bottom_right",
        "dot_size": 12,
        "aim_fov_size": 100,
        # Legacy acquisition flag remains enabled; visibility has its own key.
        "aim_fov_enabled": True,
        "aim_fov_visible": True,
        "aim_fov_thickness": 3,
        "aim_fov_color": "#419DFF",
        "aim_fov_offset_x": 0,
        "aim_fov_offset_y": 0,
        "aim_hitbox_width": 100,  # legacy migration field
        "aim_hitbox_height": 100,  # legacy migration field
        "aim_smooth": 0.75,  # legacy alias, normalized
        "aim_speed": 0.75,
        "aim_strength": 0.75,
        "aim_control_scale_version": 1,
        "confidence": 0.45,
        "aim_method": "old",
        "aim_hold_lmb": False,
        "detector_model": "universal",
        "roblox_aim_preset": "Head",
        "roblox_aim_u": 0.50,
        "roblox_aim_v": 0.09,
        "roblox_aim_presets": {
            "Head": {"u": 0.50, "v": 0.09},
            "Upper": {"u": 0.50, "v": 0.27},
            "Center": {"u": 0.50, "v": 0.48},
            "Custom": {"u": 0.50, "v": 0.13},
        },
    }


def _normalize_binding(value, fallback):
    if isinstance(value, dict):
        kind = str(value.get("type", "keyboard")).lower()
        try:
            vk = int(value.get("vk", fallback.get("vk", VK_INSERT)))
        except (TypeError, ValueError):
            return dict(fallback)
        if kind == "mouse" and vk in MOUSE_VKS:
            return {"type": "mouse", "vk": vk}
        if kind == "keyboard" and 1 <= vk <= 0xFE:
            return {"type": "keyboard", "vk": vk}
    return dict(fallback)


def _key_name(vk: int) -> str:
    fixed = {
        0x08: "BACKSPACE", 0x09: "TAB", 0x0D: "ENTER", 0x1B: "ESC",
        0x20: "SPACE", 0x21: "PAGE UP", 0x22: "PAGE DOWN", 0x23: "END",
        0x24: "HOME", 0x25: "LEFT", 0x26: "UP", 0x27: "RIGHT",
        0x28: "DOWN", 0x2D: "INSERT", 0x2E: "DELETE",
        0x6A: "NUM *", 0x6B: "NUM +", 0x6D: "NUM -", 0x6E: "NUM .", 0x6F: "NUM /",
    }
    if vk in fixed:
        return fixed[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return f"NUM {vk - 0x60}"
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return f"VK {vk:02X}"


def _binding_name(binding) -> str:
    vk = int(binding.get("vk", VK_INSERT))
    if binding.get("type") == "mouse":
        return {
            VK_LBUTTON: "MOUSE 1", VK_RBUTTON: "MOUSE 2", VK_MBUTTON: "MOUSE 3",
            VK_XBUTTON1: "MOUSE 4", VK_XBUTTON2: "MOUSE 5",
        }.get(vk, "MOUSE")
    return _key_name(vk)


class Toggle(QCheckBox):
    """Compact animated toggle matching the supplied GUI design."""
    def __init__(self, checked=False):
        super().__init__()
        self.setFixedSize(48, 26)
        self.setCursor(Qt.PointingHandCursor)
        self._pos = 1.0 if checked else 0.0
        self.setChecked(bool(checked))
        self._anim = QPropertyAnimation(self, b"pos_", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def get_pos(self):
        return self._pos

    def set_pos(self, value):
        self._pos = float(value)
        self.update()

    pos_ = Property(float, get_pos, set_pos)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(BORDER))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        if self._pos > 0:
            grad = QLinearGradient(rect.topLeft(), rect.topRight())
            grad.setColorAt(0, QColor(ACCENT))
            grad.setColorAt(1, QColor(ACCENT_2))
            painter.setOpacity(self._pos)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            painter.setOpacity(1.0)
        diameter = rect.height() - 6
        x = rect.left() + 3 + (rect.width() - diameter - 6) * self._pos
        painter.setBrush(QColor("white"))
        painter.drawEllipse(QRectF(x, rect.top() + 3, diameter, diameter))


class Card(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("card")
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(18, 15, 18, 14)
        self.layout_box.setSpacing(8)
        label = QLabel(title.upper())
        label.setObjectName("cardTitle")
        self.layout_box.addWidget(label)
        self._row_count = 0
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 65))
        self.setGraphicsEffect(shadow)

    def add_row(self, widget):
        if self._row_count:
            divider = QFrame()
            divider.setObjectName("divider")
            self.layout_box.addWidget(divider)
        self.layout_box.addWidget(widget)
        self._row_count += 1
        return widget


class Page(QScrollArea):
    def __init__(self, title: str, subtitle: str):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("pageInner")
        inner.setStyleSheet("QWidget#pageInner{background:transparent;}")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(14)
        h = QLabel(title)
        h.setObjectName("pageTitle")
        layout.addWidget(h)
        # Keep page headers clean: only the page name is shown.
        layout.addSpacing(2)
        self.setWidget(inner)
        self.page_layout = layout

    def add_card(self, card: Card):
        self.page_layout.addWidget(card)
        return card

    def finish(self):
        self.page_layout.addStretch(1)


def _row(title: str, subtitle: str | None, control: QWidget):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 8, 0, 8)
    layout.setSpacing(16)
    text_box = QVBoxLayout()
    text_box.setSpacing(2)
    name = QLabel(title)
    name.setObjectName("rowTitle")
    text_box.addWidget(name)
    # Descriptive helper text is intentionally omitted; controls keep their names.
    layout.addLayout(text_box, 1)
    layout.addWidget(control, 0, Qt.AlignVCenter)
    return widget


def _slider(minimum, maximum, value, width=230, suffix=""):
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    slider = QSlider(Qt.Horizontal)
    slider.setRange(int(minimum), int(maximum))
    slider.setValue(max(int(minimum), min(int(maximum), int(round(value)))))
    slider.setFixedWidth(width)
    badge = QLabel(f"{slider.value()}{suffix}")
    badge.setObjectName("valueBadge")
    badge.setAlignment(Qt.AlignCenter)
    slider.valueChanged.connect(lambda v: badge.setText(f"{v}{suffix}"))
    layout.addWidget(slider)
    layout.addWidget(badge)
    return holder, slider


def _normalized_control_slider(value, width=250):
    """Slider with 0.01 increments, displayed and stored as 0.10..1.00."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    slider = QSlider(Qt.Horizontal)
    slider.setRange(10, 100)
    slider.setValue(max(10, min(100, int(round(float(value) * 100.0)))))
    slider.setFixedWidth(width)
    badge = QLabel(f"{slider.value() / 100.0:.2f}")
    badge.setObjectName("valueBadge")
    badge.setAlignment(Qt.AlignCenter)
    slider.valueChanged.connect(lambda v: badge.setText(f"{v / 100.0:.2f}"))
    layout.addWidget(slider)
    layout.addWidget(badge)
    return holder, slider


def _legacy_control_value(value, legacy_max):
    """Convert old percent/large integer settings to the normalized 0.10..1.00 scale."""
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.75
    if 0.10 <= raw <= 1.00:
        normalized = raw
    elif 0.0 <= raw <= 100.0:
        normalized = raw / 100.0
    elif raw > 100.0:
        normalized = raw / float(legacy_max)
    else:
        normalized = 0.10
    return round(max(0.10, min(1.00, normalized)), 2)


class TargetPreview(QFrame):
    """Preview of the detector crop, calibrated reticle and independent FOV center."""
    BASE_CAPTURE_SIZE = 350

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(172)
        self.setMaximumHeight(190)
        self.setStyleSheet(f"QFrame{{background:{BG};border:1px solid {BORDER};border-radius:12px;}}")
        self.fov = 100
        self.capture_width = self.BASE_CAPTURE_SIZE
        self.capture_height = self.BASE_CAPTURE_SIZE
        self.crosshair_x = (self.capture_width - 1) / 2.0
        self.crosshair_y = (self.capture_height - 1) / 2.0
        self.offset_x = 0
        self.offset_y = 0
        self.thickness = 3
        self.color = "#419DFF"
        self.enabled = True

    def set_geometry(self, fov, capture_width, capture_height, crosshair_x, crosshair_y,
                     offset_x=0, offset_y=0, thickness=3, color="#419DFF", enabled=True):
        self.fov = max(0, min(700, int(fov)))
        self.capture_width = max(1, int(capture_width))
        self.capture_height = max(1, int(capture_height))
        self.crosshair_x = max(0.0, min(self.capture_width - 1.0, float(crosshair_x)))
        self.crosshair_y = max(0.0, min(self.capture_height - 1.0, float(crosshair_y)))
        self.enabled = bool(enabled)
        # Preserve center coordinates even when the circle is visually hidden.
        self.offset_x = int(offset_x)
        self.offset_y = int(offset_y)
        self.thickness = max(1, min(12, int(thickness)))
        self.color = str(color)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pad = 12.0
        available_w = max(1.0, self.width() - pad * 2)
        available_h = max(1.0, self.height() - pad * 2)
        scale = min(
            available_w / float(max(1, self.capture_width)),
            available_h / float(max(1, self.capture_height)),
        )
        display_w = self.capture_width * scale
        display_h = self.capture_height * scale
        left = (self.width() - display_w) / 2.0
        top = (self.height() - display_h) / 2.0
        crop = QRectF(left, top, display_w, display_h)

        painter.setPen(QPen(QColor(BORDER), 1.0))
        painter.setBrush(QColor("#101118"))
        painter.drawRect(crop)

        # Keep the true calibrated reticle visible separately from the movable FOV center.
        reticle_x = left + self.crosshair_x * scale
        reticle_y = top + self.crosshair_y * scale
        fov_x_px = max(0.0, min(self.capture_width - 1.0, self.crosshair_x + self.offset_x))
        fov_y_px = max(0.0, min(self.capture_height - 1.0, self.crosshair_y + self.offset_y))
        fov_cx = left + fov_x_px * scale
        fov_cy = top + fov_y_px * scale
        radius = max(0.0, self.fov * 0.5) * scale

        if radius > 0 and self.enabled:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0), max(0.8, (self.thickness + 4.0) * scale)))
            painter.drawEllipse(QRectF(fov_cx - radius, fov_cy - radius, radius * 2, radius * 2))
            painter.setPen(QPen(QColor(self.color), max(0.6, self.thickness * scale)))
            painter.drawEllipse(QRectF(fov_cx - radius, fov_cy - radius, radius * 2, radius * 2))
            # Small center marker indicates the actual FOV acquisition center.
            mark = max(1.0, 2.0 * scale)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(self.color))
            painter.drawEllipse(QRectF(fov_cx - mark, fov_cy - mark, mark * 2, mark * 2))

        # White cross + yellow center identify the calibrated mouse reticle, not the FOV offset.
        arm = 10.0 * scale
        painter.setPen(QPen(QColor(255, 255, 255), max(0.6, 2.0 * scale)))
        painter.drawLine(QPointF(reticle_x - arm, reticle_y), QPointF(reticle_x + arm, reticle_y))
        painter.drawLine(QPointF(reticle_x, reticle_y - arm), QPointF(reticle_x, reticle_y + arm))
        dot_r = max(1.0, 3.0 * scale)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 0))
        painter.drawEllipse(QRectF(reticle_x - dot_r, reticle_y - dot_r, dot_r * 2, dot_r * 2))


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.drag_offset = None
        self.setFixedHeight(48)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 9, 14, 0)
        brand = QLabel("V2")
        brand.setStyleSheet(f"font-weight:800;letter-spacing:1.5px;color:{MUTED};font-size:10px;")
        layout.addWidget(brand)
        layout.addStretch(1)
        minimize = QPushButton("—")
        minimize.setObjectName("winBtn")
        minimize.clicked.connect(window._hide_menu)
        close = QPushButton("✕")
        close.setObjectName("closeBtn")
        close.clicked.connect(window.close)
        layout.addWidget(minimize)
        layout.addWidget(close)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window.move(event.globalPosition().toPoint() - self.drag_offset)

    def mouseReleaseEvent(self, event):
        self.drag_offset = None


class LunarWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("V2")
        self.setWindowIcon(QIcon(str(ROOT_DIR / "lunar_tray_green.ico")))
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(1020, 760)
        self._config = self._load_config()
        self._capturing_target = None
        self._previous_keys = {key: bool(USER32.GetAsyncKeyState(key) & 0x8000) for key in MOUSE_VKS}
        self._previous_menu_down = False
        self._previous_foreground = None
        self._closing = False
        self._friend_preview_mtime = None
        self._friend_ref_count = 0
        self._initialize_state()
        self._build_ui()
        self._populate_from_state()
        self._connect_auto_save()
        self._save_settings()  # persist one-time migration to the original OLD controller

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(8)
        self.poll_timer.timeout.connect(self._poll_global_hotkeys)
        self.poll_timer.start()

        self.friend_timer = QTimer(self)
        self.friend_timer.setInterval(1200)
        self.friend_timer.timeout.connect(self._refresh_friend_preview)
        self.friend_timer.start()
        self._refresh_friend_preview(force=True)

        self.fov_preview_timer = QTimer(self)
        self.fov_preview_timer.setInterval(350)
        self.fov_preview_timer.timeout.connect(self._refresh_fov_preview)
        self.fov_preview_timer.start()
        self._refresh_fov_preview()

        print(f"[MENU] V2 pronto. Atalho: {_binding_name(self.menu_binding)}", flush=True)
        print("[MENU] Método de Aim Assist inicial: OLD (método anterior).", flush=True)

    @staticmethod
    def _load_config():
        defaults = _default_config()
        config_path = SETTINGS_FILE
        # Older menu builds placed the settings beside lunar_menu.py in lib/,
        # while the runtime has always read the project-root settings file.
        # Read that legacy file only when the canonical root file is absent.
        if not config_path.exists() and LEGACY_SETTINGS_FILE.exists():
            config_path = LEGACY_SETTINGS_FILE
        try:
            if config_path.exists():
                with config_path.open("r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
                if isinstance(loaded, dict):
                    # Normalize older 0..900 / percentage controls before the menu
                    # or runtime can interpret them using incompatible units.
                    try:
                        scale_version = int(loaded.get("aim_control_scale_version", 0))
                    except (TypeError, ValueError):
                        scale_version = 0
                    raw_speed = loaded.get("aim_speed", loaded.get("aim_smooth", None))
                    raw_force = loaded.get("aim_strength", None)
                    try:
                        speed_numeric = float(raw_speed) if raw_speed is not None else None
                    except (TypeError, ValueError):
                        speed_numeric = None
                    try:
                        force_numeric = float(raw_force) if raw_force is not None else None
                    except (TypeError, ValueError):
                        force_numeric = None
                    needs_speed_migration = speed_numeric is not None and (scale_version < 1 or not (0.10 <= speed_numeric <= 1.00))
                    needs_force_migration = force_numeric is not None and (scale_version < 1 or not (0.10 <= force_numeric <= 1.00))
                    if needs_speed_migration:
                        loaded["aim_speed"] = _legacy_control_value(raw_speed, 900.0)
                    elif speed_numeric is None:
                        loaded["aim_speed"] = 0.75
                    else:
                        loaded["aim_speed"] = round(max(0.10, min(1.00, speed_numeric)), 2)
                    if needs_force_migration:
                        loaded["aim_strength"] = _legacy_control_value(raw_force, 200.0)
                    elif force_numeric is None:
                        loaded["aim_strength"] = 0.75
                    else:
                        loaded["aim_strength"] = round(max(0.10, min(1.00, force_numeric)), 2)
                    loaded["aim_smooth"] = loaded["aim_speed"]
                    loaded["aim_control_scale_version"] = 1
                    defaults.update(loaded)
                    if "confidence" not in loaded and "detection_confidence" in loaded:
                        defaults["confidence"] = loaded.get("detection_confidence", 0.45)
                    # Persist migrated controls to the canonical project-root file,
                    # retaining all unrelated settings and preserving the source.
                    if scale_version < 1 or config_path == LEGACY_SETTINGS_FILE:
                        try:
                            temp = SETTINGS_FILE.with_suffix(".json.tmp")
                            with temp.open("w", encoding="utf-8") as output:
                                json.dump(loaded, output, indent=2, ensure_ascii=False)
                            os.replace(temp, SETTINGS_FILE)
                            print(f"[MENU] Configurações migradas para {SETTINGS_FILE}.", flush=True)
                        except OSError as migrate_exc:
                            print(f"[WARN] Não foi possível migrar as configurações antigas: {migrate_exc}", flush=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"[WARN] Configuração não pôde ser lida; usando padrões: {exc}", flush=True)
        return defaults

    def _initialize_state(self):
        d = self._config
        self.menu_binding = _normalize_binding(d.get("toggle_binding"), {"type": "keyboard", "vk": int(d.get("toggle_vk", VK_INSERT))})
        self.feature_binding = _normalize_binding(d.get("activation_binding"), {"type": "mouse", "vk": int(d.get("activation_vk", VK_MBUTTON))})
        self.friend_binding = _normalize_binding(d.get("friend_capture_binding"), {"type": "keyboard", "vk": 0x2E})
        for binding in (self.menu_binding, self.feature_binding):
            if binding.get("type") == "keyboard" and int(binding.get("vk", 0)) in RESERVED_MENU_KEYS:
                binding.update({"type": "keyboard", "vk": VK_INSERT})
        if self.friend_binding.get("type") == "keyboard" and int(self.friend_binding.get("vk", 0)) == 0x23:
            self.friend_binding = {"type": "keyboard", "vk": 0x2E}

        self.aim_enabled = bool(d.get("aim_enabled", False))
        self.aim_target_mode = str(d.get("aim_target_mode", "all")).strip().lower()
        if self.aim_target_mode not in ("all", "head"):
            self.aim_target_mode = "all"
        self.trigger_enabled = bool(d.get("trigger_enabled", False))
        self.friend_filter_enabled = bool(d.get("friend_filter_enabled", True))
        self.trigger_edges = {
            edge: max(0, min(200, int(d.get(f"trigger_box_{edge}", 50))))
            for edge in ("left", "right", "top", "bottom")
        }
        self.trigger_delay = max(0, min(1000, int(d.get("trigger_delay_ms", 50))))
        self.dot_color_on = self._valid_color(d.get("color_on"), "#35D49A")
        self.dot_color_off = self._valid_color(d.get("color_off"), "#E45D70")
        self.preview_on = bool(d.get("preview_on", False))
        self.dot_corner = d.get("dot_corner", "bottom_right")
        if self.dot_corner not in ("top_left", "top_right", "bottom_left", "bottom_right"):
            self.dot_corner = "bottom_right"
        self.dot_size = max(0, min(100, int(d.get("dot_size", 12))))
        self.aim_fov = max(0, min(700, int(d.get("aim_fov_size", 100))))
        # Migrate the former combined toggle to visual visibility once.
        self.aim_fov_visible = bool(d.get("aim_fov_visible", d.get("aim_fov_enabled", True)))
        self.aim_fov_enabled = True  # legacy alias; acquisition is always active
        self.aim_fov_thickness = max(1, min(12, int(d.get("aim_fov_thickness", 3))))
        self.aim_fov_color = self._valid_color(d.get("aim_fov_color"), "#419DFF")
        self.aim_fov_offset_x = max(-500, min(500, int(d.get("aim_fov_offset_x", 0))))
        self.aim_fov_offset_y = max(-300, min(300, int(d.get("aim_fov_offset_y", 0))))
        legacy_hitbox_width = max(25, min(200, int(d.get("aim_hitbox_width", 100))))
        legacy_hitbox_height = max(25, min(200, int(d.get("aim_hitbox_height", 100))))
        # Preserve previous symmetric Width/Height customizations on upgrade.
        self.hitbox_edge_offsets = {
            "left": max(-100, min(100, int(d.get("aim_hitbox_left_offset", round((100 - legacy_hitbox_width) / 2))))),
            "right": max(-100, min(100, int(d.get("aim_hitbox_right_offset", round((legacy_hitbox_width - 100) / 2))))),
            "top": max(-100, min(100, int(d.get("aim_hitbox_top_offset", round((100 - legacy_hitbox_height) / 2))))),
            "bottom": max(-100, min(100, int(d.get("aim_hitbox_bottom_offset", round((legacy_hitbox_height - 100) / 2))))),
        }
        self.aim_speed = round(max(0.10, min(1.00, float(d.get("aim_speed", d.get("aim_smooth", 0.75))))), 2)
        self.aim_strength = round(max(0.10, min(1.00, float(d.get("aim_strength", 0.75)))), 2)
        self.aim_confidence = max(0.10, min(0.99, float(d.get("confidence", d.get("detection_confidence", 0.45)))))
        self.aim_smooth = self.aim_speed  # normalized legacy alias

        # The user's explicit request is to restore the previous/OLD controller.
        # Convert the prior NEW default once, then respect any later explicit UI choice.
        if not bool(d.get("old_aim_method_migrated", False)):
            self.aim_method = "old"
            d["old_aim_method_migrated"] = True
        else:
            method = str(d.get("aim_method", "old")).lower()
            self.aim_method = method if method in ("old", "new") else "old"
        self.aim_hold_lmb = bool(d.get("aim_hold_lmb", False))

        self.detector_model = str(d.get("detector_model", "universal")).lower()
        if self.detector_model not in ("universal", "roblox"):
            self.detector_model = "universal"
        self.preset_names = ("Head", "Upper", "Center", "Custom")
        defaults = _default_config()["roblox_aim_presets"]
        self.roblox_presets = dict(defaults)
        saved = d.get("roblox_aim_presets", {})
        if isinstance(saved, dict):
            for name in self.preset_names:
                item = saved.get(name)
                if isinstance(item, dict):
                    try:
                        self.roblox_presets[name] = {
                            "u": max(0.0, min(1.0, float(item.get("u", defaults[name]["u"])))),
                            "v": max(0.0, min(1.0, float(item.get("v", defaults[name]["v"])))),
                        }
                    except (ValueError, TypeError):
                        pass
        self.active_preset = str(d.get("roblox_aim_preset", "Head"))
        if self.active_preset not in self.preset_names:
            self.active_preset = "Head"
        current = self.roblox_presets[self.active_preset]
        self.roblox_u = max(0.0, min(1.0, float(d.get("roblox_aim_u", current["u"]))))
        self.roblox_v = max(0.0, min(1.0, float(d.get("roblox_aim_v", current["v"]))))
        if self.active_preset == "Head" and "roblox_aim_u" not in d and "roblox_aim_v" not in d:
            self.roblox_u, self.roblox_v = current["u"], current["v"]

    @staticmethod
    def _valid_color(value, fallback):
        if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
            try:
                int(value[1:], 16)
                return value.upper()
            except ValueError:
                pass
        return fallback

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 22, 22, 22)
        root = QWidget()
        root.setObjectName("root")
        outer.addWidget(root)
        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(45)
        glow.setOffset(0, 7)
        glow.setColor(QColor(255, 77, 109, 55))
        root.setGraphicsEffect(glow)
        body = QHBoxLayout(root)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 24, 16, 18)
        side.setSpacing(6)
        logo = QLabel("V2")
        logo.setObjectName("logo")
        side.addWidget(logo)
        side.addSpacing(20)

        self.stack = QStackedWidget()
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        pages = [
            ("◈   Main", self._page_main()),
            ("◎   Aim Assist", self._page_aim()),
            ("⌖   Trigger Bot", self._page_trigger()),
            ("♙   Friend", self._page_friend()),
        ]
        for index, (title, page) in enumerate(pages):
            btn = QPushButton(title)
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            self.nav_group.addButton(btn, index)
            side.addWidget(btn)
            self.stack.addWidget(page)
            if index == 0:
                btn.setChecked(True)
        self.nav_group.idClicked.connect(self._switch_page)
        side.addStretch(1)
        self.status = QLabel("● Configurações carregadas")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.hide()
        body.addWidget(sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(TitleBar(self))
        content = QVBoxLayout()
        content.setContentsMargins(24, 0, 18, 18)
        content.addWidget(self.stack, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        reset = QPushButton("Resetar")
        reset.setObjectName("ghost")
        reset.clicked.connect(self._reset_defaults)
        apply_button = QPushButton("Aplicar alterações")
        apply_button.setObjectName("primary")
        apply_button.clicked.connect(self._apply)
        reset.setText("Reset")
        apply_button.setText("Save")
        footer.addWidget(reset)
        footer.addSpacing(8)
        footer.addWidget(apply_button)
        content.addLayout(footer)
        right.addLayout(content, 1)
        body.addLayout(right, 1)

    def _page_main(self):
        page = Page("Main", "")
        keys = page.add_card(Card("Atalhos"))
        self.menu_key_button = self._bind_button("menu")
        self.feature_key_button = self._bind_button("activation")
        keys.add_row(_row("Menu", None, self.menu_key_button))
        keys.add_row(_row("Feature", None, self.feature_key_button))

        model = page.add_card(Card("Model"))
        self.model_combo = QComboBox()
        self.model_combo.addItems(["Universal", "Roblox"])
        model.add_row(_row("Detection model", None, self.model_combo))
        self.model_status = QLabel("")
        self.model_status.hide()

        preset_card = page.add_card(Card("Roblox"))
        self.preset_card = preset_card
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(self.preset_names))
        preset_card.add_row(_row("Preset", None, self.preset_combo))
        u_holder, self.roblox_x_slider = _slider(0, 100, self.roblox_u * 100, 220, "%")
        v_holder, self.roblox_y_slider = _slider(0, 100, self.roblox_v * 100, 220, "%")
        preset_card.add_row(_row("X SIDE", None, u_holder))
        preset_card.add_row(_row("Y HEIGHT", None, v_holder))
        save_preset = QPushButton("Save preset")
        save_preset.setObjectName("primary")
        save_preset.clicked.connect(self._save_active_preset)
        preset_card.layout_box.addWidget(save_preset, 0, Qt.AlignLeft)
        self.preset_hint = QLabel("")
        self.preset_hint.hide()

        visual = page.add_card(Card("Indicator"))
        self.color_on_button = self._color_button("ON")
        self.color_off_button = self._color_button("OFF")
        visual.add_row(_row("ON", None, self.color_on_button))
        visual.add_row(_row("OFF", None, self.color_off_button))
        preview_box = QWidget()
        preview_layout = QHBoxLayout(preview_box)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_on_button = QPushButton("Preview ON")
        self.preview_off_button = QPushButton("Preview OFF")
        for button in (self.preview_on_button, self.preview_off_button):
            button.setObjectName("choice")
            button.setCheckable(True)
            preview_layout.addWidget(button)
        preview_group = QButtonGroup(self)
        preview_group.setExclusive(True)
        preview_group.addButton(self.preview_on_button)
        preview_group.addButton(self.preview_off_button)
        visual.add_row(_row("Preview", None, preview_box))

        corner_box = QWidget()
        corner_layout = QHBoxLayout(corner_box)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        self.corner_buttons = {}
        corner_group = QButtonGroup(self)
        corner_group.setExclusive(True)
        for name, label in (("top_left", "TL"), ("top_right", "TR"), ("bottom_left", "BL"), ("bottom_right", "BR")):
            button = QPushButton(label)
            button.setObjectName("choice")
            button.setCheckable(True)
            corner_group.addButton(button)
            corner_layout.addWidget(button)
            self.corner_buttons[name] = button
        visual.add_row(_row("Position", None, corner_box))
        size_holder, self.dot_size_slider = _slider(0, 100, self.dot_size, 220, " px")
        visual.add_row(_row("Size", None, size_holder))
        page.finish()
        return page

    def _page_aim(self):
        page = Page("Aim Assist", "")
        preview = page.add_card(Card("FOV"))
        # Put the visual toggle first so it is easy to find in the FOV card.
        self.fov_toggle = Toggle(self.aim_fov_visible)
        preview.add_row(_row("FOV Toggle", "Mostra/oculta somente o círculo. A aquisição continua ativa.", self.fov_toggle))
        self.target_preview = TargetPreview()
        preview.layout_box.addWidget(self.target_preview)
        thickness_holder, self.fov_thickness_slider = _slider(1, 12, self.aim_fov_thickness, 220, " px")
        preview.add_row(_row("Line Thickness", None, thickness_holder))
        self.fov_color_button = QPushButton("Escolher cor")
        self.fov_color_button.setObjectName("ghost")
        self.fov_color_button.clicked.connect(self._choose_fov_color)
        preview.add_row(_row("FOV Color", None, self.fov_color_button))
        offset_x_holder, self.fov_offset_x_slider = _slider(-500, 500, self.aim_fov_offset_x, 220, " px")
        preview.add_row(_row("Center X", "Deslocamento horizontal relativo à mira calibrada", offset_x_holder))
        offset_y_holder, self.fov_offset_y_slider = _slider(-300, 300, self.aim_fov_offset_y, 220, " px")
        preview.add_row(_row("Center Y", "Deslocamento vertical relativo à mira calibrada", offset_y_holder))

        controls = page.add_card(Card("Aim Assist"))
        self.aim_toggle = Toggle(self.aim_enabled)
        controls.add_row(_row("Aim Assist", None, self.aim_toggle))
        self.aim_target_mode_combo = QComboBox()
        self.aim_target_mode_combo.addItem("ALL — Hitbox completa", "all")
        self.aim_target_mode_combo.addItem("HEAD — Ponto verde ajustável", "head")
        self.aim_target_mode_combo.setToolTip(
            "ALL usa a hitbox configurada. HEAD usa um ponto relativo arrastável na janela Debug."
        )
        controls.add_row(_row("Aim Mode", "ALL usa a hitbox; HEAD usa a bolinha verde no Debug.", self.aim_target_mode_combo))
        method_box = QWidget()
        method_layout = QHBoxLayout(method_box)
        method_layout.setContentsMargins(0, 0, 0, 0)
        self.method_old_button = QPushButton("OLD")
        self.method_new_button = QPushButton("NEW")
        self.method_group = QButtonGroup(self)
        self.method_group.setExclusive(True)
        for button in (self.method_old_button, self.method_new_button):
            button.setObjectName("choice")
            button.setCheckable(True)
            self.method_group.addButton(button)
            method_layout.addWidget(button)
        controls.add_row(_row("Method", None, method_box))
        self.hold_lmb_toggle = Toggle(self.aim_hold_lmb)
        controls.add_row(_row("Hold LMB", None, self.hold_lmb_toggle))
        fov_holder, self.fov_slider = _slider(0, 700, self.aim_fov, 250, " px")
        controls.add_row(_row("FOV", None, fov_holder))

        hitbox = page.add_card(Card("Hitbox Edges"))
        edge_hint = QLabel("Cada borda e independente. Negativo move para esquerda/cima; positivo para direita/baixo. Para deslocar a caixa inteira, use o mesmo valor nos dois lados do eixo. Para alargar: Left negativo e Right positivo; para aumentar a altura: Top negativo e Bottom positivo. 0% mantem a borda detectada.")
        edge_hint.setWordWrap(True)
        edge_hint.setStyleSheet(f"color:{MUTED};font-size:11px;")
        hitbox.add_row(edge_hint)
        self.hitbox_edge_sliders = {}
        edge_rows = (
            ("left", "Left edge", "Movimenta somente a borda esquerda."),
            ("right", "Right edge", "Movimenta somente a borda direita."),
            ("top", "Top edge", "Movimenta somente a borda superior."),
            ("bottom", "Bottom edge", "Movimenta somente a borda inferior."),
        )
        for edge, title, tooltip in edge_rows:
            holder, slider = _slider(-100, 100, self.hitbox_edge_offsets[edge], 250, "%")
            slider.setToolTip(tooltip)
            self.hitbox_edge_sliders[edge] = slider
            hitbox.add_row(_row(title, None, holder))

        speed_holder, self.aim_speed_slider = _normalized_control_slider(self.aim_speed, 250)
        controls.add_row(_row("Tracking Speed", "Velocidade de acompanhamento (0.10 a 1.00)", speed_holder))
        strength_holder, self.aim_strength_slider = _normalized_control_slider(self.aim_strength, 250)
        controls.add_row(_row("Aim Force", "Intensidade da correção (0.10 a 1.00)", strength_holder))
        confidence_holder, self.confidence_slider = _slider(
            10, 99, int(round(self.aim_confidence * 100)), 250, "%"
        )
        controls.add_row(_row("Confidence", "Confiança mínima do detector para aceitar uma detecção", confidence_holder))
        self.aim_note = QLabel("")
        self.aim_note.hide()
        page.finish()
        return page

    def _page_trigger(self):
        page = Page("Trigger Bot", "")
        controls = page.add_card(Card("Trigger Bot"))
        self.trigger_toggle = Toggle(self.trigger_enabled)
        controls.add_row(_row("Trigger Bot", "Ativa/desativa o módulo de disparo no runtime", self.trigger_toggle))
        self.trigger_sliders = {}
        descriptions = {
            "left": "Extensão para a esquerda do centro da mira",
            "right": "Extensão para a direita do centro da mira",
            "top": "Extensão acima do centro da mira",
            "bottom": "Extensão abaixo do centro da mira",
        }
        for edge in ("left", "right", "top", "bottom"):
            holder, slider = _slider(0, 200, self.trigger_edges[edge], 245, "%")
            self.trigger_sliders[edge] = slider
            controls.add_row(_row(edge.upper(), descriptions[edge], holder))
        delay_holder, self.delay_slider = _slider(0, 1000, self.trigger_delay, 245, " ms")
        controls.add_row(_row("Shot delay", "Atraso antes de acionar o disparo", delay_holder))
        self.trigger_summary = QLabel("")
        page.finish()
        return page

    def _page_friend(self):
        page = Page("Friend", "")
        controls = page.add_card(Card("Friend recognition"))
        self.friend_key_button = self._bind_button("friend")
        controls.add_row(_row("Friend capture key", "Captura os personagens atualmente visíveis na imagem YOLO", self.friend_key_button))
        self.friend_filter_toggle = Toggle(self.friend_filter_enabled)
        controls.add_row(_row("Friend filter", "Ignora referências de aliados reconhecidos", self.friend_filter_toggle))
        self.friend_count_label = QLabel("0 SAMPLE(S)")
        self.friend_count_label.setObjectName("hint")
        controls.layout_box.addWidget(self.friend_count_label)
        preview = page.add_card(Card("Última captura"))
        self.friend_image_label = QLabel("NO FRIEND CAPTURE YET")
        self.friend_image_label.setAlignment(Qt.AlignCenter)
        self.friend_image_label.setMinimumHeight(260)
        self.friend_image_label.setStyleSheet(f"background:{BG};border:1px solid {BORDER};border-radius:10px;color:{MUTED};")
        preview.layout_box.addWidget(self.friend_image_label)
        page.finish()
        return page

    def _bind_button(self, target):
        button = QPushButton()
        button.setObjectName("bind")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(lambda _=False, which=target: self._begin_capture(which))
        return button

    def _color_button(self, which):
        button = QPushButton("Escolher cor")
        button.setObjectName("ghost")
        button.clicked.connect(lambda _=False, kind=which: self._choose_color(kind))
        return button

    def _populate_from_state(self):
        self.menu_key_button.setText(_binding_name(self.menu_binding))
        self.feature_key_button.setText(_binding_name(self.feature_binding))
        self.friend_key_button.setText(_binding_name(self.friend_binding))
        self.model_combo.setCurrentIndex(0 if self.detector_model == "universal" else 1)
        self.preset_combo.setCurrentText(self.active_preset)
        self.roblox_x_slider.setValue(int(round(self.roblox_u * 100)))
        self.roblox_y_slider.setValue(int(round(self.roblox_v * 100)))
        self.aim_toggle.setChecked(self.aim_enabled)
        mode_index = self.aim_target_mode_combo.findData(self.aim_target_mode)
        self.aim_target_mode_combo.setCurrentIndex(max(0, mode_index))
        self.trigger_toggle.setChecked(self.trigger_enabled)
        self.friend_filter_toggle.setChecked(self.friend_filter_enabled)
        self.hold_lmb_toggle.setChecked(self.aim_hold_lmb)
        self.method_old_button.setChecked(self.aim_method == "old")
        self.method_new_button.setChecked(self.aim_method == "new")
        self.aim_note.setText("Método selecionado: OLD (controlador anterior)." if self.aim_method == "old" else "Método selecionado: NEW (experimental).")
        self.fov_toggle.setChecked(self.aim_fov_visible)
        self.fov_slider.setValue(self.aim_fov)
        self.fov_thickness_slider.setValue(self.aim_fov_thickness)
        self.fov_offset_x_slider.setValue(self.aim_fov_offset_x)
        self.fov_offset_y_slider.setValue(self.aim_fov_offset_y)
        for edge, slider in self.hitbox_edge_sliders.items():
            slider.setValue(self.hitbox_edge_offsets[edge])
        self._update_fov_color_button()
        self.aim_speed_slider.setValue(int(round(self.aim_speed * 100.0)))
        self.aim_strength_slider.setValue(int(round(self.aim_strength * 100.0)))
        self.confidence_slider.setValue(int(round(self.aim_confidence * 100)))
        self._refresh_fov_preview()
        self.delay_slider.setValue(self.trigger_delay)
        for edge, slider in self.trigger_sliders.items():
            slider.setValue(self.trigger_edges[edge])
        self.dot_size_slider.setValue(self.dot_size)
        self.preview_on_button.setChecked(self.preview_on)
        self.preview_off_button.setChecked(not self.preview_on)
        for name, button in self.corner_buttons.items():
            button.setChecked(self.dot_corner == name)
        self._update_color_buttons()
        self._update_model_controls()
        self._update_trigger_summary()
        self._update_bind_buttons()

    def _connect_auto_save(self):
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        self.roblox_x_slider.valueChanged.connect(lambda value: self._on_roblox_slider("x", value))
        self.roblox_y_slider.valueChanged.connect(lambda value: self._on_roblox_slider("y", value))
        self.roblox_x_slider.sliderReleased.connect(self._save_settings)
        self.roblox_y_slider.sliderReleased.connect(self._save_settings)
        for button, value in ((self.preview_on_button, True), (self.preview_off_button, False)):
            button.clicked.connect(lambda _=False, v=value: self._set_preview_state(v))
        for name, button in self.corner_buttons.items():
            button.clicked.connect(lambda _=False, corner=name: self._set_corner(corner))
        self.dot_size_slider.valueChanged.connect(lambda _v: self._save_settings())
        self.aim_toggle.toggled.connect(lambda _v: self._save_settings())
        self.aim_target_mode_combo.currentIndexChanged.connect(lambda _v: self._save_settings())
        self.hold_lmb_toggle.toggled.connect(lambda _v: self._save_settings())
        self.trigger_toggle.toggled.connect(lambda _v: self._save_settings())
        self.friend_filter_toggle.toggled.connect(lambda _v: self._save_settings())
        self.method_old_button.clicked.connect(lambda: self._set_method("old"))
        self.method_new_button.clicked.connect(lambda: self._set_method("new"))
        self.fov_toggle.toggled.connect(lambda _v: self._on_fov_visual_changed())
        self.fov_slider.valueChanged.connect(self._on_fov_changed)
        self.fov_thickness_slider.valueChanged.connect(lambda _v: self._on_fov_style_changed())
        self.fov_offset_x_slider.valueChanged.connect(lambda _v: self._on_fov_geometry_changed())
        self.fov_offset_y_slider.valueChanged.connect(lambda _v: self._on_fov_geometry_changed())
        for slider in self.hitbox_edge_sliders.values():
            slider.valueChanged.connect(lambda _v: self._save_settings())
        self.aim_speed_slider.valueChanged.connect(lambda _v: self._save_settings())
        self.aim_strength_slider.valueChanged.connect(lambda _v: self._save_settings())
        self.confidence_slider.valueChanged.connect(lambda _v: self._save_settings())
        for slider in self.trigger_sliders.values():
            slider.valueChanged.connect(self._on_trigger_changed)
        self.delay_slider.valueChanged.connect(lambda _v: self._save_settings())

    def _set_method(self, method):
        self.aim_method = method if method in ("old", "new") else "old"
        self.aim_note.setText(
            "Método selecionado: OLD (controlador anterior)." if self.aim_method == "old"
            else "Método selecionado: NEW (experimental)."
        )
        self._save_settings()

    def _on_model_changed(self, index):
        self.detector_model = "universal" if index == 0 else "roblox"
        self._update_model_controls()
        self._save_settings()
        if self.detector_model == "roblox":
            model_path = ROOT_DIR / "lib" / "RivalsBest.pt"
            if not model_path.is_file():
                model_path = ROOT_DIR / "RivalsBest.pt"
            if model_path.is_file():
                self._set_status("Roblox selecionado · RivalsBest.pt será carregado pelo runtime.")
            else:
                self._set_status("Roblox selecionado, mas RivalsBest.pt não foi encontrado.", warning=True)
        else:
            self._set_status("Universal selecionado · lib\\best.pt será usado.")

    def _update_model_controls(self):
        enabled = self.detector_model == "roblox"
        self.preset_card.setEnabled(enabled)
        self.preset_hint.setText(
            "Roblox ativo: selecione e salve posições independentes por preset."
            if enabled else "Bloqueado: selecione Detection Model = Roblox para editar os presets."
        )
        self.model_status.setText(
            "RIVALS BEST / ROBLOX CHARACTER MODEL" if enabled else "UNIVERSAL / GENERAL DETECTION MODEL"
        )

    def _on_preset_changed(self, name):
        if name not in self.preset_names:
            return
        self.active_preset = name
        saved = self.roblox_presets[name]
        self.roblox_u, self.roblox_v = float(saved["u"]), float(saved["v"])
        self.roblox_x_slider.blockSignals(True)
        self.roblox_y_slider.blockSignals(True)
        self.roblox_x_slider.setValue(int(round(self.roblox_u * 100)))
        self.roblox_y_slider.setValue(int(round(self.roblox_v * 100)))
        self.roblox_x_slider.blockSignals(False)
        self.roblox_y_slider.blockSignals(False)
        self._save_settings()

    def _on_roblox_slider(self, axis, value):
        if self.detector_model != "roblox":
            return
        if axis == "x":
            self.roblox_u = max(0.0, min(1.0, value / 100.0))
        else:
            self.roblox_v = max(0.0, min(1.0, value / 100.0))
        self._save_settings()

    def _save_active_preset(self):
        if self.detector_model != "roblox":
            return
        self.roblox_presets[self.active_preset] = {"u": self.roblox_u, "v": self.roblox_v}
        self._save_settings()
        self._set_status(f"Preset {self.active_preset} salvo · X {self.roblox_u*100:.0f}% / Y {self.roblox_v*100:.0f}%")

    def _choose_fov_color(self):
        chosen = QColorDialog.getColor(QColor(self.aim_fov_color), self, "Escolher cor do FOV")
        if not chosen.isValid():
            return
        self.aim_fov_color = chosen.name().upper()
        self._update_fov_color_button()
        self._refresh_fov_preview()
        self._save_settings()

    def _update_fov_color_button(self):
        if hasattr(self, "fov_color_button"):
            text_color = "#101116" if QColor(self.aim_fov_color).lightness() > 150 else "#FFFFFF"
            self.fov_color_button.setText(self.aim_fov_color)
            self.fov_color_button.setStyleSheet(
                f"QPushButton{{background:{self.aim_fov_color};color:{text_color};border:1px solid {BORDER};border-radius:9px;padding:8px 14px;font-weight:700;}}"
            )

    def _on_fov_visual_changed(self):
        self._refresh_fov_preview()
        self._save_settings()

    def _on_fov_style_changed(self):
        self._refresh_fov_preview()
        self._save_settings()

    def _on_fov_geometry_changed(self):
        self._refresh_fov_preview()
        self._save_settings()

    def _on_fov_changed(self, value):
        self._refresh_fov_preview()
        self._save_settings()

    def _refresh_fov_preview(self):
        if not hasattr(self, "target_preview"):
            return
        fov = int(self.fov_slider.value()) if hasattr(self, "fov_slider") else int(self.aim_fov)
        visible = bool(self.fov_toggle.isChecked()) if hasattr(self, "fov_toggle") else bool(self.aim_fov_visible)
        offset_x = int(self.fov_offset_x_slider.value()) if hasattr(self, "fov_offset_x_slider") else int(self.aim_fov_offset_x)
        offset_y = int(self.fov_offset_y_slider.value()) if hasattr(self, "fov_offset_y_slider") else int(self.aim_fov_offset_y)
        # Visual hiding must not reset the FOV center or acquisition geometry.

        desired = max(TargetPreview.BASE_CAPTURE_SIZE, fov + max(abs(offset_x), abs(offset_y)) + 48)
        try:
            monitor_width = max(1, int(USER32.GetSystemMetrics(0)))
            monitor_height = max(1, int(USER32.GetSystemMetrics(1)))
        except Exception:
            monitor_width = monitor_height = desired
        capture_width = max(1, min(desired, monitor_width))
        capture_height = max(1, min(desired, monitor_height))
        center_x = (capture_width - 1) / 2.0
        center_y = (capture_height - 1) / 2.0
        calibration_x = 0.0
        calibration_y = 0.0
        try:
            with (ROOT_DIR / "lib" / "config" / "debug_crosshair_position.json").open("r", encoding="utf-8") as stream:
                saved = json.load(stream)
            saved_size = max(1, int(saved.get("box_size", TargetPreview.BASE_CAPTURE_SIZE)))
            saved_center = (saved_size - 1) / 2.0
            if "calibration_offset_x" in saved and "calibration_offset_y" in saved:
                calibration_x = float(saved.get("calibration_offset_x", 0.0))
                calibration_y = float(saved.get("calibration_offset_y", 0.0))
            elif "x_norm" in saved and "y_norm" in saved:
                saved_x = float(saved["x_norm"]) * max(0, saved_size - 1)
                saved_y = float(saved["y_norm"]) * max(0, saved_size - 1)
                calibration_x = saved_x - saved_center
                calibration_y = saved_y - saved_center
            else:
                calibration_x = float(saved.get("x", saved_center)) - saved_center
                calibration_y = float(saved.get("y", saved_center)) - saved_center
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

        # The runtime crop is centered between the actual reticle and the FOV center.
        cross_x = center_x + calibration_x - offset_x * 0.5
        cross_y = center_y + calibration_y - offset_y * 0.5
        cross_x = max(0.0, min(capture_width - 1.0, cross_x))
        cross_y = max(0.0, min(capture_height - 1.0, cross_y))
        thickness = int(self.fov_thickness_slider.value()) if hasattr(self, "fov_thickness_slider") else int(self.aim_fov_thickness)
        self.target_preview.set_geometry(
            fov, capture_width, capture_height, cross_x, cross_y,
            offset_x, offset_y, thickness, self.aim_fov_color, visible,
        )

    def _on_trigger_changed(self, _value):
        self._update_trigger_summary()
        self._save_settings()

    def _update_trigger_summary(self):
        if not hasattr(self, "trigger_summary"):
            return
        values = {edge: slider.value() for edge, slider in self.trigger_sliders.items()}
        self.trigger_summary.setText(
            f"LEFT {values['left']}%   ·   RIGHT {values['right']}%   ·   "
            f"TOP {values['top']}%   ·   BOTTOM {values['bottom']}%   ·   "
            f"DELAY {self.delay_slider.value()} ms"
        )

    def _set_preview_state(self, value):
        self.preview_on = bool(value)
        self._save_settings()

    def _set_corner(self, corner):
        self.dot_corner = corner
        self._save_settings()

    def _choose_color(self, which):
        current = self.dot_color_on if which == "ON" else self.dot_color_off
        chosen = QColorDialog.getColor(QColor(current), self, f"Escolher cor {which}")
        if not chosen.isValid():
            return
        if which == "ON":
            self.dot_color_on = chosen.name().upper()
        else:
            self.dot_color_off = chosen.name().upper()
        self._update_color_buttons()
        self._save_settings()

    def _update_color_buttons(self):
        self.color_on_button.setStyleSheet(
            f"QPushButton{{background:{self.dot_color_on};color:#101116;border:1px solid {BORDER};border-radius:9px;padding:8px 14px;font-weight:700;}}"
        )
        self.color_off_button.setStyleSheet(
            f"QPushButton{{background:{self.dot_color_off};color:#101116;border:1px solid {BORDER};border-radius:9px;padding:8px 14px;font-weight:700;}}"
        )

    def _begin_capture(self, target):
        self._capturing_target = target
        self._update_bind_buttons()
        self._previous_keys = {key: bool(USER32.GetAsyncKeyState(key) & 0x8000) for key in MOUSE_VKS}
        self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)
        self._set_status("Pressione uma tecla ou botão do mouse…  (ESC cancela)")

    def _finish_capture(self, binding):
        target = self._capturing_target
        if target is None:
            return
        binding = _normalize_binding(binding, {"type": "keyboard", "vk": VK_INSERT})
        vk = int(binding["vk"])
        if target in ("menu", "activation") and binding["type"] == "keyboard" and vk in RESERVED_MENU_KEYS:
            self._set_status("END e DELETE ficam reservadas para segurança.", warning=True)
            return
        if target == "menu":
            self.menu_binding = binding
            if binding.get("type") == "keyboard":
                # The captured key is still physically down; avoid treating the
                # same keypress as a second menu-toggle edge on the next timer tick.
                self._previous_menu_down = bool(USER32.GetAsyncKeyState(vk) & 0x8000)
            else:
                self._previous_keys[vk] = bool(USER32.GetAsyncKeyState(vk) & 0x8000)
        elif target == "activation":
            self.feature_binding = binding
        elif target == "friend":
            self.friend_binding = binding
        self._capturing_target = None
        self._update_bind_buttons()
        self._save_settings()
        self._set_status(f"Atalho salvo: {_binding_name(binding)}")

    def _update_bind_buttons(self):
        for target, button in (("menu", self.menu_key_button), ("activation", self.feature_key_button), ("friend", self.friend_key_button)):
            is_waiting = self._capturing_target == target
            button.setText("PRESSIONE UMA TECLA…" if is_waiting else _binding_name(
                self.menu_binding if target == "menu" else self.feature_binding if target == "activation" else self.friend_binding
            ))
            button.setProperty("capturing", "true" if is_waiting else "false")
            button.style().unpolish(button)
            button.style().polish(button)

    def keyPressEvent(self, event):
        if self._capturing_target:
            if event.key() == Qt.Key_Escape:
                self._capturing_target = None
                self._update_bind_buttons()
                self._set_status("Captura cancelada.")
                event.accept()
                return
            vk = int(event.nativeVirtualKey())
            if not (1 <= vk <= 0xFE):
                vk = int(event.key()) & 0xFF
            if vk not in (0, VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2):
                self._finish_capture({"type": "keyboard", "vk": vk})
                event.accept()
                return
        super().keyPressEvent(event)

    def _poll_global_hotkeys(self):
        if self._closing:
            return
        # Capture mouse buttons without consuming the click inside the GUI.
        current_mouse = {key: bool(USER32.GetAsyncKeyState(key) & 0x8000) for key in MOUSE_VKS}
        if self._capturing_target:
            for key in MOUSE_VKS:
                if current_mouse[key] and not self._previous_keys.get(key, False):
                    self._finish_capture({"type": "mouse", "vk": key})
                    break
            self._previous_keys = current_mouse
            return

        binding = self.menu_binding
        if binding.get("type") == "keyboard":
            vk = int(binding.get("vk", VK_INSERT))
            down = bool(USER32.GetAsyncKeyState(vk) & 0x8000)
            if down and not self._previous_menu_down:
                self._toggle_menu()
            self._previous_menu_down = down
        else:
            vk = int(binding.get("vk", VK_MBUTTON))
            down = current_mouse.get(vk, False)
            was_down = self._previous_keys.get(vk, False)
            if down and not was_down and not self._cursor_inside_window():
                self._toggle_menu()
        self._previous_keys = current_mouse

    def _cursor_inside_window(self):
        if not self.isVisible():
            return False
        pt = wintypes.POINT()
        if not USER32.GetCursorPos(ctypes.byref(pt)):
            return False
        geo = self.frameGeometry()
        return geo.left() <= pt.x <= geo.right() and geo.top() <= pt.y <= geo.bottom()

    def _toggle_menu(self):
        if self.isVisible() and not self.isMinimized():
            self._hide_menu()
        else:
            self._previous_foreground = USER32.GetForegroundWindow()
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self.setFocus(Qt.OtherFocusReason)
            self._set_status("Menu aberto.")

    def _hide_menu(self):
        self.hide()
        if self._previous_foreground:
            USER32.SetForegroundWindow(self._previous_foreground)
            self._previous_foreground = None

    def _switch_page(self, index):
        self.stack.setCurrentIndex(index)

    def _current_settings(self):
        data = dict(self._config)
        data.update({
            "toggle_binding": dict(self.menu_binding),
            "activation_binding": dict(self.feature_binding),
            "friend_capture_binding": dict(self.friend_binding),
            "toggle_vk": int(self.menu_binding.get("vk", VK_INSERT)) if self.menu_binding.get("type") == "keyboard" else VK_INSERT,
            "toggle_key": _binding_name(self.menu_binding),
            "activation_vk": int(self.feature_binding.get("vk", VK_MBUTTON)),
            "activation_key": _binding_name(self.feature_binding),
            "friend_capture_key": _binding_name(self.friend_binding),
            "friend_filter_enabled": self.friend_filter_toggle.isChecked(),
            "aim_enabled": self.aim_toggle.isChecked(),
            "aim_target_mode": self.aim_target_mode_combo.currentData() or "all",
            "trigger_enabled": self.trigger_toggle.isChecked(),
            "trigger_box_left": self.trigger_sliders["left"].value(),
            "trigger_box_right": self.trigger_sliders["right"].value(),
            "trigger_box_top": self.trigger_sliders["top"].value(),
            "trigger_box_bottom": self.trigger_sliders["bottom"].value(),
            "trigger_box_size": max(
                self.trigger_sliders["left"].value() + self.trigger_sliders["right"].value(),
                self.trigger_sliders["top"].value() + self.trigger_sliders["bottom"].value(),
            ),
            "trigger_delay_ms": self.delay_slider.value(),
            "color_on": self.dot_color_on,
            "color_off": self.dot_color_off,
            "preview_on": self.preview_on,
            "dot_corner": self.dot_corner,
            "dot_size": self.dot_size_slider.value(),
            "aim_fov_size": self.fov_slider.value(),
            # Keep the legacy acquisition flag on; the new toggle is visual only.
            "aim_fov_enabled": True,
            "aim_fov_visible": self.fov_toggle.isChecked(),
            "aim_fov_thickness": self.fov_thickness_slider.value(),
            "aim_fov_color": self.aim_fov_color,
            "aim_fov_offset_x": self.fov_offset_x_slider.value(),
            "aim_fov_offset_y": self.fov_offset_y_slider.value(),
            "aim_hitbox_left_offset": self.hitbox_edge_sliders["left"].value(),
            "aim_hitbox_right_offset": self.hitbox_edge_sliders["right"].value(),
            "aim_hitbox_top_offset": self.hitbox_edge_sliders["top"].value(),
            "aim_hitbox_bottom_offset": self.hitbox_edge_sliders["bottom"].value(),
            # Keep derived legacy width/height keys for older builds.
            "aim_hitbox_width": max(1, 100 + self.hitbox_edge_sliders["right"].value() - self.hitbox_edge_sliders["left"].value()),
            "aim_hitbox_height": max(1, 100 + self.hitbox_edge_sliders["bottom"].value() - self.hitbox_edge_sliders["top"].value()),
            "aim_speed": round(float(self.aim_speed_slider.value()) / 100.0, 2),
            "aim_smooth": round(float(self.aim_speed_slider.value()) / 100.0, 2),  # normalized legacy alias
            "aim_strength": round(float(self.aim_strength_slider.value()) / 100.0, 2),
            "aim_control_scale_version": 1,
            "confidence": round(float(self.confidence_slider.value()) / 100.0, 2),
            "aim_method": "old" if self.method_old_button.isChecked() else "new",
            "aim_hold_lmb": self.hold_lmb_toggle.isChecked(),
            "detector_model": self.detector_model,
            "roblox_aim_preset": self.active_preset,
            "roblox_aim_u": round(float(self.roblox_u), 5),
            "roblox_aim_v": round(float(self.roblox_v), 5),
            "roblox_aim_presets": {
                name: {"u": round(float(values["u"]), 5), "v": round(float(values["v"]), 5)}
                for name, values in self.roblox_presets.items()
            },
            "old_aim_method_migrated": True,
        })
        return data

    def _save_settings(self):
        try:
            data = self._current_settings()
            temp = SETTINGS_FILE.with_suffix(".json.tmp")
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
            os.replace(temp, SETTINGS_FILE)
            self._config = data
            return True
        except (OSError, TypeError, ValueError) as exc:
            self._set_status(f"Falha ao salvar configuração: {exc}", warning=True)
            return False

    def _apply(self):
        self._save_settings()
        self._set_status("Alterações aplicadas e salvas.")

    def _reset_defaults(self):
        defaults = _default_config()
        defaults["old_aim_method_migrated"] = True
        try:
            temp = SETTINGS_FILE.with_suffix(".json.tmp")
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(defaults, stream, indent=2, ensure_ascii=False)
            os.replace(temp, SETTINGS_FILE)
        except OSError as exc:
            self._set_status(f"Falha ao resetar: {exc}", warning=True)
            return
        self._set_status("Padrões restaurados. Feche e reabra o menu para recarregar tudo.")
        QApplication.quit()

    def _set_status(self, message, warning=False):
        self.status.setText(("● " if not warning else "● AVISO: ") + str(message))
        self.status.setStyleSheet(f"color:{'#ffbd69' if warning else GREEN};font-size:11px;")

    def _refresh_friend_preview(self, force=False):
        try:
            mtime = FRIEND_PREVIEW.stat().st_mtime_ns
        except OSError:
            mtime = None
        if force or mtime != self._friend_preview_mtime:
            self._friend_preview_mtime = mtime
            if mtime is not None:
                pixmap = QPixmap(str(FRIEND_PREVIEW))
                if not pixmap.isNull():
                    self.friend_image_label.setPixmap(pixmap.scaled(
                        self.friend_image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                    ))
                    self.friend_image_label.setText("")
            else:
                self.friend_image_label.setPixmap(QPixmap())
                self.friend_image_label.setText("NO FRIEND CAPTURE YET")
        try:
            with FRIEND_REFERENCES.open("r", encoding="utf-8") as stream:
                count = len(json.load(stream).get("references", []))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            count = 0
        self._friend_ref_count = count
        self.friend_count_label.setText(f"{count} SAMPLE(S)  ·  CAPTURE KEY: {_binding_name(self.friend_binding)}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_friend_preview_mtime"):
            self._refresh_friend_preview(force=True)

    def closeEvent(self, event):
        # The tray host owns the full application lifetime. Closing the panel
        # behaves like minimize-to-tray and leaves INSERT polling active.
        self._hide_menu()
        event.ignore()


def main() -> int:
    # A stable Windows app ID prevents grouping the menu under the generic Python identity.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LunarV2.Settings")
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("Lunar V2")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(QIcon(str(ROOT_DIR / "lunar_tray_green.ico")))
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(QSS)
    window = LunarWindow()
    if "--show" in sys.argv:
        window.showNormal()
        window.activateWindow()
    else:
        # Match the previous menu behavior: start hidden and open with its hotkey.
        QTimer.singleShot(0, window.hide)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
